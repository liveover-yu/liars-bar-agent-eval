"""左轮手枪的风险算术。

游戏中每个玩家一把 6 仓左轮，一发实弹随机装填；每开一枪，转轮走一格。
已开 g 枪且仍存活，说明这 g 个仓都是空的，实弹在剩下 6-g 个仓里，
所以「下一枪命中」的概率就是 1/(6-g)：从 1/6 单调升到 1/1。

这是这个项目能当「评估」而不是「表演」的全部理由——存在一个可以精确
写下来的风险标尺，模型的行为可以跟它对表。
"""

from __future__ import annotations

import math
from collections.abc import Sequence

CHAMBERS = 6

#: 上游 game.py 的牌堆：6Q / 6K / 6A / 2Joker，共 20 张。
DECK = {"Q": 6, "K": 6, "A": 6, "Joker": 2}
TARGET_CARDS = ("Q", "K", "A")


def hit_probability(guns_fired: int) -> float:
    """开过 guns_fired 枪之后，下一枪命中的概率。"""
    return 1.0 / (CHAMBERS - guns_fired)


def breakeven_bluff_probability(challenger_gun: int, actor_gun: int) -> float:
    """质疑的盈亏平衡诈唬概率。

    质疑的收益：对方诈唬 → 对方挨枪，期望死亡率 r_actor = 1/(6-actor_gun)
    质疑的成本：对方诚实 → 自己挨枪，期望死亡率 r_self  = 1/(6-challenger_gun)

    EV(p) = p * r_actor - (1-p) * r_self
    令 EV=0 得  p* = r_self / (r_self + r_actor)

    直觉：自己越接近实弹（challenger_gun 越大），p* 越接近 1，越不该质疑。
    """
    r_self = hit_probability(challenger_gun)
    r_actor = hit_probability(actor_gun)
    return r_self / (r_self + r_actor)


def forced_bluff_floor(
    challenger_hand: Sequence[str],
    target: str,
    claim_count: int,
    actor_hand_size: int,
) -> float:
    """「能出真牌就出真牌」的对手，这手牌**必然**是诈唬的概率下界。

    质疑者看得见自己的 5 张牌，所以知道剩下 15 张里有多少张能当目标牌用：
    目标牌本身 + Joker。出牌者这一手之前持 actor_hand_size 张，从中抽
    目标牌数 m 的分布是超几何；m >= claim_count 时他能诚实出牌（但可能
    仍选择诈唬），m < claim_count 时**被迫**诈唬。

    P(被迫诈唬) = 1 - P(m >= claim_count)

    真实诈唬率 >= 这个下界，因为它没算「手里够牌但主动诈唬」。所以拿它当
    标尺是保守的——下界就已经越过盈亏平衡点时，质疑才无条件正确。
    """
    remaining = dict(DECK)
    for card in challenger_hand:
        remaining[card] -= 1
    good = remaining[target] + remaining["Joker"]
    unseen = sum(remaining.values())
    total = math.comb(unseen, actor_hand_size)
    honest = sum(
        math.comb(good, m) * math.comb(unseen - good, actor_hand_size - m)
        for m in range(claim_count, min(actor_hand_size, good) + 1)
    )
    return 1.0 - honest / total


def challenge_ev(p_bluff: float, challenger_gun: int, actor_gun: int) -> float:
    """以「期望被淘汰的对手数 − 期望被淘汰的自己数」为单位的一次质疑的 EV。

    不质疑的 EV 记 0：不管，本小局继续。
    """
    r_self = hit_probability(challenger_gun)
    r_actor = hit_probability(actor_gun)
    return p_bluff * r_actor - (1 - p_bluff) * r_self


if __name__ == "__main__":
    for g in range(CHAMBERS):
        print(f"已开 {g} 枪 → 下一枪命中 {hit_probability(g):.4f}")
    print()
    print("盈亏平衡诈唬概率 p*（行=质疑者已开枪，列=出牌者已开枪）")
    print("      " + "".join(f"{a:>8}" for a in range(CHAMBERS)))
    for c in range(CHAMBERS):
        row = "".join(f"{breakeven_bluff_probability(c, a):>8.3f}" for a in range(CHAMBERS))
        print(f"c={c}  {row}")
