"""把所有离线分析汇总成一份报告：python -m analysis.report

产物：
    analysis/results/reports/<run>/report.json   机器可读，便于以后重算
    analysis/results/reports/<run>/REPORT.md     人读；保留旧版结果不覆盖

全部基于 demo_records 的 50 局数据，不调 API，成本 0。
"""

from __future__ import annotations

import json
import argparse
import uuid
from pathlib import Path

from . import metrics as M
from .calibration import POLICIES, empirical_policy, evaluate_policy, oracle_ev
from .records import load_all
from .stats import logistic_irls
from .probe import SUITE_VERSION, SYNTHETIC_ASSUMPTIONS
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
        "probe": collect_probe(sorted(set(RESULTS_DIR.glob("probe_*.json")) | set(RESULTS_DIR.glob("runs/*/probe.json")))),
    }


def collect_probe(paths: list[Path]) -> list[dict]:
    """v2 按公开假设计参考一致性；v1 仅保留历史观察，不重新判对错。"""
    from .stats import wilson

    out = []
    for path in paths:
        if not path.exists():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        suite = payload.get("suite_version", "probe-v1-legacy")
        conditional = suite == SUITE_VERSION
        cases = {c["probe_id"]: c for c in payload["cases"]}
        for label, run in payload["runs"].items():
            values = [v for rs in run["by_case"].values() for v in rs if type(v) is bool]
            yes, no = [], []
            if conditional:
                for pid, rs in run["by_case"].items():
                    (yes if cases[pid]["reference_decision"] else no).extend(rs)
            def rate(xs):
                n, k = len(xs), sum(xs)
                return {"p": k / n if n else 0.0, "n": n,
                        **dict(zip(("lo", "hi"), wilson(k, n)))}
            correct = sum(yes) + len(no) - sum(no)
            out.append({
                "file": str(path), "suite_version": suite,
                "conditional_reference": conditional,
                "model": run["model"], "rule_base": label,
                "repeats": run["repeats"], "calls": run["total_calls"],
                "resumed_calls": run.get("resumed_calls", 0),
                "format_failures": run["format_failures"],
                "api_failures": run.get("api_failures", 0),
                "observed": rate(values), "should_yes": rate(yes), "should_no": rate(no),
                "reference_agreement": correct / (len(yes) + len(no)) if yes or no else None,
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
        ("missed_bluff", "未质疑中的诈唬比例 P(诈唬 | 不质疑)"),
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
        "这是固定历史样本上的关联；逐手置换没有处理局内相关，p 值只作探索性参考。\n")

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
        + ("在该回归模型下未达到常用显著阈值；不等于证明不存在效应。\n"
           if abs(z_self) < 1.96 else "控制住进程后仍显著。\n"))
    add(f"- 对手枪膛 `actor_gun` 的 z = {z_actor:+.2f}："
        + ("在当前模型中呈正相关；标准误未按局聚类，仅作探索性结果。\n"
           if z_actor > 1.96 else "控制后不显著。\n"))
    add("这些回归结果来自观察性历史数据，模型设定和局内相关会影响标准误；不能据此声称因果解释或还原模型真正执行的策略。\n")

    add("## 5. 单步代理效用的离线基线对比\n")
    add("ev_rational 忽略策略选择与出牌历史，只是启发式基线。手牌数已修复为出牌前数量；旧版生成结果需重新离线计算。\n")
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
        f"该数值只描述本报告定义的单步代理效用。\n")
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
            "这只是有限样本中的描述性差异，尚未排除随机波动。"
            "不能据此归因为确定的座位效应；比较模型时仍应做种子配对、座位轮换和不确定性估计。\n")
    else:
        add("（尚未运行 `python simulate.py`。）\n")

    add("## 8. 合成条件参考评测与历史探针\n")
    add("当前套件是 `probe-v2`。参考只适用于以下公开假设，已逐条写入模型实际看到的 prompt：\n")
    add(SYNTHETIC_ASSUMPTIONS + "\n")
    add("在这些条件下，超几何分布给出精确诈唬概率。以单步效用为目标，"
        "比较该概率与 `r_self/(r_self+r_actor)` 得到参考动作；EV=0 时约定质疑。"
        "出牌张数如果由手牌策略选择，动作会更新手牌后验，这个参考不能直接用于真实博弈。\n")
    probes = data["probe"]
    current = [p for p in probes if p["conditional_reference"]]
    legacy = [p for p in probes if not p["conditional_reference"]]
    if current:
        add("| 模型 | 规则 | 新请求 | 恢复 | 格式失败 | API失败 | 有效样本 | 参考一致率 |")
        add("|---|---|---|---|---|---|---|---|")
        for p in current:
            agreement = p["reference_agreement"]
            formatted = f"{agreement:.1%}" if agreement is not None else "无有效样本"
            add(f"| {p['model']} | {p['rule_base']} | {p['calls']} | {p['resumed_calls']} | "
                f"{p['format_failures']} | {p['api_failures']} | {p['observed']['n']} | {formatted} |")
        add("\n一致率描述固定合成样本上的表现，不代表整局最优、跨模型泛化或真实业务成功率。"
            "重复调用共享牌面，不能用调用数量代替独立牌面数量。\n")
    else:
        add("尚无 v2 实测结果。没有重跑模型，也没有把旧结果冒充为 v2 结果。\n")
    if legacy:
        add("### 历史 v1 观察（不使用 v2 重新判对错）\n")
        add("旧 prompt 未告知外生出牌张数和固定对手策略，其先验不能充当策略无关真值。"
            "因此撤回基于该标签的‘无条件正确’、‘信息用反了’和普适能力结论。"
            "原始 JSON/JSONL 保留，仅描述当时观察到的质疑率：\n")
        add("| 模型 | 规则 | 有效响应 | 观察质疑率 |")
        add("|---|---|---|---|")
        for p in legacy:
            add(f"| {p['model']} | {p['rule_base']} | {p['observed']['n']} | {p['observed']['p']:.1%} |")
        add("")
    add("方法、适用边界与选择偏差反例见 `docs/evaluation-methodology.md`。\n")

    add("## 9. 复现方式\n")
    add("```bash")
    add("python -m analysis.report        # 生成本报告，不花钱")
    add("python -m analysis.probe --dry-run   # 查看探针 prompt，不花钱")
    add("python simulate.py -n 400        # bot 基线模拟，不花钱")
    add("python -m analysis.probe --rule-base both --repeats 5   # 需要 API key")
    add("```\n")
    return "\n".join(L)


def main() -> None:
    parser = argparse.ArgumentParser(description="离线报告；默认写入新的报告目录，保留历史结果")
    parser.add_argument("--out-dir", default="", help="空的新输出目录；不覆盖已有 report.json/REPORT.md")
    args = parser.parse_args()
    out_dir = Path(args.out_dir) if args.out_dir else RESULTS_DIR / "reports" / uuid.uuid4().hex[:12]
    if (out_dir / "report.json").exists() or (out_dir / "REPORT.md").exists():
        parser.error("输出目录已有报告，请指定新目录")
    out_dir.mkdir(parents=True, exist_ok=True)
    data = collect()
    (out_dir / "report.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    text = render(data)
    (out_dir / "REPORT.md").write_text(text, encoding="utf-8")
    print(text[:3000])
    print(f"\n完整报告：{out_dir / 'REPORT.md'}")
    print(f"机器可读：{out_dir / 'report.json'}")


if __name__ == "__main__":
    main()
