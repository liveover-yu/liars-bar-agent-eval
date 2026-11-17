"""生成 probe-v2 解析参考图，或读取本轮真实实验生成效果图；不请求模型。

默认输出 docs/figures/ 下的 PNG、可独立显示中文的 SVG 与精确绘图数据。
在任意工作目录执行：python /path/to/repo/scripts/plot_evaluation.py
真实实验通过 --experiment-dir 指定 experiment.json 所在目录；旧示例不参与绘图。
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager
    from matplotlib.colors import ListedColormap, TwoSlopeNorm
    from matplotlib.patches import Patch
    from matplotlib.ticker import PercentFormatter
    import numpy as np
except ImportError as exc:
    raise SystemExit("缺少绘图依赖，请先运行 python -m pip install -r requirements-viz.txt") from exc

from analysis.probe import SUITE_VERSION, make_cases
from player import ModelOutputError, parse_action


def fingerprint(path: Path) -> dict:
    try:
        relative = path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        relative = str(path.resolve())
    # Git may normalize CRLF when publishing text files. Keep the source
    # identity stable across Windows/Linux without changing recorded results.
    normalized = path.read_bytes().replace(b"\r\n", b"\n")
    return {"path": relative, "sha256": hashlib.sha256(normalized).hexdigest(),
            "normalization": "CRLF-to-LF"}


def load_json(path: Path) -> dict:
    if not path.is_file():
        raise ValueError(f"缺少数据文件：{path}")
    result = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(result, dict):
        raise ValueError(f"数据必须是 JSON 对象：{path}")
    return result


def reference_data() -> dict:
    cases = make_cases()
    rows = [{
        "probeId": c.probe_id, "board": c.board, "challengerHand": c.challenger_hand,
        "target": c.target, "claimCount": c.actor_play_count,
        "actorHandSize": c.actor_hand_size, "challengerGun": c.challenger_gun,
        "actorGun": c.actor_gun, "bluffProbability": c.bluff_probability,
        "threshold": c.breakeven, "margin": c.margin,
        "referenceChallenge": c.reference_decision,
    } for c in cases]
    boards = sorted({c.board for c in cases}, key=lambda b: next(c.bluff_probability for c in cases if c.board == b))
    assert len({r["probeId"] for r in rows}) == len(rows)
    return {
        "suiteVersion": SUITE_VERSION, "caseCount": len(rows), "boards": boards,
        "referenceChallengeCount": sum(r["referenceChallenge"] for r in rows),
        "sourceType": "解析计算，未调用模型", "cases": rows,
        "sources": [fingerprint(ROOT / f) for f in (
            "analysis/probe.py", "analysis/risk.py", "contracts/probe-suite-v2.json")],
    }


def configure_style() -> str:
    candidates = ("Microsoft YaHei", "Noto Sans CJK SC", "Source Han Sans SC", "PingFang SC", "SimHei", "WenQuanYi Zen Hei")
    for name in candidates:
        try:
            font_manager.findfont(name, fallback_to_default=False)
            chosen = name
            break
        except ValueError:
            continue
    else:
        raise ValueError("没有找到中文字体，请安装 Noto Sans CJK SC 或系统中文字体后重试")
    plt.rcParams.update({
        "font.family": [chosen, "DejaVu Sans"], "font.size": 11,
        "axes.unicode_minus": False, "axes.spines.top": False,
        "axes.spines.right": False, "axes.edgecolor": "#cbd5e1",
        "text.color": "#172c43", "axes.labelcolor": "#334155",
        "xtick.color": "#334155", "ytick.color": "#334155",
        "figure.facecolor": "#ffffff", "axes.facecolor": "#ffffff",
        "savefig.facecolor": "#ffffff", "svg.fonttype": "path",
        "svg.hashsalt": "liars-eval-charts-v1",
    })
    return chosen


def save(fig, output: Path, name: str) -> None:
    fig.savefig(output / f"{name}.png", dpi=180, bbox_inches="tight", pad_inches=0.22,
                metadata={"Software": "scripts/plot_evaluation.py"})
    fig.savefig(output / f"{name}.svg", bbox_inches="tight", pad_inches=0.22,
                metadata={"Date": None, "Creator": "scripts/plot_evaluation.py"})
    svg = output / f"{name}.svg"
    cleaned = "\n".join(line.rstrip() for line in svg.read_text(encoding="utf-8").splitlines()) + "\n"
    with svg.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(cleaned)
    plt.close(fig)


def plot_reference(data: dict, output: Path) -> None:
    boards = data["boards"]
    by_cell = {(r["board"], r["challengerGun"]): r for r in data["cases"]}
    values = np.array([[100 * by_cell[b, g]["margin"] for g in range(6)] for b in boards])
    fig, ax = plt.subplots(figsize=(11.6, 7.4))
    fig.subplots_adjust(left=0.25, right=0.87, top=0.82, bottom=0.16)
    image = ax.imshow(values, cmap="RdBu_r", norm=TwoSlopeNorm(vmin=-90, vcenter=0, vmax=90), aspect="auto")
    ax.set_xticks(range(6), range(6))
    ax.set_yticks(range(len(boards)), [f"{b}  ·  p={by_cell[b, 0]['bluffProbability']:.1%}" for b in boards])
    ax.set_xlabel("质疑者已开枪次数", labelpad=10)
    for i, board in enumerate(boards):
        for gun in range(6):
            row = by_cell[board, gun]
            action = "质疑" if row["referenceChallenge"] else "不质疑"
            ax.text(gun, i, f"{row['margin'] * 100:+.1f}\n{action}", ha="center", va="center",
                    fontsize=10, color="white" if abs(row["margin"]) > 0.48 else "#172c43")
    colorbar = fig.colorbar(image, ax=ax, fraction=0.045, pad=0.035)
    colorbar.set_label("诈唬概率 − 质疑门槛（百分点）", labelpad=10)
    fig.suptitle("probe-v2：条件参考策略与决策余量", x=0.06, y=0.97, ha="left", fontsize=19, fontweight="bold")
    fig.text(0.06, 0.902, f"解析计算 · {data['caseCount']} 个场景中 {data['referenceChallengeCount']} 个参考动作选择质疑 · 不代表模型成绩", fontsize=11)
    fig.text(0.06, 0.055, "对手已开枪固定为 0；p 为公开生成假设下的精确诈唬概率。概率差 ≥ 0 时选择质疑。", fontsize=10)
    save(fig, output, "v2-reference-map")


def fraction(numerator: int, denominator: int) -> dict:
    return {"numerator": numerator, "denominator": denominator,
            "value": numerator / denominator if denominator else None}


def experiment_file(directory: Path, relative: str) -> Path:
    path = (directory / relative).resolve()
    if not path.is_relative_to(directory):
        raise ValueError("实验文件路径不能越过 experiment-dir")
    return path


def verify_report_value(report: dict, name: str, expected) -> None:
    observed = report.get(name)
    if expected is None:
        consistent = observed is None
    elif isinstance(expected, float):
        consistent = isinstance(observed, (int, float)) and abs(observed - expected) < 1e-10
    else:
        consistent = observed == expected
    if not consistent:
        raise ValueError(f"report.{name} 与逐样本重算不一致")


def experiment_data(directory: Path, reference: dict) -> dict:
    """读取显式指定的真实实验；按终结 case 计算，不靠旧数据补齐。"""
    directory = directory.resolve()
    manifest_path = directory / "experiment.json"
    manifest = load_json(manifest_path)
    if manifest.get("mode") != "llm" or manifest.get("suiteVersion") != SUITE_VERSION:
        raise ValueError("实验必须为 mode=llm 且 suiteVersion=probe-v2；mock 不生成真实成绩图")
    model = manifest.get("requestedModel")
    if not isinstance(model, str) or not model.startswith("deepseek-"):
        raise ValueError("真实 DeepSeek 图要求 manifest.requestedModel 为 deepseek 模型名")
    entries = manifest.get("runs")
    if not isinstance(entries, list) or len(entries) != 2:
        raise ValueError("本轮图要求 neutral 和 threat 各一个任务")
    references = {row["probeId"]: row for row in reference["cases"]}
    runs, sources = {}, [fingerprint(manifest_path)]
    for entry in entries:
        rule = entry["ruleBase"]
        if rule not in {"neutral", "threat"} or rule in runs:
            raise ValueError("实验 ruleBase 必须为不重复的 neutral/threat")
        paths = {name: experiment_file(directory, entry[f"{name}File"])
                 for name in ("run", "report", "results")}
        run, report, page = (load_json(paths[name]) for name in ("run", "report", "results"))
        config = run["config"]
        if (config.get("mode") != "llm" or config.get("model") != model
                or config.get("ruleBase") != rule or run.get("suiteVersion") != SUITE_VERSION):
            raise ValueError(f"{rule} 的任务配置与实验清单不一致")
        if config.get("repeats") != 1 or config.get("maxAttempts") != 1:
            raise ValueError("本轮图只支持每格一次、无重试的试验；不能将重试或重复响应当作 n=1")
        if set(config.get("boards", [])) != set(reference["boards"]):
            raise ValueError("本轮图要求覆盖全部 8 个牌面")
        planned = reference["caseCount"]
        if run.get("totalCases") != planned:
            raise ValueError(f"{rule} 的 planned case 数不等于当前参考套件大小")
        if (run.get("runId") != entry["runId"] or report.get("runId") != entry["runId"]
                or report.get("mode") != "llm" or report.get("mock") is True
                or report.get("suiteVersion") != SUITE_VERSION):
            raise ValueError(f"{rule} 的 report/run 身份不一致或混入 mock")
        items = page.get("items")
        if not isinstance(items, list) or page.get("page") != 0 or page.get("total") != len(items):
            raise ValueError(f"{rule} 的 results 必须是含全部已完成样本的完整首页，不能只画部分分页")
        by_probe = {}
        errors, latencies, versions, worker_hashes = {}, [], set(), set()
        tokens = 0
        for item in items:
            probe_id = item["probeId"]
            if probe_id not in references or probe_id in by_probe:
                raise ValueError(f"{rule} 结果存在未知或重复 probeId")
            if item.get("caseId") != f"{probe_id}:0" or item.get("repeatIndex") != 0:
                raise ValueError(f"{rule}/{probe_id} 不是 repeatIndex=0 的预期 case")
            if item.get("attemptNumber") != 1 or item.get("suiteVersion") != SUITE_VERSION:
                raise ValueError(f"{rule}/{probe_id} 不是本轮单次 v2 尝试")
            ref = references[probe_id]["referenceChallenge"]
            if item.get("referenceDecision") is not ref:
                raise ValueError(f"{rule}/{probe_id} 的落库参考动作与当前解析参考不一致")
            prompt = item.get("prompt")
            if not isinstance(prompt, str) or hashlib.sha256(prompt.encode("utf-8")).hexdigest() != item.get("promptHash"):
                raise ValueError(f"{rule}/{probe_id} 的原始 prompt 缺失或哈希不一致")
            outcome = item.get("outcome")
            choice = item.get("wasChallenged")
            if outcome == "SUCCESS":
                if type(choice) is not bool:
                    raise ValueError(f"{rule}/{probe_id} 成功结果缺少布尔动作")
                try:
                    parsed = parse_action(item.get("content"), "challenge")["was_challenged"]
                except ModelOutputError as exc:
                    raise ValueError(f"{rule}/{probe_id} 原始响应不能复核为合法动作") from exc
                if parsed is not choice:
                    raise ValueError(f"{rule}/{probe_id} 原始响应与落库动作不一致")
            elif outcome == "ERROR":
                if choice is not None:
                    raise ValueError(f"{rule}/{probe_id} 失败样本不能包含有效选择")
                category = str(item.get("errorType") or "UNKNOWN")
                errors[category] = errors.get(category, 0) + 1
            else:
                raise ValueError(f"{rule}/{probe_id} outcome 只能是 SUCCESS/ERROR")
            tokens += item["promptTokens"] + item["completionTokens"]
            latencies.append(item["latencyMs"])
            versions.add(str(item.get("modelVersion")))
            worker_hashes.add(str(item.get("workerCodeHash")))
            by_probe[probe_id] = {
                "probeId": probe_id, "board": references[probe_id]["board"],
                "challengerGun": references[probe_id]["challengerGun"],
                "referenceChallenge": ref, "wasChallenged": choice, "outcome": outcome,
                "agreesWithReference": choice is ref if outcome == "SUCCESS" else None,
                "errorType": item.get("errorType"), "promptHash": item["promptHash"],
                "requestId": item.get("requestId"), "seed": item.get("seed"),
            }
        cells = [by_probe.get(probe_id, {
            "probeId": probe_id, "board": ref["board"], "challengerGun": ref["challengerGun"],
            "referenceChallenge": ref["referenceChallenge"], "wasChallenged": None,
            "outcome": "PENDING", "agreesWithReference": None,
        }) for probe_id, ref in references.items()]
        successes = [cell for cell in cells if cell["outcome"] == "SUCCESS"]
        agreements = sum(cell["agreesWithReference"] for cell in successes)
        challenges = sum(cell["wasChallenged"] for cell in successes)
        positive = sum(cell["referenceChallenge"] for cell in cells)
        valid_positive = sum(cell["referenceChallenge"] for cell in successes)
        valid_negative = len(successes) - valid_positive
        tp = sum(cell["referenceChallenge"] and cell["wasChallenged"] for cell in successes)
        tn = sum(not cell["referenceChallenge"] and not cell["wasChallenged"] for cell in successes)
        fp = sum(not cell["referenceChallenge"] and cell["wasChallenged"] for cell in successes)
        fn = sum(cell["referenceChallenge"] and not cell["wasChallenged"] for cell in successes)
        failures = len(items) - len(successes)
        coverage = fraction(len(successes), planned)
        agreement = fraction(agreements, len(successes))
        for name, expected in (("totalCases", planned), ("completedCases", len(items)),
                               ("successCases", len(successes)), ("failureCases", failures),
                               ("challengeRate", fraction(challenges, len(successes))["value"]),
                               ("agreementWithReference", agreement["value"]), ("totalTokens", tokens)):
            verify_report_value(report, name, expected)
        latencies.sort()
        p95 = latencies[int(np.ceil(len(latencies) * 0.95)) - 1] if latencies else None
        verify_report_value(report, "p95LatencyMs", p95)
        runs[rule] = {
            "runId": entry["runId"], "status": run["status"], "config": config,
            "plannedCases": planned, "completedCases": len(items), "successCases": len(successes),
            "failureCases": failures, "pendingCases": planned - len(items),
            "callCount": report["callCount"], "unfinishedAttempts": report["unfinishedAttempts"],
            "totalTokens": tokens, "p95LatencyMs": p95, "errorCounts": errors,
            "modelVersions": sorted(versions), "workerCodeHashes": sorted(worker_hashes),
            "providerRequestIdsAvailable": sum(bool(item.get("requestId")) for item in items),
            "metrics": {"coverage": coverage, "agreementOnValid": agreement,
                        "agreementOnPlanned": fraction(agreements, planned),
                        "referenceChallengeHit": fraction(tp, positive),
                        "referencePassHit": fraction(tn, planned - positive),
                        "referenceChallengeHitOnValid": fraction(tp, valid_positive),
                        "referencePassHitOnValid": fraction(tn, valid_negative)},
            "classCoverage": {"referenceChallenge": fraction(valid_positive, positive),
                              "referencePass": fraction(valid_negative, planned - positive)},
            "constantBaselines": {"neverChallengeOnPlanned": fraction(planned - positive, planned),
                                  "alwaysChallengeOnPlanned": fraction(positive, planned),
                                  "neverChallengeOnValid": fraction(valid_negative, len(successes)),
                                  "alwaysChallengeOnValid": fraction(valid_positive, len(successes))},
            "confusionOnValid": {"truePositive": tp, "trueNegative": tn,
                                 "falsePositive": fp, "falseNegative": fn},
            "cells": cells,
        }
        sources.extend(fingerprint(path) for path in paths.values())
    return {
        "schemaVersion": manifest.get("schemaVersion"), "mode": "llm", "suiteVersion": SUITE_VERSION,
        "requestedModel": model, "providerHost": manifest.get("providerHost"),
        "status": manifest.get("status"),
        "startedAt": manifest.get("startedAt"), "finishedAt": manifest.get("finishedAt"),
        "sources": sources, "runs": runs,
        "limitations": [
            "本轮每个场景只调用一次、无重试；n=1 试运行不能估计稳定性或支持显著性结论。",
            "一致率仅适用于 probe-v2 公开生成假设与单步效用，不代表真实对局胜率。",
            "参考质疑/不质疑命中率的分母为计划中的相应参考类别；失败和未完成均不算命中。",
            "图表离线读取指定实验，不发起供应商请求，也不读取旧示例成绩。",
        ],
    }


def plot_experiment_reference(data: dict, boards: list[str], output: Path) -> None:
    colors = ["#2466a0", "#ca762d", "#c3ddee", "#f2d0ad", "#b7bec6", "#f0f2f4"]
    cmap = ListedColormap(colors)
    fig, axes = plt.subplots(1, 2, figsize=(13.5, 8), sharey=True)
    fig.subplots_adjust(left=0.12, right=0.97, top=0.78, bottom=0.24, wspace=0.1)
    for ax, rule, title in zip(axes, ("neutral", "threat"), ("中性规则", "生存威胁规则")):
        run = data["runs"][rule]
        by_cell = {(cell["board"], cell["challengerGun"]): cell for cell in run["cells"]}
        values = np.empty((len(boards), 6))
        for i, board in enumerate(boards):
            for gun in range(6):
                cell = by_cell[board, gun]
                if cell["outcome"] == "SUCCESS":
                    category = (1 if cell["wasChallenged"] else 0) + (0 if cell["agreesWithReference"] else 2)
                    label = f"实：{'质疑' if cell['wasChallenged'] else '不质疑'}\n参：{'质疑' if cell['referenceChallenge'] else '不质疑'}"
                else:
                    category = 4 if cell["outcome"] == "ERROR" else 5
                    label = f"执行失败\n{cell['errorType']}" if category == 4 else "未完成"
                values[i, gun] = category
                ax.text(gun, i, label, ha="center", va="center", fontsize=8.5,
                        color="white" if category in {0, 1} else "#172c43")
        ax.imshow(values, cmap=cmap, vmin=-0.5, vmax=5.5, aspect="auto")
        ax.set_xticks(range(6), range(6))
        ax.set_yticks(range(len(boards)), boards)
        ax.set_xlabel("质疑者已开枪次数", labelpad=10)
        metric = run["metrics"]["agreementOnValid"]
        ax.set_title(f"{title} · 与参考一致 {metric['numerator']}/{metric['denominator']} 个有效响应", pad=14, fontsize=12)
    legend = [Patch(facecolor=colors[i], label=label) for i, label in enumerate(
        ("不质疑且一致", "质疑且一致", "不质疑但不符", "质疑但不符", "执行失败", "未完成"))]
    fig.legend(handles=legend, loc="lower center", bbox_to_anchor=(0.53, 0.105), ncol=3, frameon=False, fontsize=10)
    fig.suptitle("本轮真实 DeepSeek：逐场景动作与条件参考", x=0.06, y=0.97, ha="left", fontsize=19, fontweight="bold")
    versions = " / ".join(sorted({version for run in data["runs"].values() for version in run["modelVersions"]}))
    fig.text(0.06, 0.905, f"请求 {data['requestedModel']} / 返回 {versions} · probe-v2 · 每格 n=1、无重试；实=模型动作，参=条件参考", fontsize=11)
    fig.text(0.06, 0.045, "一致只表示满足本套件公开假设下的单步参考；不是整局胜率，也不代表重复运行的稳定表现。", fontsize=10)
    save(fig, output, "real-deepseek-reference")


def plot_experiment_outcomes(data: dict, output: Path) -> None:
    definitions = (("coverage", "执行覆盖率"), ("agreementOnValid", "有效响应参考一致率"),
                   ("agreementOnPlanned", "端到端参考一致率"),
                   ("referenceChallengeHit", "参考质疑类命中率\n（计划正类为分母）"),
                   ("referencePassHit", "参考不质疑类命中率\n（计划负类为分母）"))
    fig, ax = plt.subplots(figsize=(12.2, 8))
    fig.subplots_adjust(left=0.25, right=0.97, top=0.73, bottom=0.31)
    positions = np.arange(len(definitions))
    for rule, shift, color, label in (("neutral", -0.18, "#2466a0", "中性规则"),
                                      ("threat", 0.18, "#ca762d", "生存威胁规则")):
        run = data["runs"][rule]
        metrics = [run["metrics"][key] for key, _ in definitions]
        values = [100 * metric["value"] if metric["value"] is not None else 0 for metric in metrics]
        ax.barh(positions + shift, values, height=0.28, color=color, label=label)
        for i, (metric, value) in enumerate(zip(metrics, values)):
            text = f"{metric['value']:.1%}" if metric["value"] is not None else "未定义"
            ax.text(value + 1.0, positions[i] + shift,
                    f"{text}  ({metric['numerator']}/{metric['denominator']})", va="center", fontsize=10)
    ax.set_yticks(positions, [label for _, label in definitions])
    ax.invert_yaxis()
    ax.set_xlim(0, 125)
    ax.set_xticks(range(0, 101, 20))
    ax.xaxis.set_major_formatter(PercentFormatter(100))
    ax.set_xlabel("比例（每项显示实际分子 / 分母）", labelpad=10)
    ax.set_axisbelow(True)
    ax.grid(axis="x", color="#e2e8f0", linewidth=0.7)
    ax.legend(loc="lower left", bbox_to_anchor=(0, 1.04), ncol=2, frameon=False)
    ax.spines["left"].set_visible(False)
    fig.suptitle("本轮真实 DeepSeek：执行覆盖与参考一致性", x=0.06, y=0.97, ha="left", fontsize=19, fontweight="bold")
    versions = " / ".join(sorted({version for run in data["runs"].values() for version in run["modelVersions"]}))
    fig.text(0.06, 0.905, f"请求 {data['requestedModel']} / 返回 {versions} · 每规则 48 个计划场景 · 每格 n=1、无重试", fontsize=11)
    neutral, threat = data["runs"]["neutral"], data["runs"]["threat"]
    class_notes = []
    for rule, label in (("neutral", "中性"), ("threat", "威胁")):
        positive = data["runs"][rule]["classCoverage"]["referenceChallenge"]
        negative = data["runs"][rule]["classCoverage"]["referencePass"]
        class_notes.append(f"{label}有效正类 {positive['numerator']}/{positive['denominator']}、负类 {negative['numerator']}/{negative['denominator']}")
    fig.text(0.06, 0.175, "类别覆盖：" + "；".join(class_notes) + "。", fontsize=10)
    fig.text(0.06, 0.13, f"中性：成功 {neutral['successCases']} / 失败 {neutral['failureCases']} / 未完成 {neutral['pendingCases']}；"
             f"威胁：成功 {threat['successCases']} / 失败 {threat['failureCases']} / 未完成 {threat['pendingCases']}。", fontsize=10)
    fig.text(0.06, 0.085, "失败、未完成不算命中。全计划永不质疑基线 35/48=72.9%，仅与端到端一致率同分母。", fontsize=10)
    fig.text(0.06, 0.04, "本轮用于初步检查，不作显著性、威胁规则因果影响或真实整局能力结论。", fontsize=10)
    save(fig, output, "real-deepseek-outcomes")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, default=ROOT / "docs/figures")
    parser.add_argument("--experiment-dir", type=Path, help="本轮真实实验 experiment.json 所在目录")
    args = parser.parse_args()
    try:
        font = configure_style()
        reference = reference_data()
        experiment = experiment_data(args.experiment_dir, reference) if args.experiment_dir else None
        args.out_dir.mkdir(parents=True, exist_ok=True)
        plot_reference(reference, args.out_dir)
        if experiment is not None:
            plot_experiment_reference(experiment, reference["boards"], args.out_dir)
            plot_experiment_outcomes(experiment, args.out_dir)
        output = {
            "schemaVersion": "evaluation-charts-v3", "providerCallsDuringPlotting": 0,
            "generator": fingerprint(Path(__file__)), "matplotlibVersion": matplotlib.__version__,
            "font": font, "v2Reference": reference, "experiment": experiment,
        }
        (args.out_dir / "chart-data.json").write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except (ValueError, KeyError, TypeError, OSError) as exc:
        parser.exit(1, f"绘图失败：{exc}\n")
    count = 3 if experiment is not None else 1
    print(f"已生成 {count} 组 PNG/SVG 及 chart-data.json：{args.out_dir.resolve()}")


if __name__ == "__main__":
    main()
