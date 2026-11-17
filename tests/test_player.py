"""动作验证、原子手牌更新及错误分类；不发出真实模型请求。"""

import json
import unittest
from unittest.mock import patch

from player import ModelOutputError, Player, parse_action


class StubClient:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = 0
        self.snapshots = []
        self.player = None

    def chat(self, messages, model):
        self.calls += 1
        self.snapshots.append(list(self.player.hand))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response, "reasoning"


def play(cards, **overrides):
    payload = dict(played_cards=cards, behavior="平静", play_reason="测试")
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


class PlayerActionTests(unittest.TestCase):
    def make_player(self, responses, hand=("Q", "K")):
        client = StubClient(responses)
        player = Player("test", "stub", llm_client=client)
        player.hand = list(hand)
        client.player = player
        self.addCleanup(patch.stopall)
        patch.object(player, "_read_file", return_value="").start()
        return player, client

    def test_duplicate_cards_cannot_partially_consume_hand(self):
        player, client = self.make_player([play(["Q", "Q"])] * 5)
        original = player.hand
        with self.assertRaises(RuntimeError):
            player.choose_cards_to_play("", "", "")
        self.assertIs(player.hand, original)
        self.assertEqual(player.hand, ["Q", "K"])
        self.assertEqual(client.snapshots, [["Q", "K"]] * 5)
        self.assertEqual(player.format_failures["choose_cards_to_play"], 5)

    def test_invalid_then_valid_action_commits_once(self):
        player, client = self.make_player([play(["Q", "Q"]), play(["K"])])
        result, reasoning = player.choose_cards_to_play("", "", "")
        self.assertEqual(client.snapshots, [["Q", "K"], ["Q", "K"]])
        self.assertEqual(player.hand, ["Q"])
        self.assertEqual(result["played_cards"], ["K"])
        self.assertEqual(reasoning, "reasoning")
        self.assertEqual(player.format_failures["choose_cards_to_play"], 1)

    def test_multiple_owned_duplicates_are_legal(self):
        player, _ = self.make_player([play(["Q", "Q"])], hand=("Q", "Q", "K"))
        player.choose_cards_to_play("", "", "")
        self.assertEqual(player.hand, ["K"])

    def test_schema_rejects_coercion_and_non_string_reasons(self):
        invalid = [play("Q"), play([1]), play(["Q"], behavior={}),
                   play(["Q"], play_reason=None),
                   '{"was_challenged":1,"challenge_reason":"x"}',
                   '{"was_challenged":true,"challenge_reason":[]}', "not json"]
        for content in invalid:
            with self.subTest(content=content), self.assertRaises(ModelOutputError):
                parse_action(content, "challenge" if "was_challenged" in content else "play")

    def test_only_a_single_object_or_fenced_object_is_an_action(self):
        good = '{"was_challenged":false,"challenge_reason":"x"}'
        self.assertFalse(parse_action("```json\n" + good + "\n```", "challenge")["was_challenged"])
        for content in ("[" + good + "]", good + good, "prefix " + good):
            with self.subTest(content=content), self.assertRaises(ModelOutputError):
                parse_action(content, "challenge")

    def test_challenge_failure_count_tracks_each_invalid_response(self):
        player, client = self.make_player([
            '{"was_challenged":"false","challenge_reason":"x"}',
            '{"was_challenged":false,"challenge_reason":"x"}',
        ])
        result, _ = player.decide_challenge("", "", "", "", "")
        self.assertIs(result["was_challenged"], False)
        self.assertEqual(client.calls, 2)
        self.assertEqual(player.format_failures["decide_challenge"], 1)

    def test_network_errors_propagate_without_format_retry_or_state_change(self):
        for action in ("play", "challenge"):
            with self.subTest(action=action):
                player, client = self.make_player([ConnectionError("offline")])
                with self.assertRaises(ConnectionError):
                    if action == "play":
                        player.choose_cards_to_play("", "", "")
                    else:
                        player.decide_challenge("", "", "", "", "")
                self.assertEqual(client.calls, 1)
                self.assertEqual(sum(player.format_failures.values()), 0)
                self.assertEqual(player.hand, ["Q", "K"])


if __name__ == "__main__":
    unittest.main()
