"""左轮手枪的风险算术。

游戏中每个玩家一把 6 仓左轮，一发实弹随机装填；每开一枪，转轮走一格。
已开 g 枪且仍存活，说明这 g 个仓都是空的，实弹在剩下 6-g 个仓里，
所以「下一枪命中」的概率就是 1/(6-g)：从 1/6 单调升到 1/1。

该命中率用于定义单步代理效用；它本身不构成完整博弈最优策略的证明。
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Sequence

CHAMBERS = 6

#: 上游 game.py 的牌堆：6Q / 6K / 6A / 2Joker，共 20 张。
DECK = {"Q": 6, "K": 6, "A": 6, "Joker": 2}
TARGET_CARDS = ("Q", "K", "A")


def hit_probability(guns_fired: int) -> float:
    """开过 guns_fired 枪之后，下一枪命中的概率。"""
    if type(guns_fired) is not int or not 0 <= guns_fired < CHAMBERS:
        raise ValueError("guns_fired 必须是 0–5 的整数")
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
    """兼容旧名字：返回随机手牌缺少指定数量合法牌的概率。

    仅在 probe-v2 的公开生成假设下，它才等于诈唬概率：宣称张数在发牌前
    外生固定且独立于手牌；出牌者有足够合法牌时总是诚实，否则诈唬。
    真实博弈里出牌张数是策略选择，观察动作会更新手牌分布，因此本值不是
    策略无关的诈唬率下界。历史牌局后续回合还存在出牌历史的选择偏差。
    """
    if target not in TARGET_CARDS:
        raise ValueError("target 必须是 Q、K 或 A")
    counts = Counter(challenger_hand)
    if any(card not in DECK or count > DECK[card] for card, count in counts.items()):
        raise ValueError("质疑者手牌不符合牌堆数量")
    unseen_count = sum(DECK.values()) - len(challenger_hand)
    if type(actor_hand_size) is not int or not 0 <= actor_hand_size <= unseen_count:
        raise ValueError("actor_hand_size 超出未见牌数量")
    if type(claim_count) is not int or not 1 <= claim_count <= actor_hand_size:
        raise ValueError("claim_count 必须在 1 和出牌前手牌数之间")
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


# 新调用方使用能准确表达适用范围的名字；旧导入保持兼容。
synthetic_bluff_probability = forced_bluff_floor


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
