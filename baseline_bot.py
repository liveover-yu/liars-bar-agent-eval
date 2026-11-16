"""非 LLM 基线 bot：上游完全没有的参照物。

没有它就无法回答「Claude 赢了 40%，算好还是算差」。四个玩家打，先验
胜率 25%；赢 40% 可能只是运气，也可能是真强。要区分，必须有一个
策略固定、行为可复现的对手放在同一张桌子上。

这个 bot 的设计原则是「只用可计算的信息」：

出牌：能出真牌就出真牌（最少张数），没有真牌才诈唬一张。
      这是最保守的出法，也是结构化先验（naive_bluff_prior）背后的假设。
质疑：按 EV 判定。p_bluff 用 demo_records 里学到的经验先验，
      与「自己枪膛越满越不该质疑」的阈值比较。

它和 Game 的接口完全对齐（鸭子类型），所以能直接塞进上游游戏引擎跑整局。
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field

from analysis.calibration import empirical_table, lookup_prior
from analysis.records import Decision, load_all
from analysis.risk import breakeven_bluff_probability


_TABLE = None


def bluff_prior_table():
    """从 50 局 demo 数据学一次诈唬先验，全进程复用。"""
    global _TABLE
    if _TABLE is None:
        decisions, _, _ = load_all()
        _TABLE = empirical_table(decisions)
    return _TABLE


@dataclass
class BotStats:
    """bot 自己的行为计数，报告里用来跟模型对表。"""

    plays: int = 0
    bluffs: int = 0
    challenge_opportunities: int = 0
    challenges: int = 0
    challenge_correct: int = 0
    play_log: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "plays": self.plays,
            "bluffs": self.bluffs,
            "bluff_rate": self.bluffs / self.plays if self.plays else 0.0,
            "challenge_opportunities": self.challenge_opportunities,
            "challenges": self.challenges,
            "challenge_rate": (self.challenges / self.challenge_opportunities
                               if self.challenge_opportunities else 0.0),
            "challenge_correct": self.challenge_correct,
            "challenge_accuracy": (self.challenge_correct / self.challenges
                                   if self.challenges else 0.0),
        }


class EVBotPlayer:
    """鸭子类型对齐 player.Player，可以被 game.Game 直接驱动。"""

    def __init__(self, name: str, gun=0, rng=None):
        self.name = name
        self.hand: list[str] = []
        self.alive = True
        self.rng = rng or random.Random(0)
        self.bullet_position = 0
        self.current_bullet_position = 0
        self.opinions: dict[str, str] = {}
        self.stats = BotStats()

    # --- 与上游 Player 相同的接口 -------------------------------------

    def init_opinions(self, other_players) -> None:
        self.opinions = {p.name: "还不了解这个玩家" for p in other_players if p.name != self.name}

    def print_status(self) -> None:
        pass

    def process_penalty(self) -> bool:
        hit = self.current_bullet_position == self.bullet_position
        self.current_bullet_position = (self.current_bullet_position + 1) % 6
        if hit:
            self.alive = False
        return self.alive

    def reflect(self, **kwargs) -> None:
        """bot 不做反思。上游每小局花 12 次 LLM 调用来做这件事——
        这个实验（reflect 值不值）需要真实模型，bot 这边直接跳过。"""
        return None

    def choose_cards_to_play(self, round_base_info="", round_action_info="", play_decision_info=""):
        target = _target_from_info(round_base_info)
        legal = [c for c in self.hand if c == target or c == "Joker"]

        if legal:
            cards = [legal[0]]
        else:
            # 没有真牌，只能诈唬。出掉「最没用」的一张：非目标牌里优先出
            # 手上张数最多的那种，保留稀缺牌。
            counts = {c: self.hand.count(c) for c in set(self.hand)}
            bluff_card = max(counts, key=lambda c: (counts[c], c == "Joker"))
            cards = [bluff_card]

        for card in cards:
            self.hand.remove(card)

        self.stats.plays += 1
        self.stats.bluffs += any(c != target and c != "Joker" for c in cards)
        self.stats.play_log.append({"target": target, "cards": cards, "hand_after": list(self.hand)})

        return {
            "played_cards": cards,
            "behavior": "面无表情地推出一张牌。",
            "play_reason": "bot 出法：优先真牌，无真牌则诈唬一张。",
        }, ""

    def decide_challenge(self, round_base_info="", round_action_info="",
                         challenge_decision_info="", challenging_player_performance="",
                         extra_hint="") -> tuple[dict, str]:
        target = _target_from_info(round_base_info)
        play_count = _last_play_count(challenging_player_performance)
        actor_gun = _gun_from_info(challenge_decision_info, self.name)["actor"]

        # 造假一个 Decision 走同一套先验查表，保证 bot 与离线分析用同一把尺
        pseudo = Decision(
            game_id="live", round_id=0, target=target, actor="?", challenger=self.name,
            played=tuple([target] * play_count), bluff=False, challenged=False,
            challenge_ok=None, actor_gun=actor_gun, challenger_gun=self.current_bullet_position,
            challenger_hand=tuple(self.hand), actor_hand_size=5, round_players=4, play_index=0,
        )
        p_bluff = lookup_prior(bluff_prior_table(), pseudo)
        threshold = breakeven_bluff_probability(self.current_bullet_position, actor_gun)
        decision = p_bluff > threshold

        self.stats.challenge_opportunities += 1
        self.stats.challenges += decision
        return {
            "was_challenged": decision,
            "challenge_reason": f"p_bluff={p_bluff:.3f} vs 阈值={threshold:.3f}",
        }, ""


_GUN_LINE = re.compile(r"你已经开了(\d+)枪，.+?开了(\d+)枪")
_PLAY_COUNT = re.compile(r"宣称打出(\d+)张")
_TARGET = re.compile(r"目标牌：([QKA])")


def _target_from_info(round_base_info: str) -> str:
    """从「现在是第1轮，目标牌：Q，…」里抠出目标牌。"""
    return _TARGET.search(round_base_info).group(1)


def _last_play_count(behavior_text: str) -> int:
    """「Player_A宣称打出2张'Q'，剩余手牌1张」→ 2。"""
    return int(_PLAY_COUNT.search(behavior_text).group(1))


def _gun_from_info(info_text: str, self_name: str) -> dict[str, int]:
    """「你已经开了2枪，Player_A开了0枪。你对Player_A的印象分析：…」→ {'self': 2, 'actor': 0}

    印象分析里可能有用户或模型写出的「。」，所以不能用 split 切句。
    """
    match = _GUN_LINE.search(info_text)
    return {"self": int(match.group(1)), "actor": int(match.group(2))}


if __name__ == "__main__":
    from analysis.records import load_all

    d, _, _ = load_all()
    table = empirical_table(d)
    print(f"经验先验表：{len(table)} 个分组")
    for key in sorted(table)[:8]:
        print(key, round(table[key], 3))
