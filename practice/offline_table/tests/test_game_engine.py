"""Behavioral checks for the offline game engine (no API calls)."""

from collections import Counter
import json
from pathlib import Path
import random
import sys
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from game_engine import GameEngine, GameError  # noqa: E402


class GameEngineTests(unittest.TestCase):
    def setUp(self):
        self.game = GameEngine(rng=random.Random(20261015))

    def player(self, name):
        return next(player for player in self.game.players if player.name == name)

    def assert_rejected_without_change(self, action):
        before = (
            self.game.phase,
            self.game.round,
            self.game.forced_challenge,
            tuple((p.name, tuple(p.hand), p.alive, p.current_bullet_position) for p in self.game.players),
        )
        with self.assertRaises(GameError):
            action()
        after = (
            self.game.phase,
            self.game.round,
            self.game.forced_challenge,
            tuple((p.name, tuple(p.hand), p.alive, p.current_bullet_position) for p in self.game.players),
        )
        self.assertEqual(after, before)

    def set_up_last_card(self, played_card):
        """Leave only the current player and the next player holding cards."""
        actor = self.game.current_player
        names = [player.name for player in self.game.players]
        challenger = names[(names.index(actor) + 1) % len(names)]
        self.assertNotEqual(actor, challenger)
        for player in self.game.players:
            player.hand.clear()
            player.bullet_position = 5  # Position zero is an empty chamber.
            player.current_bullet_position = 0
        self.player(actor).hand.append(played_card)
        self.player(challenger).hand.append("Joker")
        return actor, challenger

    def test_initial_deck_distribution_and_target(self):
        self.assertEqual([p.name for p in self.game.players], ["P1", "P2", "P3", "P4"])
        self.assertTrue(all(p.alive and len(p.hand) == 5 for p in self.game.players))
        all_cards = [card for player in self.game.players for card in player.hand]
        self.assertEqual(
            Counter(all_cards),
            Counter({"Q": 6, "K": 6, "A": 6, "Joker": 2}),
        )
        self.assertIn(self.game.target_card, {"Q", "K", "A"})
        self.assertEqual(self.game.round, 1)

    def test_play_rejects_empty_too_many_and_cards_not_owned(self):
        actor = self.game.current_player
        hand = self.player(actor).hand
        self.assert_rejected_without_change(lambda: self.game.play(actor, []))
        self.assert_rejected_without_change(lambda: self.game.play(actor, [hand[0]] * 4))
        self.assert_rejected_without_change(lambda: self.game.play(actor, ["X"]))

    def test_play_rejects_duplicate_beyond_hand_quantity(self):
        # Find a seeded, ordinary hand containing one or two copies of a card.
        for seed in range(20):
            self.game = GameEngine(rng=random.Random(seed))
            actor = self.game.current_player
            counts = Counter(self.player(actor).hand)
            candidate = next(((card, count) for card, count in counts.items() if count < 3), None)
            if candidate is not None:
                break
        else:
            self.fail("Could not construct a hand for the duplicate-card check")
        card, owned = candidate
        self.assert_rejected_without_change(
            lambda: self.game.play(actor, [card] * (owned + 1))
        )

    def test_forced_challenge_of_truthful_last_card_penalizes_challenger(self):
        actor, challenger = self.set_up_last_card(self.game.target_card)
        self.game.play(actor, [self.game.target_card])
        self.assertTrue(self.game.forced_challenge)
        self.assertEqual(self.game.challenger, challenger)
        self.assert_rejected_without_change(lambda: self.game.challenge(challenger, False))
        self.game.challenge(challenger, True)
        self.assertEqual(self.player(challenger).current_bullet_position, 1)
        self.assertEqual(self.player(actor).current_bullet_position, 0)

    def test_forced_challenge_of_bluff_penalizes_player_who_played(self):
        bluff = next(card for card in ("Q", "K", "A") if card != self.game.target_card)
        actor, challenger = self.set_up_last_card(bluff)
        self.game.play(actor, [bluff])
        self.assertTrue(self.game.forced_challenge)
        self.game.challenge(challenger, True)
        self.assertEqual(self.player(actor).current_bullet_position, 1)
        self.assertEqual(self.player(challenger).current_bullet_position, 0)

    def test_empty_chamber_ends_round_and_redeals(self):
        actor, challenger = self.set_up_last_card(self.game.target_card)
        self.game.play(actor, [self.game.target_card])
        self.game.challenge(challenger, True)
        self.assertTrue(self.player(challenger).alive)
        self.assertEqual(self.player(challenger).current_bullet_position, 1)
        self.game.advance()
        self.assertEqual(self.game.round, 2)
        self.assertIsNone(self.game.result)
        self.assertTrue(all(len(p.hand) == 5 for p in self.game.players))
        self.assertFalse(self.game.forced_challenge)

    def test_loaded_chamber_kills_player_then_redeals_survivors(self):
        actor, challenger = self.set_up_last_card(self.game.target_card)
        self.player(challenger).bullet_position = 0
        self.game.play(actor, [self.game.target_card])
        self.game.challenge(challenger, True)
        self.assertFalse(self.player(challenger).alive)
        self.game.advance()
        self.assertEqual(self.game.round, 2)
        self.assertEqual(self.player(challenger).hand, [])
        self.assertEqual(len([p for p in self.game.players if p.alive]), 3)
        self.assertTrue(all(len(p.hand) == 5 for p in self.game.players if p.alive))

    def test_view_shows_own_hand_but_not_other_hands_or_hidden_bullet(self):
        viewer = self.game.current_player
        other = next(p for p in self.game.players if p.name != viewer)
        other.hand[:] = ["PRIVATE_HAND_SENTINEL_293847"]
        state = self.game.view(viewer)
        self.assertEqual(state["hand"], self.player(viewer).hand)
        encoded = json.dumps(state, ensure_ascii=False)
        self.assertNotIn("PRIVATE_HAND_SENTINEL_293847", encoded)
        self.assertNotIn('"bullet_position"', encoded)

    def test_out_of_turn_and_repeated_actions_are_rejected(self):
        actor = self.game.current_player
        names = [player.name for player in self.game.players]
        challenger = names[(names.index(actor) + 1) % len(names)]
        card = self.player(actor).hand[0]
        self.assert_rejected_without_change(
            lambda: self.game.play(challenger, [self.player(challenger).hand[0]])
        )
        self.assert_rejected_without_change(lambda: self.game.advance())
        self.game.play(actor, [card])
        self.assert_rejected_without_change(lambda: self.game.play(actor, [card]))
        self.assert_rejected_without_change(lambda: self.game.challenge(actor, True))
        self.game.challenge(challenger, True)
        self.assert_rejected_without_change(lambda: self.game.challenge(challenger, True))


if __name__ == "__main__":
    unittest.main()
