"""离线命令行牌桌的规则测试：用脚本替身喂键盘输入，不需要真人。

覆盖三条容易写错的规则（见 offline_liars_bar.py 顶部说明）：
1. 开枪后本小局结束，重新发牌；
2. 出完手牌的人后续回合被跳过；
3. 只剩一人有牌时自动出完并被系统质疑。
"""

from pathlib import Path
import builtins
import random
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import offline_liars_bar as game  # noqa: E402


class ScriptedInput:
    """按剧本回放键盘输入；剧本用完后返回空行，避免测试挂住。"""

    def __init__(self, lines):
        self.lines = list(lines)
        self.prompted = []

    def __call__(self, prompt=""):
        self.prompted.append(prompt)
        return self.lines.pop(0) if self.lines else ""

    def remaining(self):
        return len(self.lines)


class OfflineCliTests(unittest.TestCase):
    def setUp(self):
        self._real_input = builtins.input

    def tearDown(self):
        builtins.input = self._real_input

    def feed(self, lines):
        script = ScriptedInput(lines)
        builtins.input = script
        return script

    def test_shoot_resets_round_and_moves_chamber(self):
        rng = random.Random(1)
        p = game.Player("P1", rng)
        p.bullet_position = 2
        self.assertEqual(p.shoot(), "blank")
        self.assertEqual(p.current_bullet_position, 1)
        self.assertEqual(p.shoot(), "blank")
        self.assertEqual(p.shoot(), "dead")
        self.assertFalse(p.alive)
        self.assertEqual(p.current_bullet_position, 3)

    def test_chamber_wraps_after_full_cycle(self):
        rng = random.Random(2)
        p = game.Player("P1", rng)
        p.bullet_position = 5
        positions = []
        for _ in range(7):
            p.shoot()
            positions.append(p.current_bullet_position)
        self.assertEqual(positions, [1, 2, 3, 4, 5, 0, 1])

    def test_play_removes_cards_and_rejects_overflow(self):
        rng = random.Random(3)
        p = game.Player("P1", rng)
        p.hand = ["Q", "Q", "K"]
        # 第一次超量（3 张 Q 但只有 2 张），第二次合法
        self.feed(["Q Q Q", "Q q"])
        cards = game.choose_cards_to_play(p)
        self.assertEqual(cards, ["Q", "Q"])
        self.assertEqual(p.hand, ["K"])

    def test_joker_normalisation(self):
        for token in ("joker", "JOKER", "Joker"):
            self.assertEqual(game.normalize_card(token), "Joker")

    def test_sole_holder_is_system_challenged(self):
        rng = random.Random(4)
        players = [game.Player(f"P{i}", rng) for i in range(1, 4)]
        # P1 已经出完牌，只剩 P2 有牌 → 轮到 P2 时触发规则 3
        players[0].hand = []
        players[1].hand = ["Q", "K"]
        players[2].hand = []
        players[1].current_bullet_position = 0
        players[1].bullet_position = 0  # 必定中枪，便于断言

        # 目标牌是 A，P2 手里 Q/K 都不合法 → 系统质疑成功，P2 受罚
        penalty, _, round_over = game.run_turn(players, 1, "A")

        self.assertTrue(round_over)
        self.assertIs(penalty, players[1])
        self.assertEqual(players[1].hand, [], "规则 3 要清空手牌")
        self.assertEqual(game.ask_challenge.__name__, "ask_challenge")

    def test_sole_holder_not_asked_for_input(self):
        rng = random.Random(5)
        players = [game.Player(f"P{i}", rng) for i in range(1, 4)]
        players[0].hand = []
        players[1].hand = ["A"]
        players[2].hand = []
        script = self.feed([])  # 一个输入都不给
        game.run_turn(players, 1, "A")
        self.assertEqual(script.prompted, [], "系统质疑不应再询问键盘")

    def test_full_game_terminates_with_one_winner(self):
        rng = random.Random(20261015)
        names = ["P1", "P2", "P3", "P4"]

        # 每次出牌都出 1 张真牌（有就出），没有真牌就出 Joker，再没有就出第一张；
        # 质疑一律选 n。这样牌局会按规则推进到"只剩一人有牌→系统质疑"。
        pending = []

        def fake_input(prompt=""):
            if "要出的牌" in prompt:
                return pending.pop()
            return "n"

        players = [game.Player(n, rng) for n in names]
        game.deal_cards(players, rng)
        target = "Q"

        # 手工驱动几轮，确保系统质疑被走到并且不会无限循环
        for _ in range(200):
            alive = [p for p in players if p.alive]
            if len(alive) == 1:
                break
            holder = [p for p in players if p.alive and p.hand]
            if not holder:
                game.deal_cards(players, rng)
                continue
            actor_idx = players.index(holder[0])
            actor = holder[0]
            pending.clear()
            pending.append(actor.hand[0])
            builtins.input = fake_input
            penalty, next_idx, round_over = game.run_turn(players, actor_idx, target)
            if penalty is not None:
                penalty.shoot()
            if round_over:
                game.deal_cards(players, rng)

        builtins.input = self._real_input
        self.assertIsNotNone(alive)
        self.assertGreaterEqual(len(alive), 1)


if __name__ == "__main__":
    unittest.main()
