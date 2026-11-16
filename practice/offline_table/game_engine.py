"""Rules and state for a four-player, offline Liar's Bar table.

This module has no terminal or web input/output. A caller submits one action at
a time and asks for a view filtered to one player's private information.
"""

from collections import Counter
import random


SEATS = ("P1", "P2", "P3", "P4")
TARGET_CARDS = ("Q", "K", "A")
ALL_CARDS = (*TARGET_CARDS, "Joker")


class GameError(ValueError):
    """An action is invalid for the current game state."""


class Player:
    def __init__(self, name, rng):
        self.name = name
        self.hand = []
        self.alive = True
        self.bullet_position = rng.randint(0, 5)
        self.current_bullet_position = 0

    def shoot(self):
        hit = self.current_bullet_position == self.bullet_position
        self.current_bullet_position = (self.current_bullet_position + 1) % 6
        if hit:
            self.alive = False
        return "dead" if hit else "blank"


class GameEngine:
    """One game. Only the server should own this object and its hidden state."""

    def __init__(self, rng=None):
        self.rng = rng if rng is not None else random.Random()
        self.players = [Player(name, self.rng) for name in SEATS]
        self.round = 0
        self.phase = "play"
        self.target_card = None
        self.deck = []
        self.current_player = None
        self.challenger = None
        self.forced_challenge = False
        self.last_play = None
        self.result = None
        self.winner = None
        self.notice = ""
        self._start_round()

    def _player(self, seat):
        for player in self.players:
            if player.name == seat:
                return player
        raise GameError("玩家席位不存在")

    def _next_player_with_cards(self, current_seat):
        start = SEATS.index(current_seat)
        for offset in range(1, len(SEATS)):
            player = self.players[(start + offset) % len(SEATS)]
            if player.alive and player.hand:
                return player
        return None

    def _start_round(self):
        alive = [player for player in self.players if player.alive]
        if len(alive) < 2:
            raise GameError("存活玩家不足，无法开始新一局")

        self.deck = ["Q"] * 6 + ["K"] * 6 + ["A"] * 6 + ["Joker"] * 2
        self.rng.shuffle(self.deck)
        for player in self.players:
            player.hand.clear()
        for _ in range(5):
            for player in alive:
                player.hand.append(self.deck.pop())

        self.round += 1
        self.target_card = self.rng.choice(TARGET_CARDS)
        self.current_player = alive[0].name
        self.challenger = None
        self.forced_challenge = False
        self.last_play = None
        self.result = None
        self.phase = "play"
        self.notice = f"第 {self.round} 局开始，目标牌是 {self.target_card}"

    def play(self, actor, cards):
        if self.phase != "play":
            raise GameError("现在不是出牌阶段")
        if actor != self.current_player:
            raise GameError("现在不是你的出牌回合")
        if not isinstance(cards, list) or not 1 <= len(cards) <= 3:
            raise GameError("每次需要出 1 到 3 张牌")
        if any(not isinstance(card, str) or card not in ALL_CARDS for card in cards):
            raise GameError("提交的牌面无效")

        player = self._player(actor)
        if Counter(cards) - Counter(player.hand):
            raise GameError("提交的牌不在你的手牌中，或数量超过手牌")
        if self._next_player_with_cards(actor) is None:
            raise GameError("没有其他持牌玩家可以质疑")

        for card in cards:
            player.hand.remove(card)

        challenger = self._next_player_with_cards(actor)
        holders = [p for p in self.players if p.alive and p.hand]
        self.last_play = {"player": actor, "count": len(cards), "cards": cards.copy()}
        self.challenger = challenger.name
        self.forced_challenge = len(holders) == 1
        self.phase = "challenge"
        self.result = None
        if self.forced_challenge:
            self.notice = f"{actor} 打出了 {len(cards)} 张牌；{self.challenger} 是唯一持牌者，必须质疑"
        else:
            self.notice = f"{actor} 打出了 {len(cards)} 张牌，等待 {self.challenger} 决定是否质疑"

    def challenge(self, actor, decision):
        if self.phase != "challenge":
            raise GameError("现在不是质疑阶段")
        if actor != self.challenger:
            raise GameError("现在不是你决定质疑")
        if type(decision) is not bool:
            raise GameError("质疑决定必须是 true 或 false")
        if self.forced_challenge and not decision:
            raise GameError("你是唯一持牌者，必须质疑上一手")

        played_by = self.last_play["player"]
        cards = self.last_play["cards"]
        if not decision:
            self.current_player = actor
            self.challenger = None
            self.forced_challenge = False
            self.last_play = None
            self.phase = "play"
            self.notice = f"{actor} 没有质疑，轮到 {actor} 出牌"
            return

        valid = all(card == self.target_card or card == "Joker" for card in cards)
        penalty_seat = actor if valid else played_by
        shot = self._player(penalty_seat).shoot()
        self.result = {
            "cards": cards.copy(),
            "valid": valid,
            "penalty_player": penalty_seat,
            "shot": shot,
        }
        self.challenger = None
        self.forced_challenge = False
        self.last_play = None
        self.current_player = None

        alive = [player for player in self.players if player.alive]
        if len(alive) == 1:
            self.winner = alive[0].name
            self.phase = "over"
            self.notice = f"{penalty_seat} 中枪，{self.winner} 获胜"
        else:
            self.phase = "result"
            outcome = "中枪出局" if shot == "dead" else "空枪幸存"
            self.notice = f"{penalty_seat} 受罚：{outcome}。本小局结束"

    def advance(self):
        if self.phase != "result":
            raise GameError("现在不能进入下一局")
        self._start_round()

    def view(self, viewer):
        """Return only public data and this viewer's own cards."""
        player = self._player(viewer)
        return {
            "phase": self.phase,
            "round": self.round,
            "target_card": self.target_card,
            "current_player": self.current_player,
            "challenger": self.challenger,
            "last_play": (
                {"player": self.last_play["player"], "count": self.last_play["count"]}
                if self.last_play else None
            ),
            "forced_challenge": self.forced_challenge,
            "hand": player.hand.copy() if player.alive else [],
            "seats": [
                {
                    "id": seat.name,
                    "alive": seat.alive,
                    "card_count": len(seat.hand),
                    "joined": True,
                }
                for seat in self.players
            ],
            "notice": self.notice,
            "result": self.result.copy() if self.result else None,
            "winner": self.winner,
            "can": {
                "start": False,
                "play": self.phase == "play" and viewer == self.current_player,
                "challenge": self.phase == "challenge" and viewer == self.challenger,
                "advance": self.phase == "result",
                "restart": self.phase == "over" and viewer == "P1",
            },
        }
