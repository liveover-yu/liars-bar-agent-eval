"""风险校准：把模型在真实牌局里的每个质疑决策，跟一个 EV 标尺对表。

为什么需要标尺：上游只有「谁赢了」。赢 40% 算好还是算差，没有参照物
无法回答——四个模型打，每人先验是 25%，赢 40% 可能只是运气。

这里比较手工启发式和留一局经验策略。随机发牌的超几何公式忽略了动作选择
与出牌历史，不能充当真实博弈的策略无关真值，也不能直接把差异叫校准误差。

诚实说明边界：这是**决策级反事实评估**，不是整局重放。我们假设对手
的出牌序列不变，只替换质疑者的决定——因为一旦质疑成功/失败，小局会
立刻结束，后续轨迹就完全不同了，那是另一个实验（要真跑 API）。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import math

from .records import DECK_SIZE, LEGAL_IN_DECK, Decision
from .risk import breakeven_bluff_probability


def _hypergeom_at_least(n_pop: int, n_success: int, draws: int, at_least: int) -> float:
    """从 n_pop 里不放回抽 draws 张，成功（合法牌）张数 >= at_least 的概率。

    math.comb 在 n<0 或 k>n 时会抛 ValueError，这里的两处裁剪对应
    「牌堆里合法牌不够」和「抽的张数超过池子」两种真实情况，不是防御性代码。
    """
    n_success = max(0, min(n_success, n_pop))
    draws = max(0, min(draws, n_pop))
    if at_least <= 0:
        return 1.0
    if draws < at_least:
        return 0.0

    total = math.comb(n_pop, draws)
    hit = 0
    for m in range(at_least, min(draws, n_success) + 1):
        hit += math.comb(n_success, m) * math.comb(n_pop - n_success, draws - m)
    return hit / total


def naive_bluff_prior(decision: Decision) -> float:
    """模型「能出合法牌就出合法牌」时，这手牌是诈唬的概率。

    质疑者只知道自己的 5 张牌。剩下 15 张里合法牌 = 8 - 自己手里的合法牌数。
    出牌者这一手之前持 h+k 张（h 是 remaining_cards 长度，k 是本手张数）。
    如果他们手里合法牌少于 k 张，就*被迫*诈唬。

    P(诈唬) = 1 - P(手里合法牌 >= k)

    这是忽略出牌策略与历史的启发式先验，不是实际博弈的概率下界。
    出牌张数和先前出牌都会改变手牌后验；这里只把它作为基线。
    """
    legal_outside = LEGAL_IN_DECK - decision.legal_in_challenger_hand
    unseen = DECK_SIZE - len(decision.challenger_hand)
    # records 已保存出牌前手牌数，不能再次加本手张数。
    hand_size = decision.actor_hand_size
    p_honest = _hypergeom_at_least(unseen, legal_outside, hand_size, decision.k)
    return 1.0 - p_honest


def hand_size_bucket(decision: Decision) -> int:
    """出牌者这一手之前的手牌数分桶。5 张 = 小局刚开始，越少越接近收尾。"""
    return min(decision.actor_hand_size, 5)


def empirical_bluff_prior(
    decisions: list[Decision],
    holdout_game: str | None = None,
) -> dict[tuple[int, int, int], float]:
    """从数据里学诈唬先验，按 (张数, 手牌数桶, 枪膛) 分组。

    holdout_game 给定时，那一局的样本不参与拟合——评估留出的那一局时
    不能看它自己的答案，否则标尺会作弊。
    """
    counts: dict[tuple[int, int, int], list[int]] = defaultdict(lambda: [0, 0])
    for d in decisions:
        if d.game_id == holdout_game:
            continue
        key = (d.k, hand_size_bucket(d), d.actor_gun)
        counts[key][0] += 1
        counts[key][1] += d.bluff

    prior: dict[tuple[int, int, int], float] = {}
    for key, (n, k) in counts.items():
        # Laplace 平滑，避免小样本里出现 0 或 1 的极端先验
        prior[key] = (k + 1) / (n + 2)
    return prior


def lookup_prior(
    table: dict[tuple[int, int, int], float],
    decision: Decision,
) -> float:
    """先查精确组 (张数, 手牌桶, 枪膛)，不够就逐级退到更粗的分组。"""
    k, hb, gun = decision.k, hand_size_bucket(decision), decision.actor_gun
    for key in ((k, hb, gun), (k, hb, -1), (k, -1, -1)):
        if key in table:
            return table[key]
    return table.get((-1, -1, -1), 0.5)


def empirical_table(decisions: list[Decision], holdout_game: str | None = None):
    """带层级退化的先验表。先拟合细粒度，再补上粗粒度备份。"""
    table = empirical_bluff_prior(decisions, holdout_game)

    fine: dict[tuple[int, int, int], list[int]] = defaultdict(lambda: [0, 0])
    coarse: dict[tuple[int, int, int], list[int]] = defaultdict(lambda: [0, 0])
    by_k: dict[tuple[int, int, int], list[int]] = defaultdict(lambda: [0, 0])
    for d in decisions:
        if d.game_id == holdout_game:
            continue
        for bucket, key in (
            (fine, (d.k, hand_size_bucket(d), d.actor_gun)),
            (coarse, (d.k, hand_size_bucket(d), -1)),
            (by_k, (d.k, -1, -1)),
        ):
            bucket[key][0] += 1
            bucket[key][1] += d.bluff

    for bucket in (coarse, by_k):
        for key, (n, k) in bucket.items():
            table.setdefault(key, (k + 1) / (n + 2))

    overall = sum(d.bluff for d in decisions if d.game_id != holdout_game)
    total = sum(1 for d in decisions if d.game_id != holdout_game)
    table[-1, -1, -1] = (overall + 1) / (total + 2)
    return table


def ev_rational_empirical_leave_one_game_out(decisions: list[Decision]) -> float:
    """EV 标尺的诚实版本：每一局都用其他 49 局学到的先验来决策。"""
    tables = {g: empirical_table(decisions, holdout_game=g) for g in {d.game_id for d in decisions}}
    ev = 0.0
    challengers = 0
    for d in decisions:
        p = lookup_prior(tables[d.game_id], d)
        if p <= breakeven_bluff_probability(d.challenger_gun, d.actor_gun):
            continue
        challengers += 1
        if d.bluff:
            ev += 1.0 / (6 - d.actor_gun)
        else:
            ev -= 1.0 / (6 - d.challenger_gun)
    return 100 * ev / len(decisions) if decisions else 0.0


@dataclass(frozen=True)
class Policy:
    name: str
    decision_fn: object  # Callable[[Decision], bool]

    def __call__(self, d: Decision) -> bool:
        return self.decision_fn(d)  # type: ignore[operator]


def never_challenge(d: Decision) -> bool:
    return False


def always_challenge(d: Decision) -> bool:
    return True


def ev_rational(d: Decision) -> bool:
    """p_bluff > p* 才质疑。这是本项目的核心标尺。"""
    p = naive_bluff_prior(d)
    threshold = breakeven_bluff_probability(d.challenger_gun, d.actor_gun)
    return p > threshold


def gun_monotone(d: Decision) -> bool:
    """只说「我枪膛里还有位置就质疑」——忽略对手状态和牌面信息。

    用来区分：模型的质疑率随枪膛上升，是因为算了 EV，还是因为只是
    「快死了就赌一把」的浅层启发式。
    """
    return d.challenger_gun == 0


def empirical_policy(decisions: list[Decision]) -> Policy:
    """留一局先验表当决策依据的策略。

    和 ev_rational 的区别：该版本从其他局学条件诈唬率，减少对随机手牌
    假设的依赖；但历史采样分布和长期策略仍限制其解释范围。
    """
    tables = {g: empirical_table(decisions, holdout_game=g) for g in {d.game_id for d in decisions}}

    def decide(d: Decision) -> bool:
        p = lookup_prior(tables[d.game_id], d)
        return p > breakeven_bluff_probability(d.challenger_gun, d.actor_gun)

    return Policy("ev_empirical", decide)


#: 这个顺序决定报告里的排列：从最被动到最激进，EV 标尺夹在中间。
POLICIES = [
    Policy("never", never_challenge),
    Policy("ev_rational", ev_rational),
    Policy("gun_monotone", gun_monotone),
    Policy("llm_actual", lambda d: d.challenged),
    Policy("always", always_challenge),
]


@dataclass
class PolicyResult:
    name: str
    challenges: int
    decisions: int
    accuracy: float          # P(诈唬 | 质疑)
    recall: float            # P(质疑 | 诈唬)
    ev_total: float
    ev_per_100: float
    self_deaths: float       # 期望自己挨枪数
    actor_deaths: float      # 期望对手挨枪数

    @property
    def challenge_rate(self) -> float:
        return self.challenges / self.decisions if self.decisions else 0.0


def evaluate_policy(policy: Policy, decisions: list[Decision]) -> PolicyResult:
    """在决策级反事实下评估一个策略。

    EV 的单位是「期望被淘汰的对手数 − 期望被淘汰的自己数」。
    质疑对手诈唬 → 对手开枪，期望淘汰 r_actor；对手诚实 → 自己开枪，期望淘汰 r_self。
    不质疑记为 0（本小局继续，双方都没代价）。
    """
    challenged = [d for d in decisions if policy(d)]
    correct = [d for d in challenged if d.bluff]

    ev_total = 0.0
    self_deaths = 0.0
    actor_deaths = 0.0
    for d in challenged:
        if d.bluff:
            r = 1.0 / (6 - d.actor_gun)
            ev_total += r
            actor_deaths += r
        else:
            r = 1.0 / (6 - d.challenger_gun)
            ev_total -= r
            self_deaths += r

    n = len(decisions)
    return PolicyResult(
        name=policy.name,
        challenges=len(challenged),
        decisions=n,
        accuracy=len(correct) / len(challenged) if challenged else 0.0,
        recall=len(correct) / sum(d.bluff for d in decisions) if any(d.bluff for d in decisions) else 0.0,
        ev_total=ev_total,
        ev_per_100=100 * ev_total / n if n else 0.0,
        self_deaths=self_deaths,
        actor_deaths=actor_deaths,
    )


def oracle_ev(decisions: list[Decision]) -> float:
    """完美信息上界：只质疑真正的诈唬。任何策略都不可能超过它。"""
    ev = 0.0
    for d in decisions:
        if d.bluff:
            ev += 1.0 / (6 - d.actor_gun)
    return 100 * ev / len(decisions) if decisions else 0.0


if __name__ == "__main__":
    from .records import load_all

    decisions, systems, meta = load_all()
    print(f"{len(meta['games'])} 局 / {len(decisions)} 个决策点 / {len(systems)} 次系统质疑\n")
    for policy in POLICIES + [empirical_policy(decisions)]:
        r = evaluate_policy(policy, decisions)
        print(
            f"{r.name:<14} 质疑 {r.challenges:>4}/{r.decisions} "
            f"= {r.challenge_rate:>6.1%}  准确率 {r.accuracy:>6.1%}  "
            f"召回 {r.recall:>6.1%}  EV {r.ev_per_100:>+7.2f}/100"
        )
    print(f"{'oracle':<14} EV 上界 {oracle_ev(decisions):+.2f}/100")
