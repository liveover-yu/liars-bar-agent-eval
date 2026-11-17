"""上游没算过的决策层指标。

game_analyze.py 只统计胜场/开枪次数/存活积分——都是结果层。这里补上
行为层：诈唬率、质疑率、质疑准确率、枪膛条件质疑率，以及每个指标
的不确定性。

所有指标的计算都建立在 records.load_all() 的决策样本上，一次读盘，
不调 API。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, asdict

from .records import Decision
from .stats import wilson, cluster_bootstrap, permutation_test


@dataclass
class Rate:
    k: int
    n: int
    lo: float
    hi: float

    @property
    def p(self) -> float:
        return self.k / self.n if self.n else 0.0

    def fmt(self) -> str:
        return f"{self.p:>6.1%} [{self.lo:.1%}, {self.hi:.1%}]  n={self.n}"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["p"] = self.p
        return d


def rate(values: list[bool]) -> Rate:
    k = sum(values)
    lo, hi = wilson(k, len(values))
    return Rate(k=k, n=len(values), lo=lo, hi=hi)


def bluff_rate(decisions: list[Decision]) -> Rate:
    """P(这一手是诈唬)，按决策点计。

    诈唬的定义直接对齐上游规则：打出的牌里只要有一张不是目标牌也不是 Joker。
    """
    return rate([d.bluff for d in decisions])


def challenge_rate(decisions: list[Decision]) -> Rate:
    return rate([d.challenged for d in decisions])


def challenge_accuracy(decisions: list[Decision]) -> Rate:
    """P(质疑是对的 | 质疑了)。随机质疑的基线是当时的诈唬率。"""
    return rate([d.bluff for d in decisions if d.challenged])


def missed_bluff_rate(decisions: list[Decision]) -> Rate:
    """兼容历史名称：P(诈唬 | 未质疑)，即未质疑中的残余诈唬比例。

    这不是分类漏检率 FNR；后者的分母应为全部诈唬，而非全部未质疑。
    """
    return rate([d.bluff for d in decisions if not d.challenged])


def challenge_signal(decisions: list[Decision], iterations: int = 20000) -> dict:
    """质疑这个信号有没有信息量。

    如果 P(诈唬|质疑) == P(诈唬|不质疑)，那质疑行为跟牌面真相无关，
    只是风格。置换检验给 p 值。
    """
    challenged = [d.bluff for d in decisions if d.challenged]
    passed = [d.bluff for d in decisions if not d.challenged]
    p_value = permutation_test(
        [1.0 if x else 0.0 for x in challenged],
        [1.0 if x else 0.0 for x in passed],
        iterations=iterations,
    )
    return {
        "p_bluff_given_challenge": sum(challenged) / len(challenged) if challenged else 0.0,
        "p_bluff_given_pass": sum(passed) / len(passed) if passed else 0.0,
        "lift": (sum(challenged) / len(challenged) if challenged else 0.0)
        - (sum(passed) / len(passed) if passed else 0.0),
        "permutation_p": p_value,
        "base_rate": sum(d.bluff for d in decisions) / len(decisions) if decisions else 0.0,
    }


def by_challenger_gun(decisions: list[Decision]) -> dict[int, Rate]:
    groups: dict[int, list[bool]] = defaultdict(list)
    for d in decisions:
        groups[d.challenger_gun].append(d.challenged)
    return {g: rate(v) for g, v in sorted(groups.items())}


def by_actor_gun(decisions: list[Decision]) -> dict[int, Rate]:
    groups: dict[int, list[bool]] = defaultdict(list)
    for d in decisions:
        groups[d.actor_gun].append(d.challenged)
    return {g: rate(v) for g, v in sorted(groups.items())}


def by_player(decisions: list[Decision], field: str) -> dict[str, dict]:
    """按模型分组。field: 'challenger' 看它作为质疑者的行为，'actor' 看出牌行为。"""
    groups: dict[str, list[Decision]] = defaultdict(list)
    for d in decisions:
        groups[getattr(d, field)].append(d)

    out = {}
    for name, ds in sorted(groups.items()):
        challenged = [d for d in ds if d.challenged]
        out[name] = {
            "decisions": len(ds),
            "challenge_rate": challenge_rate(ds).to_dict(),
            "bluff_rate": bluff_rate(ds).to_dict(),
            "challenge_accuracy": rate([d.bluff for d in challenged]).to_dict(),
            "avg_cards_per_play": sum(d.k for d in ds) / len(ds),
        }
    return out


def by_round_players(decisions: list[Decision]) -> dict[int, dict]:
    """3 人局 vs 4 人局。4 人局里 20 张牌刚好发完，信息结构与 3 人局不同。"""
    groups: dict[int, list[Decision]] = defaultdict(list)
    for d in decisions:
        groups[d.round_players].append(d)
    return {
        n: {
            "decisions": len(ds),
            "bluff_rate": bluff_rate(ds).to_dict(),
            "challenge_rate": challenge_rate(ds).to_dict(),
            "challenge_accuracy": rate([d.bluff for d in ds if d.challenged]).to_dict(),
        }
        for n, ds in sorted(groups.items())
    }


def bluff_by_k(decisions: list[Decision]) -> dict[int, Rate]:
    groups: dict[int, list[bool]] = defaultdict(list)
    for d in decisions:
        groups[d.k].append(d.bluff)
    return {k: rate(v) for k, v in sorted(groups.items()) if len(v) >= 10}


def clustered_challenge_rate(decisions: list[Decision]) -> tuple[float, float, float]:
    """按局重采样的质疑率置信区间（处理局内相关）。"""
    per_game: dict[str, list[float]] = defaultdict(list)
    for d in decisions:
        per_game[d.game_id].append(1.0 if d.challenged else 0.0)
    return cluster_bootstrap(list(per_game.values()), lambda a: float(a.mean()))


def clustered_bluff_rate(decisions: list[Decision]) -> tuple[float, float, float]:
    per_game: dict[str, list[float]] = defaultdict(list)
    for d in decisions:
        per_game[d.game_id].append(1.0 if d.bluff else 0.0)
    return cluster_bootstrap(list(per_game.values()), lambda a: float(a.mean()))
