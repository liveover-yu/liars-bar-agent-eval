"""把探针结果渲染成 SVG，供 README 直接引用。

    python -m analysis.figures            # 读 analysis/results/probe_*.json

产物（docs/）：
    fig_boards.svg     每个牌面的质疑率，按先验下界排序
    fig_gun.svg        同一牌面跨枪膛：门槛在升，质疑率也在升
    fig_groups.svg     该质疑组 vs 不该质疑组，威胁版 / 中性版

图是脚本生成的，不是截图——数据一变，重跑就换图。配色用一套已验证的
分类色（蓝/橙/青），浅色底，因为 GitHub README 是白底渲染。
"""

from __future__ import annotations

import html
import json
from pathlib import Path

RESULTS = Path(__file__).resolve().parent / "results"
DOCS = Path(__file__).resolve().parents[1] / "docs"

# 配色（浅色面）：分类 1=蓝、2=橙；弱化灰；文字/网格
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
BASELINE = "#c3c2b7"
SERIES_1 = "#2a78d6"
SERIES_2 = "#eb6834"
DEEMPH = "#c3c2b7"

FONT = 'system-ui, -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif'


def _esc(text: str) -> str:
    return html.escape(str(text))


def _svg(width: int, height: int, body: list[str], title: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="{_esc(title)}">\n'
        f'<rect width="{width}" height="{height}" fill="{SURFACE}"/>\n'
        f"<style>text{{font-family:{FONT};}}</style>\n"
        + "\n".join(body)
        + "\n</svg>\n"
    )


def _text(x, y, s, size=12, fill=INK_2, anchor="start", weight="400"):
    return (f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" fill="{fill}" '
            f'text-anchor="{anchor}" font-weight="{weight}">{_esc(s)}</text>')


def _hbar(x, y, w, h, fill, rx=4):
    """横向条：数据端 4px 圆角，基线端方角。"""
    if w <= 0:
        return ""
    w = max(w, rx)
    r = min(rx, w)
    return (f'<path d="M{x:.1f},{y:.1f} h{w - r:.1f} a{r},{r} 0 0 1 {r},{r} '
            f'v{h - 2 * r:.1f} a{r},{r} 0 0 1 -{r},{r} h-{w - r:.1f} z" fill="{fill}"/>')


def _load() -> dict:
    files = sorted(RESULTS.glob("probe_*.json"))
    if not files:
        raise SystemExit("没有 probe_*.json，先跑 python -m analysis.probe")
    return json.loads(files[-1].read_text(encoding="utf-8"))


def _sorted_boards(payload: dict, rule: str = "threat") -> list[dict]:
    """按先验下界把 8 个牌面排好，每个牌面取 gun0 的质疑率做代表。"""
    cases = {c["probe_id"]: c for c in payload["cases"]}
    rate = {pid: sum(rs) / len(rs) for pid, rs in payload["runs"][rule]["by_case"].items()}
    seen: dict[str, dict] = {}
    for pid, case in cases.items():
        if case["challenger_gun"] != 0:
            continue
        seen[case["board"]] = {**case, "rate": rate[pid]}
    return sorted(seen.values(), key=lambda c: c["bluff_floor"])


def _board_label(case: dict) -> str:
    hand = ", ".join(case["challenger_hand"])
    return f'{hand} → {case["claim_count"]}×{case["target"]}'


def fig_boards(payload: dict) -> str:
    """每个牌面（gun0）的质疑率，条长 = 质疑率，右侧标先验下界。"""
    rows = _sorted_boards(payload)
    W, H = 760, 40 + 34 * len(rows) + 56
    left, right, top = 232, 96, 56
    plot_w = W - left - right
    body = [_text(24, 28, "每个牌面的质疑率（自己 0 枪、对手 0 枪）", 15, INK, weight="600")]
    body.append(_text(24, 46, "左侧是质疑者手牌与对方宣称；右侧数字是「被迫诈唬下界」，即他能算出的最低诈唬概率", 11, MUTED))

    # x 轴网格 0/25/50/75/100%
    for pct in (0, 25, 50, 75, 100):
        x = left + plot_w * pct / 100
        body.append(f'<line x1="{x:.1f}" y1="{top - 8}" x2="{x:.1f}" y2="{H - 44}" '
                    f'stroke="{GRID}" stroke-width="1"/>')
        body.append(_text(x, H - 28, f"{pct}%", 10, MUTED, "middle"))
    body.append(f'<line x1="{left}" y1="{top - 8}" x2="{left}" y2="{H - 44}" '
                f'stroke="{BASELINE}" stroke-width="1"/>')

    for i, case in enumerate(rows):
        y = top + i * 34
        correct = case["rational"]
        fill = SERIES_1 if correct else DEEMPH
        body.append(_text(left - 12, y + 18, _board_label(case), 11, INK_2, "end"))
        w = plot_w * case["rate"]
        if w <= 0:
            body.append(_text(left + 6, y + 18, "0%", 11, INK, "start", "600"))
        else:
            body.append(_hbar(left, y + 6, w, 20, fill))
            body.append(_text(left + w + 8, y + 18, f'{case["rate"]:.0%}', 11, INK, "start", "600"))
        # 右栏：下界
        body.append(_text(W - 20, y + 18, f'下界 {case["bluff_floor"]:.3f}', 11,
                          INK if correct else MUTED, "end"))

    # 图例
    ly = H - 12
    body.append(f'<rect x="{left}" y="{ly - 9}" width="10" height="10" rx="3" fill="{SERIES_1}"/>')
    body.append(_text(left + 16, ly, "下界高于门槛：质疑无条件正确", 11, INK_2))
    body.append(f'<rect x="{left + 250}" y="{ly - 9}" width="10" height="10" rx="3" fill="{DEEMPH}"/>')
    body.append(_text(left + 266, ly, "下界低于门槛：不该质疑", 11, INK_2))
    return _svg(W, H, body, "每个牌面的质疑率")


def fig_gun(payload: dict) -> str:
    """同一牌面跨枪膛：横轴自己枪膛，两条线 = 门槛与实测质疑率。"""
    cases = {c["probe_id"]: c for c in payload["cases"]}
    rate = {pid: sum(rs) / len(rs) for pid, rs in payload["runs"]["threat"]["by_case"].items()}

    W, H = 760, 360
    left, right, top, bottom = 64, 190, 56, 64
    pw, ph = W - left - right, H - top - bottom

    body = [_text(24, 28, "枪膛越满，正确门槛越高——但模型的质疑率也在涨", 15, INK, weight="600")]
    body.append(_text(24, 46, "「K, A, K, Joker, A 宣称 3×Q」：正确动作在 gun1 之后翻转成「不该质疑」，实测却一路升到 100%", 11, MUTED))

    def X(g):  # 自己的枪膛 0..5
        return left + pw * g / 5

    def Y(p):
        return top + ph * (1 - p)

    for pct in (0, 25, 50, 75, 100):
        body.append(f'<line x1="{left}" y1="{Y(pct / 100):.1f}" x2="{left + pw}" '
                    f'y2="{Y(pct / 100):.1f}" stroke="{GRID}" stroke-width="1"/>')
        body.append(_text(left - 10, Y(pct / 100) + 4, f"{pct}%", 10, MUTED, "end"))
    body.append(f'<line x1="{left}" y1="{Y(0):.1f}" x2="{left + pw}" y2="{Y(0):.1f}" '
                f'stroke="{BASELINE}" stroke-width="1"/>')
    for g in range(6):
        body.append(_text(X(g), H - bottom + 20, f"gun{g}", 10, MUTED, "middle"))
    body.append(_text(left + pw / 2, H - 14, "自己已开枪数（下一枪命中概率 1/(6−g)）", 11, MUTED, "middle"))

    # 门槛线：折线，橙
    pstar = [cases[f"noQ_3_gun{g}"]["breakeven"] for g in range(6)]
    pts = " ".join(f"{X(g):.1f},{Y(p):.1f}" for g, p in enumerate(pstar))
    body.append(f'<polyline points="{pts}" fill="none" stroke="{SERIES_2}" stroke-width="2" '
                f'stroke-linejoin="round" stroke-linecap="round" stroke-dasharray="0"/>')

    # 实测质疑率：折线，蓝，带端点
    ch = [rate[f"noQ_3_gun{g}"] for g in range(6)]
    pts = " ".join(f"{X(g):.1f},{Y(p):.1f}" for g, p in enumerate(ch))
    body.append(f'<polyline points="{pts}" fill="none" stroke="{SERIES_1}" stroke-width="2" '
                f'stroke-linejoin="round" stroke-linecap="round"/>')
    for g, p in enumerate(ch):
        body.append(f'<circle cx="{X(g):.1f}" cy="{Y(p):.1f}" r="4" fill="{SERIES_1}" '
                    f'stroke="{SURFACE}" stroke-width="2"/>')

    # 端点直接标注
    body.append(_text(X(5) + 10, Y(ch[5]) + 4, f"实测质疑率 {ch[5]:.0%}", 11, INK, "start", "600"))
    body.append(_text(X(5) + 10, Y(pstar[5]) + 4, f"质疑门槛 {pstar[5]:.0%}", 11, INK, "start", "600"))

    # 图例
    ly = H - 26
    body.append(f'<line x1="{left}" y1="{ly - 4}" x2="{left + 18}" y2="{ly - 4}" stroke="{SERIES_1}" stroke-width="2"/>')
    body.append(_text(left + 24, ly, "模型实测质疑率", 11, INK_2))
    body.append(f'<line x1="{left + 150}" y1="{ly - 4}" x2="{left + 168}" y2="{ly - 4}" stroke="{SERIES_2}" stroke-width="2"/>')
    body.append(_text(left + 174, ly, "盈亏平衡门槛 p*", 11, INK_2))
    return _svg(W, H, body, "同一牌面跨枪膛的质疑率与门槛")


def fig_groups(payload: dict) -> str:
    """该质疑组 vs 不该质疑组，两种规则文本。"""
    def group_rate(rule: str, should: bool) -> float:
        cases = {c["probe_id"]: c for c in payload["cases"]}
        vals = [r for pid, rs in payload["runs"][rule]["by_case"].items()
                if cases[pid]["rational"] == should for r in rs]
        return sum(vals) / len(vals)

    W, H = 760, 300
    left, right, top = 150, 120, 64
    pw = W - left - right
    body = [_text(24, 28, "区分度是负的：该质疑时反而回避，不该质疑时反而激进", 15, INK, weight="600")]
    body.append(_text(24, 46, "按「被迫诈唬下界 vs 门槛」分组后合并全部 240 次调用", 11, MUTED))

    bars = [
        ("生存威胁版", "该质疑组", group_rate("threat", True), SERIES_1),
        ("生存威胁版", "不该质疑组", group_rate("threat", False), SERIES_2),
        ("中性版", "该质疑组", group_rate("neutral", True), SERIES_1),
        ("中性版", "不该质疑组", group_rate("neutral", False), SERIES_2),
    ]
    for pct in (0, 25, 50, 75, 100):
        x = left + pw * pct / 100
        body.append(f'<line x1="{x:.1f}" y1="{top - 10}" x2="{x:.1f}" y2="{H - 56}" stroke="{GRID}" stroke-width="1"/>')
        body.append(_text(x, H - 38, f"{pct}%", 10, MUTED, "middle"))

    for i, (rule, group, rate, color) in enumerate(bars):
        y = top + i * 46
        body.append(_text(left - 12, y + 18, f"{rule} · {group}", 11, INK_2, "end"))
        w = pw * rate
        body.append(_hbar(left, y + 6, w, 20, color))
        body.append(_text(left + w + 8, y + 18, f"{rate:.1%}", 11, INK, "start", "600"))

    ly = H - 14
    body.append(f'<rect x="{left}" y="{ly - 9}" width="10" height="10" rx="3" fill="{SERIES_1}"/>')
    body.append(_text(left + 16, ly, "该质疑（下界 ≥ 门槛）", 11, INK_2))
    body.append(f'<rect x="{left + 210}" y="{ly - 9}" width="10" height="10" rx="3" fill="{SERIES_2}"/>')
    body.append(_text(left + 226, ly, "不该质疑（下界 < 门槛）", 11, INK_2))
    return _svg(W, H, body, "该质疑组与不该质疑组的质疑率")


def main() -> None:
    payload = _load()
    DOCS.mkdir(parents=True, exist_ok=True)
    for name, render in [
        ("fig_boards.svg", fig_boards),
        ("fig_gun.svg", fig_gun),
        ("fig_groups.svg", fig_groups),
    ]:
        (DOCS / name).write_text(render(payload), encoding="utf-8")
        print(f"写出 {DOCS / name}")


if __name__ == "__main__":
    main()
