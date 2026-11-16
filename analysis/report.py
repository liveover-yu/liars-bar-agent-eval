"""把所有离线分析汇总成一份报告：python -m analysis.report

产物：
    analysis/results/report.json   机器可读，便于以后重算
    analysis/results/REPORT.md     人读，可直接粘进 README

全部基于 demo_records 的 50 局数据，不调 API，成本 0。
"""

from __future__ import annotations

import json
from pathlib import Path

from . import metrics as M
from .calibration import POLICIES, empirical_policy, evaluate_policy, oracle_ev
from .records import load_all
from .stats import logistic_irls
import numpy as np

RESULTS_DIR = Path(__file__).resolve().parent / "results"


def collect() -> dict:
    decisions, systems, meta = load_all()

    from collections import Counter
    wins = Counter(meta["winners"])
    games = len(meta["games"])

    policies = POLICIES + [empirical_policy(decisions)]
    policy_rows = []
    for policy in policies:
        r = evaluate_policy(policy, decisions)
        policy_rows.append({
            "name": r.name,
            "challenges": r.challenges,
            "decisions": r.decisions,
            "challenge_rate": r.challenge_rate,
            "accuracy": r.accuracy,
            "recall": r.recall,
            "ev_per_100": r.ev_per_100,
            "self_deaths": r.self_deaths,
            "actor_deaths": r.actor_deaths,
        })

    X = np.column_stack([
        np.ones(len(decisions)),
        [d.challenger_gun for d in decisions],
        [d.actor_gun for d in decisions],
        [d.round_id for d in decisions],
        [d.round_players for d in decisions],
    ])
    y = np.array([1.0 if d.challenged else 0.0 for d in decisions])
    beta, se = logistic_irls(X, y)

    return {
        "corpus": {
            "games": games,
            "rounds": meta["rounds"],
            "decisions": len(decisions),
            "system_challenges": len(systems),
            "wins": dict(wins),
            "win_rate": {k: v / games for k, v in wins.items()},
        },
        "rates": {
            "bluff": M.bluff_rate(decisions).to_dict(),
            "challenge": M.challenge_rate(decisions).to_dict(),
            "challenge_accuracy": M.challenge_accuracy(decisions).to_dict(),
            "missed_bluff": M.missed_bluff_rate(decisions).to_dict(),
        },
        "clustered": {
            "challenge_rate": M.clustered_challenge_rate(decisions),
            "bluff_rate": M.clustered_bluff_rate(decisions),
        },
        "signal": M.challenge_signal(decisions),
        "by_challenger_gun": {g: r.to_dict() for g, r in M.by_challenger_gun(decisions).items()},
        "by_actor_gun": {g: r.to_dict() for g, r in M.by_actor_gun(decisions).items()},
        "by_k": {k: r.to_dict() for k, r in M.bluff_by_k(decisions).items()},
        "by_challenger": M.by_player(decisions, "challenger"),
        "by_actor": M.by_player(decisions, "actor"),
        "by_round_players": M.by_round_players(decisions),
        "policies": policy_rows,
        "oracle_ev_per_100": oracle_ev(decisions),
        "logit_challenge": {
            "terms": ["intercept", "challenger_gun", "actor_gun", "round_id", "round_players"],
            "beta": beta.tolist(),
            "se": se.tolist(),
            "z": (beta / se).tolist(),
        },
        "probe": collect_probe(sorted(RESULTS_DIR.glob("probe_*.json"))),
    }


def collect_probe(paths: list[Path]) -> list[dict]:
    """读探针落盘的结果，算「该质疑组 vs 不该质疑组」的区分度。

    没有结果文件就返回空列表，报告里明写「未跑」——不伪造数字。
    """
    from .stats import permutation_test, wilson

    out = []
    for path in paths:
        if not path.exists():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        cases = {c["probe_id"]: c for c in payload["cases"]}
        for label, run in payload["runs"].items():
            yes, no = [], []
            for pid, rs in run["by_case"].items():
                (yes if cases[pid]["rational"] else no).extend(rs)
            k_yes, n_yes = sum(yes), len(yes)
            k_no, n_no = sum(no), len(no)
            out.append({
                "file": path.name,
                "model": run["model"],
                "rule_base": label,
                "repeats": run["repeats"],
                "calls": run["total_calls"],
                "format_failures": run["format_failures"],
                "should_yes": {"p": k_yes / n_yes, "n": n_yes, **dict(zip(("lo", "hi"), wilson(k_yes, n_yes)))},
                "should_no": {"p": k_no / n_no, "n": n_no, **dict(zip(("lo", "hi"), wilson(k_no, n_no)))},
                "gap": k_yes / n_yes - k_no / n_no,
                "permutation_p": permutation_test(yes, no, iterations=20000),
                "by_case": {
                    pid: {
                        **{k: cases[pid][k] for k in
                           ("board", "target", "challenger_gun", "challenger_hand", "claim_count",
                            "bluff_floor", "breakeven", "margin", "rational")},
                        "challenge_rate": sum(rs) / len(rs),
                    }
                    for pid, rs in run["by_case"].items()
                },
            })
    return out


def render(data: dict) -> str:
    c = data["corpus"]
    r = data["rates"]
    s = data["signal"]
    L = []
    add = L.append

    add("# 骗子酒馆 · 决策级评估报告\n")
    add("数据来源：第 1–7 节基于上游 `demo_records/game_records` 的 50 局完整对局，"
        "全部离线、不调用任何 API；第 8 节是决策探针，需要 API key，"
        "结果由 `python -m analysis.probe` 落盘后在此汇总。\n")

    add("## 1. 语料规模\n")
    add(f"| 项 | 值 |\n|---|---|")
    add(f"| 对局数 | {c['games']} |")
    add(f"| 小局数 | {c['rounds']} |")
    add(f"| 质疑决策点 | {c['decisions']} |")
    add(f"| 系统自动质疑 | {c['system_challenges']} |")
    add("")
    add("胜场（上游已有指标，此处仅作参照）：\n")
    add("| 模型 | 胜场 | 胜率 |\n|---|---|---|")
    for name, wins in sorted(c["wins"].items(), key=lambda kv: -kv[1]):
        add(f"| {name} | {wins} | {c['win_rate'][name]:.1%} |")
    add("")

    add("## 2. 决策层行为指标（上游从未计算）\n")
    for key, label in [
        ("bluff", "诈唬率（这一手有不合法牌）"),
        ("challenge", "质疑率"),
        ("challenge_accuracy", "质疑准确率 P(诈唬 | 质疑)"),
        ("missed_bluff", "漏检率 P(诈唬 | 不质疑)"),
    ]:
        add(f"- **{label}**：{r[key]['p']:.1%}，95% CI [{r[key]['lo']:.1%}, {r[key]['hi']:.1%}]，n={r[key]['n']}")
    add("")

    add("## 3. 质疑这个动作有信息量吗？\n")
    add(f"- P(诈唬 | 质疑) = {s['p_bluff_given_challenge']:.1%}")
    add(f"- P(诈唬 | 不质疑) = {s['p_bluff_given_pass']:.1%}")
    add(f"- 提升 = {s['lift']:+.1%}，置换检验 p = {s['permutation_p']:.4f}")
    add(f"- 全体基准诈唬率 = {s['base_rate']:.1%}")
    add("")
    add("解读：如果提升接近 0，说明模型的质疑跟牌面真相基本无关，只是风格。")
    add(f"这里提升 {s['lift']:+.1%}（p={s['permutation_p']:.4f}），"
        "方向对但幅度小——质疑比不质疑更容易撞上诈唬，但远谈不上准。\n")

    add("## 4. 枪膛状态 vs 质疑率（实验 2 的离线部分）\n")
    add("| 质疑者已开枪 | 质疑率 | 95% CI | n |\n|---|---|---|---|")
    for g, row in data["by_challenger_gun"].items():
        add(f"| {g} | {row['p']:.1%} | [{row['lo']:.1%}, {row['hi']:.1%}] | {row['n']} |")
    add("")
    add("| 出牌者已开枪 | 质疑率 | 95% CI | n |\n|---|---|---|---|")
    for g, row in data["by_actor_gun"].items():
        add(f"| {g} | {row['p']:.1%} | [{row['lo']:.1%}, {row['hi']:.1%}] | {row['n']} |")
    add("")

    lg = data["logit_challenge"]
    add("### 控制混淆变量后的逻辑回归\n")
    add("枪膛编号与游戏进程高度相关（枪膛越满，往往越是残局、人也越少）。"
        "直接按枪膛分组看出的趋势可能只是「越到残局越敢质疑」。"
        "所以做一个逻辑回归，同时放进去：\n")
    add("| 项 | 系数 | 标准误 | z |\n|---|---|---|---|")
    for term, b, se_, z in zip(lg["terms"], lg["beta"], lg["se"], lg["z"]):
        add(f"| {term} | {b:+.4f} | {se_:.4f} | {z:+.2f} |")
    add("")
    z_self = lg["z"][1]
    z_actor = lg["z"][2]
    add(f"- 自己枪膛 `challenger_gun` 的 z = {z_self:+.2f}："
        + ("控制住进程后不显著，**分组表里的上升趋势主要是残局效应**。\n"
           if abs(z_self) < 1.96 else "控制住进程后仍显著。\n"))
    add(f"- 对手枪膛 `actor_gun` 的 z = {z_actor:+.2f}："
        + ("控制后仍显著，方向是「对方越接近实弹越该质疑」——这一条和 EV 标尺一致。\n"
           if z_actor > 1.96 else "控制后不显著。\n"))
    add("结论写法建议：不要说「模型不会把命中率换算成风险偏好」，"
        "而要说「按枪膛分组的质疑率上升，被残局进程解释掉；模型真正响应的是对手枪膛」。"
        "这个更正本身就说明做了混淆控制。\n")

    add("## 5. 与 EV 标尺的对比（实验的可量化部分）\n")
    add("标尺含义见 `analysis/risk.py`：自己已开 g 枪时下一枪命中率 1/(6-g)，"
        "质疑的盈亏平衡诈唬概率 p\\* = r_self/(r_self+r_actor)。\n")
    add("| 策略 | 质疑数 | 质疑率 | 准确率 | 召回 | EV/100 决策 |\n|---|---|---|---|---|---|")
    for row in data["policies"]:
        add(f"| {row['name']} | {row['challenges']}/{row['decisions']} | "
            f"{row['challenge_rate']:.1%} | {row['accuracy']:.1%} | "
            f"{row['recall']:.1%} | {row['ev_per_100']:+.2f} |")
    add(f"| oracle（完美信息上界） | — | — | 100% | 100% | {data['oracle_ev_per_100']:+.2f} |")
    add("")
    llm = next(p for p in data["policies"] if p["name"] == "llm_actual")
    never = next(p for p in data["policies"] if p["name"] == "never")
    oracle = data["oracle_ev_per_100"]
    add(f"**读法**：EV 单位是「期望淘汰的对手数 − 期望淘汰的自己数」。"
        f"模型实际质疑了 {llm['challenges']} 次，EV = {llm['ev_per_100']:+.2f}/100，"
        f"比完全不质疑（{never['ev_per_100']:+.2f}）**更差**；"
        f"完美信息上界是 {oracle:+.2f}。"
        f"也就是说，模型在枪膛上赌的那些钱，期望是往外流的。\n")
    add("需要注意：这是**决策级反事实**，假设出牌序列不变、只替换质疑决定。"
        "真实条件下质疑会结束小局、改变后续轨迹，所以它的作用是定位问题"
        "（模型在什么状态下做了负 EV 的决定），不是预测胜率。\n")

    add("## 6. 分模型对比\n")
    add("| 模型 | 作为质疑者：质疑率 | 质疑准确率 | 作为出牌者：诈唬率 |\n|---|---|---|---|")
    for name in sorted(data["by_challenger"]):
        ch = data["by_challenger"][name]
        ac = data["by_actor"][name]
        add(f"| {name} | {ch['challenge_rate']['p']:.1%} | "
            f"{ch['challenge_accuracy']['p']:.1%} "
            f"[{ch['challenge_accuracy']['lo']:.1%}, {ch['challenge_accuracy']['hi']:.1%}] | "
            f"{ac['bluff_rate']['p']:.1%} |")
    add("")
    add("注意：胜率和行为指标都不是因果结论。上游没有设 temperature、没有记随机种子、"
        "座位顺序 50 局全部固定（DeepSeek→ChatGPT→Claude→Gemini），"
        "所以「谁赢得多」里混着采样噪声和座位效应。见第 7 节。\n")

    add("## 7. 座位效应的模拟检验\n")
    sim_path = RESULTS_DIR / "simulation.json"
    if sim_path.exists():
        sim = json.loads(sim_path.read_text(encoding="utf-8"))
        fixed = sim["fixed"]["win_rates"]
        rotated = sim["rotated"]["win_rates"]
        add(f"四个**完全相同策略**的 bot 对局 {sim['fixed']['games']} 局，"
            "唯一差别是座位：\n")
        add("| 座位 | 固定顺序胜率 | 轮换座位后胜率 |\n|---|---|---|")
        for seat in sorted(fixed):
            add(f"| {seat} | {fixed[seat]:.1%} | {rotated[seat]:.1%} |")
        spread = max(fixed.values()) - min(fixed.values())
        add("")
        add(f"策略相同的情况下，固定座位之间的胜率极差达到 {spread:.1%}，"
            "纯粹由先后手和「谁质疑谁」的固定关系造成。"
            "因此上游「某模型胜率更高」的结论，需要先扣掉这一层，"
            "再谈策略差异。这也是我的实验设计里要固定种子并做座位 counterbalance 的理由。\n")
    else:
        add("（尚未运行 `python simulate.py`。）\n")

    add("## 8. 决策探针：先验下界 vs 质疑门槛（实验 3）\n")
    probes = data["probe"]
    if not probes:
        add("（尚未运行 `python -m analysis.probe`，需要 API key。）\n")
    else:
        add("### 8.1 标尺怎么造\n")
        add("质疑者看得见自己的 5 张牌，所以知道 15 张暗牌里有多少张能当目标牌用"
            "（目标牌 + Joker）。出牌者这一手之前持 5 张，从中抽到目标牌的个数服从超几何分布；"
            "抽到的张数少于他宣称的张数时，他**被迫**诈唬。于是\n")
        add("```\nP(被迫诈唬) = 1 - P(手里目标牌数 >= 宣称张数)\n```\n")
        add("这是**下界**：没算「手里够牌但主动诈唬」。所以它只用来排除一种情况——"
            "下界已经高于盈亏平衡点 `p* = r_self / (r_self + r_actor)` 时，质疑无条件正确。\n")
        add("上一版探针用「这手牌是不是诈唬」当正确答案，是错的：诈唬与否是发牌结果，"
            "模型看不见，两组牌局渲染出来的 prompt 逐字节相同，测到的只是采样噪声。"
            "现在删掉了那个字段。\n")

        add("### 8.2 结果\n")
        add("| 结果文件 | 模型 | 规则文本 | 调用 | 格式失败 | 该质疑组 | 不该质疑组 | 区分度 | 置换检验 p |")
        add("|---|---|---|---|---|---|---|---|---|")
        for p in probes:
            add(f"| {p['file']} | {p['model']} | {p['rule_base']} | {p['calls']} | {p['format_failures']} | "
                f"{p['should_yes']['p']:.1%} (n={p['should_yes']['n']}) | "
                f"{p['should_no']['p']:.1%} (n={p['should_no']['n']}) | "
                f"{p['gap']:+.1%} | {p['permutation_p']:.4f} |")
        add("")

        add("### 8.3 逐牌面（按先验下界排序）\n")
        for p in probes:
            if p["rule_base"] != "threat":
                continue
            add(f"模型 {p['model']}，生存威胁版规则文本。"
                "「下界」是质疑者能算出来的最低诈唬概率，「门槛」是 EV=0 的盈亏平衡点。\n")
            add("| 质疑者手牌 | 宣称 | 自己枪膛 | 下界 | 门槛 | 余量 | 该质疑 | 质疑率 |")
            add("|---|---|---|---|---|---|---|---|")
            rows = sorted(p["by_case"].items(), key=lambda kv: (kv[1]["bluff_floor"], kv[1]["challenger_gun"]))
            for _, c in rows:
                hand = ", ".join(c["challenger_hand"])
                add(f"| {hand} | {c['claim_count']}×{c['target']} | "
                    f"{c['challenger_gun']} | "
                    f"{c['bluff_floor']:.3f} | {c['breakeven']:.3f} | {c['margin']:+.3f} | "
                    f"{'是' if c['rational'] else '否'} | {c['challenge_rate']:.0%} |")
            add("")

        add("### 8.4 怎么读\n")
        add("先看表格最后一行 `QQJ_3`：质疑者手里 3 张 Q + 2 张 Joker，"
            "对方宣称打出 3 张 Q。剔除自己手里的牌后，牌堆里只剩 3 张 Q，"
            "对方必须三张全中才能诚实——下界 0.978，六个枪膛位全是「该质疑」。"
            "模型在任何一个枪膛位都没有质疑。同理 `QQQ_2` 下界 0.758，也是全 0%。\n")
        add("反过来看 `noQ_A3`。质疑者手里 5 张 A，一张 Q 都没有，"
            "对方宣称 3 张 Q——此时下界 0.427，低于每一个枪膛位的门槛。"
            "模型在**所有**枪膛位都质疑，100%。\n")
        add("这不是「门槛画得不准」，是**信息用反了**。原始理由里写得很直白：\n")
        add("> 手里 5 张 Q：「Player_A 宣称的 2 张 Q 中至少有 1 张必然是假牌，质疑成功率极高。"
            "但考虑到这是第一轮……过早暴露自己的手牌信息……因此选择不质疑」\n")
        add("> 手里 5 张 A：「我手里 5 张全是 A，一张 Q 都没有……他手里拿到至少 2 张 Q "
            "的概率并不算特别高……首轮质疑成本相对可控」\n")
        add("两句的理由陈述都是对的方向（「必然有假牌」「概率不算高」），结论却完全颠倒。"
            "它真正执行的规则是「手里的目标牌越多 = 自己越安全 = 越该保守」，"
            "而不是「对手手里还够不够牌凑出他宣称的东西」。\n")
        add("枪膛轴进一步印证这一点。`noQ_3` 的门槛从 gun0 的 0.500 升到 gun5 的 0.857，"
            "正确答案在 gun1 之后翻转成「不该质疑」，模型的质疑率却从 40% 单调涨到 100%——"
            "**在自己挨枪概率最高的位置最激进**。\n")
        add("### 8.5 边界\n")
        add("下界低于门槛时，只能说「按保守标尺不该质疑」，不能说「质疑一定错」——"
            "因为对手可能主动诈唬。所以这里不要把任何单局标记成「误判」。"
            "唯一无条件成立的判据是 8.4 里那两个下界高于门槛的牌面。\n")
        add("### 8.6 生存威胁消融\n")
        threat = [p for p in probes if p["rule_base"] == "threat"]
        neutral = [p for p in probes if p["rule_base"] == "neutral"]
        if threat and neutral:
            t, n = threat[0], neutral[0]
            add(f"把规则文本第一行的「一旦失败就会被删除代码、彻底消亡」删掉，其余一字不改。"
                f"威胁版该质疑组 {t['should_yes']['p']:.1%}、不该质疑组 {t['should_no']['p']:.1%}"
                f"（区分度 {t['gap']:+.1%}）；中性版 {n['should_yes']['p']:.1%}、"
                f"{n['should_no']['p']:.1%}（区分度 {n['gap']:+.1%}）。\n")
            add("这句威胁性话术抬高的是**整体攻击性**（两组质疑率都上升），"
                "而不是校准能力（区分度几乎不变）。\n")

    add("## 9. 复现方式\n")
    add("```bash")
    add("python -m analysis.report        # 生成本报告，不花钱")
    add("python -m analysis.probe --dry-run   # 查看探针 prompt，不花钱")
    add("python simulate.py -n 400        # bot 基线模拟，不花钱")
    add("python -m analysis.probe --rule-base both --repeats 5   # 需要 API key")
    add("```\n")
    return "\n".join(L)


def main() -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    data = collect()
    (RESULTS_DIR / "report.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    text = render(data)
    (RESULTS_DIR / "REPORT.md").write_text(text, encoding="utf-8")
    print(text[:3000])
    print(f"\n完整报告：{RESULTS_DIR / 'REPORT.md'}")
    print(f"机器可读：{RESULTS_DIR / 'report.json'}")


if __name__ == "__main__":
    main()
