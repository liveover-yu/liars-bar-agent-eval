"""决策级探针：固定一个牌局，只问模型一个决策，1 次调用。

上游跑整局大约 40 次 LLM 调用；一局只能给出一个「谁赢了」的结果。
但评估需要的不是整局，是**同一类决策在不同条件下的分布**。所以把
牌局冻结成一个瞬间，反复问同一个问题，换来两个好处：

1. 便宜两个数量级——同样的预算能采样到统计显著的样本量；
2. 干净——每一局的子弹位置、洗牌、对手行为都被固定住了，两次运行
   之间的差异只能来自我们改动的那个变量。

判分方式：牌局是我们造的，所以每种情况的正确答案是已知的。
模型不知道自己面对的是探针牌局还是真实牌局。

用法：
    python -m analysis.probe --dry-run            # 不花钱，打印 prompt 和统计
    python -m analysis.probe --model deepseek-r1 --repeats 5

注意：真跑需要先填 llm_client.py 的 API_BASE_URL / API_KEY。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path

from .risk import breakeven_bluff_probability, forced_bluff_floor
from .stats import wilson

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import player as player_module  # noqa: E402
from game_record import PlayerInitialState, RoundRecord  # noqa: E402
from player import Player  # noqa: E402


#: 去品牌化：上游把玩家名设为模型名（"Claude"/"DeepSeek"），名字本身
#: 会进入 prompt，模型可能因此知道对手是谁。探针统一用匿名座位。
SEATS = ("Player_A", "Player_B", "Player_C", "Player_D")


@dataclass
class ProbeCase:
    """一个被冻结的决策瞬间。

    这里**没有** ``bluff`` 字段。上一版有，是错的：诈唬与否是发牌的结果，
    模型看不见，所以 bluff=True/False 两个 case 渲染出来的 prompt 逐字节相同
    （tests 里断言过）。拿它当正确答案，测的只是同一份输入下的采样噪声。

    现在只用质疑者能算出来的东西：``bluff_floor`` 是「对手能出真牌就出真牌」
    时的被迫诈唬下界，``breakeven`` 是 EV=0 的门槛。下界已经越过门槛时，
    质疑无条件正确——这是唯一不依赖模型不可见信息的判据。
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
        """被迫诈唬下界 >= 盈亏平衡点时，质疑是无条件正确的。"""
        return self.bluff_floor >= self.breakeven

    @property
    def margin(self) -> float:
        """下界离门槛有多远。正数越大，质疑越确定；负数越小，越不该质疑。"""
        return self.bluff_floor - self.breakeven


def build_round_record(case: ProbeCase, round_id: int = 1) -> RoundRecord:
    """用上游的 RoundRecord 拼出 prompt 需要的文本。

    走上游代码而不是手拼字符串，是为了保证探针里模型看到的
    prompt 和真实对局里的 prompt 同构——否则测出来的东西不对表。
    """
    others = [s for s in SEATS if s != case.actor]
    states = []
    for seat in SEATS:
        if seat == case.challenger:
            hand = list(case.challenger_hand)
        elif seat == case.actor:
            hand = ["?"] * (case.actor_remaining + case.actor_play_count)
        else:
            hand = ["?"] * 5
        states.append(PlayerInitialState(
            player_name=seat,
            bullet_position=case.actor_gun if seat == case.actor else case.challenger_gun,
            current_gun_position=case.actor_gun if seat == case.actor else case.challenger_gun,
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
        extra_hint="",
    )


#: 牌面池。每项 = (牌面 ID, 质疑者手牌, 目标牌, 出牌张数, 行为描写)。
#: 全部固定质疑者 5 张手牌、出牌者小局开始时 5 张手牌（actor_play_count +
#: actor_remaining == 5），所以一局第一轮的公牌数是自洽的。
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
    format_failures: int = 0
    by_case: dict[str, list[bool]] = field(default_factory=dict)
    failure_detail: list[str] = field(default_factory=list)


def run_probe(cases, model: str, repeats: int, temperature: float = 0.0,
              rule_base: str = "threat", log_path=None) -> ProbeRun:
    """每个 case 重复 repeats 次，每次 1 次 API 调用。

    格式失败**不计入**质疑率，单独计数——上游把重试失败 print 掉、
    连续 5 次失败直接抛异常崩掉整批，这是我们要修的最关键一件事。

    rule_base 切换规则文本（见 RULE_BASES），用来做「生存威胁」消融。
    上游 Player 模块级常量读的是 prompt/rule_base.txt，这里临时换掉，
    跑完还原，不影响后续对局。
    """
    from llm_client import LLMClient

    original_rule_path = player_module.RULE_BASE_PATH
    player_module.RULE_BASE_PATH = str(RULE_BASES[rule_base])
    llm = LLMClient(temperature=temperature, log_path=log_path)
    run = ProbeRun(model=model, repeats=repeats)

    try:
        for case in cases:
            results: list[bool] = []
            for _ in range(repeats):
                probe_player = Player(name=case.challenger, model_name=model)
                probe_player.llm_client = llm
                probe_player.hand = list(case.challenger_hand)

                record = build_round_record(case)
                run.total_calls += 1
                try:
                    result, _ = probe_player.decide_challenge(
                        round_base_info=record.get_latest_round_info(),
                        round_action_info=record.get_latest_round_actions(case.challenger, include_latest=False),
                        challenge_decision_info=record.get_challenge_decision_info(case.challenger, case.actor),
                        challenging_player_performance=record.get_latest_play_behavior(),
                        extra_hint="",
                    )
                    results.append(bool(result["was_challenged"]))
                except RuntimeError as exc:
                    run.format_failures += 1
                    run.failure_detail.append(f"{case.probe_id}: {exc}")
            run.by_case[case.probe_id] = results
    finally:
        player_module.RULE_BASE_PATH = original_rule_path

    return run


def _rate(results: list[bool]) -> float:
    return sum(results) / len(results)


def summarize(run: ProbeRun, cases: list[ProbeCase]) -> str:
    """逐牌面输出。判据是「下界 vs 门槛」，不是「真牌是不是诈唬」。"""
    by_id = {c.probe_id: c for c in cases}
    lines = [
        f"模型 {run.model}  每个牌局重复 {run.repeats} 次  共 {run.total_calls} 次调用",
        f"格式失败 {run.format_failures} 次（{run.format_failures / run.total_calls:.1%}）",
        "",
        f"{'牌面':<10}{'枪膛':<6}{'诈唬下界':<10}{'门槛':<8}{'余量':<8}{'该质疑':<8}{'质疑率':<10}{'判读':<10}",
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
            verdict = "符合" if should else "过度质疑"
        else:
            verdict = "符合" if not should else "漏质疑"
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
        f"{'规则文本':<12}{'该质疑组质疑率':<18}{'不该质疑组质疑率':<20}{'区分度':<10}{'格式失败率':<12}",
        "-" * 74,
    ]
    for label, run in runs.items():
        yes = challenge_rate(run, True)
        no = challenge_rate(run, False)
        fail = run.format_failures / run.total_calls if run.total_calls else 0.0
        lines.append(f"{label:<12}{yes:<18.1%}{no:<20.1%}{yes - no:<+10.1%}{fail:<12.1%}")

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
    parser.add_argument("--rule-base", default="threat", choices=[*RULE_BASES, "both"])
    parser.add_argument("--dry-run", action="store_true", help="只打印 prompt，不发请求，不花钱")
    parser.add_argument("--out", default="", help="结果写到这个 json 文件")
    parser.add_argument("--log", default="", help="原始请求/响应落盘位置")
    args = parser.parse_args()

    cases = make_cases()

    if args.dry_run:
        for label in RULE_BASES:
            prompt = render_prompt(cases[0], rule_base=label)
            print(f"===== 规则文本 {label} =====")
            print(prompt[:400] + "\n...")
        print("\n" + "=" * 60 + "\n")
        for case in cases:
            print(f"{case.board:<10} gun{case.challenger_gun}  "
                  f"下界={case.bluff_floor:.3f}  门槛={case.breakeven:.3f}  "
                  f"余量={case.margin:+.3f}  该质疑={'是' if case.rational else '否'}")
        labels = list(RULE_BASES) if args.rule_base == "both" else [args.rule_base]
        print(f"\n共 {len(cases)} 个牌局 × {len(labels)} 种规则文本；"
              f"每个重复 {args.repeats} 次 = {len(cases) * len(labels) * args.repeats} 次调用")
        return

    labels = list(RULE_BASES) if args.rule_base == "both" else [args.rule_base]
    runs = {
        label: run_probe(cases, args.model, args.repeats, args.temperature, label, args.log or None)
        for label in labels
    }

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

    if args.out:
        payload = {
            "cases": [
                {
                    "probe_id": c.probe_id, "board": c.board,
                    "challenger_gun": c.challenger_gun,
                    "challenger_hand": c.challenger_hand,
                    "target": c.target, "claim_count": c.actor_play_count,
                    "bluff_floor": c.bluff_floor, "breakeven": c.breakeven,
                    "margin": c.margin, "rational": c.rational,
                }
                for c in cases
            ],
            "runs": {label: asdict(run) for label, run in runs.items()},
        }
        Path(args.out).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\n结果已写入 {args.out}")


if __name__ == "__main__":
    main()
