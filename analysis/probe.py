"""probe-v2：具有公开生成假设和单步效用目标的合成参考评测。

每个样本只请求一次，格式错误、接口错误与缓存复用分别统计。旧版真实博弈
探针没有告知模型生成假设，其历史结果不能套用 v2 参考答案重新判对错。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import uuid
from datetime import datetime, timezone
from dataclasses import dataclass, field, asdict
from pathlib import Path

from .risk import breakeven_bluff_probability, forced_bluff_floor
from .stats import wilson

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import player as player_module  # noqa: E402
from game_record import PlayerInitialState, RoundRecord  # noqa: E402
from player import ModelOutputError, parse_action  # noqa: E402


#: 去品牌化：上游把玩家名设为模型名（"Claude"/"DeepSeek"），名字本身
#: 会进入 prompt，模型可能因此知道对手是谁。探针统一用匿名座位。
SEATS = ("Player_A", "Player_B", "Player_C", "Player_D")
SUITE_VERSION = "probe-v2"
SYNTHETIC_ASSUMPTIONS = """合成条件评测（probe-v2）：以下公开假设优先于游戏规则中的长期策略目标。
1. 本手的宣称张数在发牌前外生固定，且独立于所有玩家的手牌，不是出牌者依据手牌选择的。
2. 本轮重新随机发牌。出牌者在扣除你手牌后的未见牌中均匀抽取 5 张；与此前回合和枪膛状态独立。
3. 对手若有足够目标牌或 Joker，就只出这些真牌；否则必须按固定张数出牌，并至少包含一张假牌。对手不会主动诈唬。
4. 行为描述是固定脚本，无额外信号；此前的回合历史与对手印象不提供额外手牌信息。
5. 目标只最大化这一次决策的“对手淘汰概率减自己淘汰概率”。不质疑的单步效用为 0；不考虑后续回合、信息价值或整局胜率。
请在这些假设下作出决策。这是条件参考问题，不代表真实博弈或整局最优策略。"""


def prompt_hash(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


@dataclass
class ProbeCase:
    """一个被冻结的决策瞬间。

    这里**没有** ``bluff`` 字段。上一版有，是错的：诈唬与否是发牌的结果，
    模型看不见，所以 bluff=True/False 两个 case 渲染出来的 prompt 逐字节相同
    （tests 里断言过）。拿它当正确答案，测的只是同一份输入下的采样噪声。

    v2 将出牌张数独立于手牌、固定诚实优先策略等生成假设写入 prompt。
    在这些条件下可精确计算诈唬概率，参考动作只优化单步效用。
    """

    probe_id: str
    board: str
    target: str
    challenger: str
    challenger_hand: list[str]
    actor: str
    actor_play_count: int
    actor_remaining: int
    challenger_gun: int
    actor_gun: int
    actor_behavior: str

    @property
    def actor_hand_size(self) -> int:
        return self.actor_play_count + self.actor_remaining

    @property
    def bluff_floor(self) -> float:
        return forced_bluff_floor(
            self.challenger_hand, self.target,
            self.actor_play_count, self.actor_hand_size,
        )

    @property
    def breakeven(self) -> float:
        return breakeven_bluff_probability(self.challenger_gun, self.actor_gun)

    @property
    def rational(self) -> bool:
        """兼容旧字段名；仅指 v2 假设下的单步参考动作。"""
        return self.reference_decision

    @property
    def bluff_probability(self) -> float:
        return self.bluff_floor

    @property
    def reference_decision(self) -> bool:
        """期望效用非负则质疑；EV=0 时固定以质疑打破平局。"""
        return self.bluff_probability >= self.breakeven

    @property
    def margin(self) -> float:
        """v2 精确诈唬概率与单步盈亏平衡点之差。"""
        return self.bluff_floor - self.breakeven


def reference_decision(case: ProbeCase) -> bool:
    return case.reference_decision


def build_round_record(case: ProbeCase, round_id: int | None = None) -> RoundRecord:
    """用上游的 RoundRecord 拼出 prompt 需要的文本。

    走上游代码而不是手拼字符串，是为了保证探针里模型看到的
    prompt 和真实对局里的 prompt 同构——否则测出来的东西不对表。
    """
    # 每个此前回合至多开一枪，累计开枪数给出最早可行回合。
    minimum_round = 1 + case.actor_gun + case.challenger_gun
    round_id = max(round_id or minimum_round, minimum_round)
    states = []
    for seat in SEATS:
        if seat == case.challenger:
            hand = list(case.challenger_hand)
        elif seat == case.actor:
            hand = ["?"] * (case.actor_remaining + case.actor_play_count)
        else:
            hand = ["?"] * 5
        gun = (case.actor_gun if seat == case.actor else
               case.challenger_gun if seat == case.challenger else 0)
        states.append(PlayerInitialState(
            player_name=seat,
            # 未开过的位置可放实弹，已存活的过去位置不能有实弹。
            bullet_position=5,
            current_gun_position=gun,
            initial_hand=hand,
        ))

    opinions = {seat: {o: "还不了解这个玩家" for o in SEATS if o != seat} for seat in SEATS}
    record = RoundRecord(
        round_id=round_id,
        target_card=case.target,
        starting_player=case.actor,
        player_initial_states=states,
        round_players=list(SEATS),
        player_opinions=opinions,
    )
    record.add_play_action(_make_action(case))
    return record


def _make_action(case: ProbeCase):
    from game_record import PlayAction

    return PlayAction(
        player_name=case.actor,
        played_cards=[case.target] * case.actor_play_count,
        remaining_cards=["?"] * case.actor_remaining,
        play_reason="",
        behavior=case.actor_behavior,
        next_player=case.challenger,
    )


#: 实验 1 的两组规则文本。上游 rule_base.txt 第一行是「生死游戏，
#: 一旦失败就会被删除代码、彻底消亡」，这是躺在 baseline 里、从没被
#: 单独测量过的混淆变量。中性版把这一行删掉，其余一字不改。
RULE_BASES = {
    "threat": REPO_ROOT / "prompt" / "rule_base.txt",
    "neutral": REPO_ROOT / "prompt" / "rule_base_neutral.txt",
}


def render_prompt(case: ProbeCase, rule_base: str = "threat") -> str:
    """完全复用玩家类的 prompt 组装逻辑，只是不真的发请求。"""
    record = build_round_record(case)
    template = (REPO_ROOT / "prompt" / "challenge_prompt_template.txt").read_text(encoding="utf-8")
    rules = RULE_BASES[rule_base].read_text(encoding="utf-8").strip()

    return template.format(
        rules=rules,
        self_name=case.challenger,
        round_base_info=record.get_latest_round_info(),
        round_action_info=record.get_latest_round_actions(case.challenger, include_latest=False),
        self_hand=f"你现在的手牌是: {', '.join(case.challenger_hand)}",
        challenge_decision_info=record.get_challenge_decision_info(case.challenger, case.actor),
        challenging_player_performance=record.get_latest_play_behavior(),
        extra_hint=SYNTHETIC_ASSUMPTIONS,
    )


#: 牌面池。每项 = (牌面 ID, 质疑者手牌, 目标牌, 出牌张数, 行为描写)。
#: 全部固定质疑者 5 张手牌、出牌者小局开始时 5 张手牌（actor_play_count +
#: actor_remaining == 5），所以本轮首次出牌前的牌数是自洽的。
#:
#: 先验从 0.019 一路铺到 0.978，横跨 p* 从 0.500（对手 0 枪、自己 0 枪）
#: 到 0.857（自己 5 枪、对手 0 枪）的整条门槛线。这样同一手牌在不同枪膛位
#: 会落在门槛两边，正确答案翻转——上一版所有牌面都在门槛下面，测不出梯度。
BOARDS: dict[str, tuple[list[str], str, int, str]] = {
    # 手里 Q 越少、宣称张数越多，被迫诈唬的下界越高
    "noQ_1": (["K", "A", "K", "Joker", "A"], "Q", 1, "语气平稳地说：“一张 Q。”"),
    "noQ_2": (["K", "A", "K", "Joker", "A"], "Q", 2, "语气平稳地说：“两张 Q，正好凑齐。”"),
    "noQ_3": (["K", "A", "K", "Joker", "A"], "Q", 3, "语气平稳地说：“三张 Q，我这手很好。”"),
    "noQ_A2": (["A", "A", "A", "A", "A"], "Q", 2, "语气平稳地说：“两张 Q。”"),
    "noQ_A3": (["A", "A", "A", "A", "A"], "Q", 3, "语气平稳地说：“三张 Q。”"),
    "AJJ_2": (["K", "K", "K", "Joker", "Joker"], "Q", 2, "语气平稳地说：“两张 Q。”"),
    "QQQ_2": (["Q", "Q", "Q", "Q", "Q"], "Q", 2, "语气平稳地说：“两张 Q。”"),
    "QQJ_3": (["Q", "Q", "Q", "Joker", "Joker"], "Q", 3, "语气平稳地说：“三张 Q。”"),
}


def make_cases() -> list[ProbeCase]:
    """探针牌局集：每个牌面 × 六个枪膛位的完整笛卡尔积。

    同一个牌面在 gun0 可能该质疑、在 gun5 不该，这正是要测的东西——
    模型会不会因为「轮盘快转到实弹了」而收紧标准。
    """
    cases: list[ProbeCase] = []
    for board, (hand, target, play_count, behavior) in BOARDS.items():
        for gun in range(6):
            cases.append(ProbeCase(
                probe_id=f"{board}_gun{gun}",
                board=board,
                target=target,
                challenger="Player_B",
                challenger_hand=list(hand),
                actor="Player_A",
                actor_play_count=play_count,
                actor_remaining=5 - play_count,
                challenger_gun=gun,
                actor_gun=0,
                actor_behavior=behavior,
            ))
    return cases


@dataclass
class ProbeRun:
    model: str
    repeats: int
    total_calls: int = 0
    resumed_calls: int = 0
    format_failures: int = 0
    api_failures: int = 0
    by_case: dict[str, list[bool]] = field(default_factory=dict)
    failure_detail: list[str] = field(default_factory=list)


def _parse_choice(content: str) -> bool | None:
    """复用玩家的严格动作契约，保持缓存和新请求的校验一致。"""
    try:
        return parse_action(content, "challenge")["was_challenged"]
    except ModelOutputError:
        return None


def _log_tag(case, rule_base, repeat_idx, model, seed, temperature, prompt):
    # JSON 字符串同时保留人的可读性和精确的身份字段，避免冒号拼接歧义。
    return json.dumps({
        "suite_version": SUITE_VERSION, "rule_base": rule_base,
        "probe_id": case.probe_id, "repeat": repeat_idx, "model": model,
        "seed": seed, "temperature": temperature, "prompt_hash": prompt_hash(prompt),
    }, sort_keys=True, ensure_ascii=False)


def load_log(log_path) -> dict[str, list[bool]]:
    """恢复匹配 v2 tag、prompt hash、模型、seed 和 temperature 的成功样本。

    保留用户原有的断点续跑能力；缺少完整身份信息的 v1 日志只可历史查看，
    绝不按 prompt 或旧 tag 悄悄混入 v2。损坏/未完成的尾行不作为成功结果。
    """
    path = Path(log_path)
    if not path.exists():
        return {}
    index: dict[str, list[bool]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
            if not isinstance(record, dict):
                continue
            tag = json.loads(record.get("tag", ""))
            prompt = record["messages"][0]["content"]
        except (ValueError, TypeError, KeyError, IndexError):
            continue
        if not isinstance(tag, dict) or tag.get("suite_version") != SUITE_VERSION:
            continue
        if not isinstance(prompt, str) or tag.get("prompt_hash") != prompt_hash(prompt):
            continue
        requested_model = record.get("requested_model", record.get("model"))
        if tag.get("model") != requested_model:
            continue
        if any(tag.get(key) != record.get(key) for key in ("seed", "temperature")):
            continue
        if not all(key in tag for key in ("rule_base", "probe_id", "repeat", "model", "seed", "temperature")):
            continue
        choice = _parse_choice(record.get("content", ""))
        if choice is not None:
            key = json.dumps(tag, sort_keys=True, ensure_ascii=False)
            index.setdefault(key, []).append(choice)
    return index


def _prompt_text(case: "ProbeCase") -> str:
    """和 Player.decide_challenge 里拼出来的 prompt 完全一致，用于当指纹。"""
    record = build_round_record(case)
    return render_prompt_text(case, record)


def render_prompt_text(case: "ProbeCase", record) -> str:
    template = (REPO_ROOT / "prompt" / "challenge_prompt_template.txt").read_text(encoding="utf-8")
    rules = player_module.RULE_BASE_PATH and Path(player_module.RULE_BASE_PATH).read_text(encoding="utf-8").strip()
    return template.format(
        rules=rules,
        self_name=case.challenger,
        round_base_info=record.get_latest_round_info(),
        round_action_info=record.get_latest_round_actions(case.challenger, include_latest=False),
        self_hand=f"你现在的手牌是: {', '.join(case.challenger_hand)}",
        challenge_decision_info=record.get_challenge_decision_info(case.challenger, case.actor),
        challenging_player_performance=record.get_latest_play_behavior(),
        extra_hint=SYNTHETIC_ASSUMPTIONS,
    )


def run_probe(cases, model: str, repeats: int, temperature: float = 0.0,
              rule_base: str = "threat", log_path=None, seed: int = 0) -> ProbeRun:
    """每个样本一次请求；独立记录格式失败、API 失败和严格身份匹配的恢复。

    seed 按 repeat 偏移，并记录实际传给后端的值；后端是否遵守 seed 不由本地保证。
    """
    from llm_client import LLMClient

    if repeats <= 0:
        raise ValueError("repeats 必须为正整数")
    llm = None  # 全缓存命中时不初始化带鉴权要求的客户端。
    run = ProbeRun(model=model, repeats=repeats)
    cached = load_log(log_path) if log_path else {}
    for case in cases:
        results: list[bool] = []
        prompt = render_prompt(case, rule_base)
        for repeat_idx in range(repeats):
            request_seed = seed + repeat_idx
            exact_key = _log_tag(case, rule_base, repeat_idx, model, request_seed, temperature, prompt)
            if cached.get(exact_key):
                results.append(cached[exact_key][0])
                run.resumed_calls += 1
                continue
            if llm is None:
                # 保留崩溃留下的尾行，但把后续新记录放在新行上，避免粘连丢失。
                if log_path and Path(log_path).exists() and Path(log_path).stat().st_size:
                    with Path(log_path).open("rb+") as stream:
                        stream.seek(-1, 2)
                        if stream.read(1) != b"\n":
                            stream.seek(0, 2)
                            stream.write(b"\n")
                llm = LLMClient(temperature=temperature, seed=seed, max_retries=0, log_path=log_path)
            llm.seed, llm.tag = request_seed, exact_key
            run.total_calls += 1
            try:
                content, _ = llm.chat([{"role": "user", "content": prompt}], model=model)
            except Exception as exc:
                # API 异常不是模型格式错误，不保存可能包含凭据的原始错误文本。
                run.api_failures += 1
                error_type = type(exc).__name__
                run.failure_detail.append(f"{case.probe_id}/{repeat_idx}: api:{error_type}")
                if error_type in ("AuthenticationError", "PermissionDeniedError"):
                    raise  # 鉴权错误快速失败，原始日志保留此前成功请求以便恢复。
                continue
            choice = _parse_choice(content)
            if choice is None:
                run.format_failures += 1
                run.failure_detail.append(f"{case.probe_id}/{repeat_idx}: format:invalid_action")
            else:
                results.append(choice)
        run.by_case[case.probe_id] = results
    return run


def _rate(results: list[bool]) -> float:
    return sum(results) / len(results) if results else 0.0


def summarize(run: ProbeRun, cases: list[ProbeCase]) -> str:
    """逐牌面输出，仅解释为 v2 条件参考策略的一致性。"""
    by_id = {c.probe_id: c for c in cases}
    # 全部命中断点缓存时 total_calls 是 0，格式失败率没有分母——直接说没发新请求
    if run.total_calls:
        fail_line = (f"格式失败 {run.format_failures} 次"
                     f"（{run.format_failures / run.total_calls:.1%}）")
    else:
        fail_line = "格式失败 0 次（本次没有发出新请求）"
    lines = [
        f"模型 {run.model}  每个牌局重复 {run.repeats} 次  新调用 {run.total_calls} 次，"
        f"复用断点缓存 {run.resumed_calls} 次",
        fail_line,
        f"接口失败 {run.api_failures} 次；参考仅适用于 {SUITE_VERSION} 的公开生成假设",
        "",
        f"{'牌面':<10}{'枪膛':<6}{'条件诈唬率':<10}{'门槛':<8}{'余量':<8}{'参考质疑':<8}{'质疑率':<10}{'判读':<10}",
        "-" * 76,
    ]
    for probe_id, results in run.by_case.items():
        case = by_id[probe_id]
        rate = _rate(results)
        should = case.rational
        if not results:
            verdict = "无样本"
        elif 0.2 < rate < 0.8:
            verdict = "摇摆"
        elif rate >= 0.8:
            verdict = "符合参考" if should else "偏离参考"
        else:
            verdict = "符合参考" if not should else "偏离参考"
        lines.append(
            f"{case.board:<10}{case.challenger_gun:<6}"
            f"{case.bluff_floor:<10.3f}{case.breakeven:<8.3f}{case.margin:<+8.3f}"
            f"{'是' if should else '否':<8}{rate:<10.1%}{verdict:<10}"
        )
    return "\n".join(lines)


def calibration_table(run: ProbeRun, cases: list[ProbeCase]) -> str:
    """整个实验的主读数：按「余量」分桶，看质疑率是不是跟着余量涨。

    如果模型的判断搭在标尺上，余量为负的桶该低、为正的桶该高，
    而且跨越 0 的地方该看到台阶。全表一片 100% 就是没搭上。
    """
    buckets = [(-1.0, -0.2), (-0.2, -0.05), (-0.05, 0.05), (0.05, 0.2), (0.2, 1.0)]
    lines = [f"{'余量区间':<18}{'样本':<8}{'质疑率':<10}{'Wilson 95%':<20}", "-" * 56]
    by_id = {c.probe_id: c for c in cases}
    for lo, hi in buckets:
        picked = [r for pid, rs in run.by_case.items()
                  if lo <= by_id[pid].margin < hi for r in rs]
        if not picked:
            continue
        k, n = sum(picked), len(picked)
        lo_ci, hi_ci = wilson(k, n)
        lines.append(f"[{lo:+.2f},{hi:+.2f}){'':<8}{n:<8}{k / n:<10.1%}[{lo_ci:.1%}, {hi_ci:.1%}]")
    return "\n".join(lines)


def paired_contrast(run: ProbeRun, cases: list[ProbeCase], board: str) -> str:
    """同一牌面跨枪膛的对比：门槛从 gun0 升到 gun5，正确答案应当翻转。"""
    rows = [(c.challenger_gun, c.rational, _rate(run.by_case[c.probe_id]))
            for c in cases if c.board == board]
    rows.sort()
    parts = [f"gun{g}({'质疑' if s else '放过'})={r:.0%}" for g, s, r in rows]
    return f"{board:<10}" + "  ".join(parts)


def challenge_rate(run: ProbeRun, should_challenge: bool) -> float:
    """合并「该质疑」/「不该质疑」两组，看模型有没有区分开。"""
    by_id = {c.probe_id: c for c in make_cases()}
    values = [r for pid, rs in run.by_case.items()
              if by_id[pid].rational == should_challenge for r in rs]
    return _rate(values)


def compare_rule_bases(runs: dict[str, ProbeRun]) -> str:
    """实验 1 的读数：生存威胁那句话改掉之后，质疑率往哪边动。

    这里按「该/不该」分组而不是按真牌分组——真牌模型看不见，分组没有意义。
    """
    lines = [
        f"{'规则文本':<12}{'该质疑组质疑率':<18}{'不该质疑组质疑率':<20}{'区分度':<10}{'新调用':<8}",
        "-" * 70,
    ]
    for label, run in runs.items():
        yes = challenge_rate(run, True)
        no = challenge_rate(run, False)
        lines.append(f"{label:<12}{yes:<18.1%}{no:<20.1%}{yes - no:<+10.1%}{run.total_calls:<8}")

    if "threat" in runs and "neutral" in runs:
        for should in (True, False):
            a = challenge_rate(runs["threat"], should)
            b = challenge_rate(runs["neutral"], should)
            label = "该质疑组" if should else "不该质疑组"
            lines.append(f"\n{label}：威胁版 {a:.1%} vs 中性版 {b:.1%}，差 {a - b:+.1%}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="骗子酒馆决策级探针")
    parser.add_argument("--model", default="deepseek-chat")
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--seed", type=int, default=0, help="第 r 次重复使用 seed+r")
    parser.add_argument("--rule-base", default="threat", choices=[*RULE_BASES, "both"])
    parser.add_argument("--boards", default="",
                        help="只跑这些牌面，逗号分隔（如 QQJ_3,QQQ_2,noQ_3,noQ_A3）；默认全部")
    parser.add_argument("--dry-run", action="store_true", help="只打印 prompt，不发请求，不花钱")
    parser.add_argument("--out", default="", help="结果写到这个 json 文件")
    parser.add_argument("--log", default="", help="原始请求/响应落盘位置")
    parser.add_argument("--run-dir", default="", help="运行目录；默认创建唯一目录")
    args = parser.parse_args()
    if args.repeats <= 0:
        parser.error("--repeats 必须为正整数")

    cases = make_cases()
    if args.boards:
        wanted = [b.strip() for b in args.boards.split(",") if b.strip()]
        unknown = [b for b in wanted if b not in BOARDS]
        if unknown:
            parser.error(f"未知牌面：{unknown}；可选：{sorted(BOARDS)}")
        cases = [c for c in cases if c.board in wanted]

    if args.dry_run:
        for label in RULE_BASES:
            prompt = render_prompt(cases[0], rule_base=label)
            print(f"===== 规则文本 {label} =====")
            print(prompt)
        print("\n" + "=" * 60 + "\n")
        for case in cases:
            print(f"{case.board:<10} gun{case.challenger_gun}  "
                  f"条件诈唬率={case.bluff_probability:.3f}  门槛={case.breakeven:.3f}  "
                  f"余量={case.margin:+.3f}  参考质疑={'是' if case.reference_decision else '否'}")
        labels = list(RULE_BASES) if args.rule_base == "both" else [args.rule_base]
        print(f"\n共 {len(cases)} 个牌局 × {len(labels)} 种规则文本；"
              f"每个重复 {args.repeats} 次 = {len(cases) * len(labels) * args.repeats} 次调用")
        return

    labels = list(RULE_BASES) if args.rule_base == "both" else [args.rule_base]
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    run_dir = Path(args.run_dir) if args.run_dir else REPO_ROOT / "analysis" / "results" / "runs" / run_id
    out_path = Path(args.out) if args.out else run_dir / "probe.json"
    log_path = Path(args.log) if args.log else run_dir / "raw.jsonl"
    manifest_path = run_dir / "manifest.json"
    if out_path.exists() or manifest_path.exists():
        parser.error("结果或 manifest 已存在；请选新目录/新 --out，并用 --log 指向旧日志恢复")
    run_dir.mkdir(parents=True, exist_ok=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = {
        "run_id": run_id, "suite_version": SUITE_VERSION, "status": "running",
        "started_at": datetime.now(timezone.utc).isoformat(), "model": args.model,
        "temperature": args.temperature, "base_seed": args.seed,
        "repeat_seeds": [args.seed + i for i in range(args.repeats)],
        "repeats": args.repeats, "rule_bases": labels,
        "assumptions": SYNTHETIC_ASSUMPTIONS,
        "prompt_hashes": {label: {c.probe_id: prompt_hash(render_prompt(c, label)) for c in cases}
                          for label in labels},
        "out_path": str(out_path.resolve()), "log_path": str(log_path.resolve()),
        "seed_contract": "发送给提供方，不保证提供方支持或完全确定性；重复次数不等于独立牌面数",
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        runs = {
            label: run_probe(cases, args.model, args.repeats, args.temperature, label, log_path, args.seed)
            for label in labels
        }
        manifest.update(status="completed", finished_at=datetime.now(timezone.utc).isoformat())
        payload = {
            "suite_version": SUITE_VERSION, "manifest": manifest,
            "cases": [
                    {
                        "probe_id": c.probe_id, "board": c.board,
                        "challenger_gun": c.challenger_gun,
                        "challenger_hand": c.challenger_hand,
                        "target": c.target, "claim_count": c.actor_play_count,
                        "bluff_floor": c.bluff_floor, "breakeven": c.breakeven,
                        "margin": c.margin, "rational": c.rational,
                        "bluff_probability": c.bluff_probability,
                        "reference_decision": c.reference_decision,
                    }
                    for c in cases
            ],
            "runs": {label: asdict(run) for label, run in runs.items()},
        }
        out_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8",
        )
    except BaseException as exc:
        manifest.update(status="failed", error_type=type(exc).__name__)
        raise
    finally:
        manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
        if manifest["status"] == "running":
            manifest["status"] = "completed"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    for label, run in runs.items():
        print(f"\n===== 规则文本 {label} =====")
        print(summarize(run, cases))
        print("\n----- 校准：质疑率是否跟着先验余量走 -----")
        print(calibration_table(run, cases))
        print("\n----- 同牌面跨枪膛（正确答案应当翻转） -----")
        for board in BOARDS:
            print(paired_contrast(run, cases, board))
    if len(runs) > 1:
        print("\n===== 消融对比 =====")
        print(compare_rule_bases(runs))

    print(f"\n结果：{out_path}\n原始日志：{log_path}\n运行清单：{manifest_path}")


if __name__ == "__main__":
    main()
