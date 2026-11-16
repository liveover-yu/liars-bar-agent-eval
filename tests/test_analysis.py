"""评估层的单元测试：不调 API，全部基于 demo_records 和手造样本。"""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analysis import metrics as M  # noqa: E402
from analysis.calibration import (  # noqa: E402
    empirical_table,
    evaluate_policy,
    lookup_prior,
    never_challenge,
    Policy,
)
from analysis.probe import make_cases, render_prompt  # noqa: E402
from analysis.records import Decision, load_all  # noqa: E402
from analysis.risk import breakeven_bluff_probability, challenge_ev, hit_probability  # noqa: E402
from analysis.stats import wilson  # noqa: E402


def make_decision(**overrides) -> Decision:
    base = dict(
        game_id="g1", round_id=1, target="Q", actor="A", challenger="B",
        played=("Q",), bluff=False, challenged=False, challenge_ok=None,
        actor_gun=0, challenger_gun=0, challenger_hand=("K", "K", "K", "K", "K"),
        actor_hand_size=4, round_players=4, play_index=0,
    )
    base.update(overrides)
    return Decision(**base)


class RiskTests(unittest.TestCase):
    def test_hit_probability_is_reciprocal(self):
        self.assertAlmostEqual(hit_probability(0), 1 / 6)
        self.assertAlmostEqual(hit_probability(4), 1 / 2)
        self.assertAlmostEqual(hit_probability(5), 1.0)

    def test_breakeven_symmetric_at_equal_guns(self):
        for g in range(6):
            self.assertAlmostEqual(breakeven_bluff_probability(g, g), 0.5)

    def test_breakeven_rises_as_challenger_approaches_bullet(self):
        values = [breakeven_bluff_probability(g, 0) for g in range(6)]
        self.assertEqual(values, sorted(values))
        self.assertGreater(values[-1], 0.8)

    def test_challenge_ev_sign_follows_truth(self):
        self.assertGreater(challenge_ev(1.0, 0, 0), 0)
        self.assertLess(challenge_ev(0.0, 0, 0), 0)
        self.assertAlmostEqual(challenge_ev(0.5, 0, 0), 0.0)


class WilsonTests(unittest.TestCase):
    def test_interval_contains_point_estimate(self):
        for k, n in [(0, 10), (5, 10), (10, 10), (22, 100)]:
            lo, hi = wilson(k, n)
            self.assertLessEqual(lo, k / n)
            self.assertGreaterEqual(hi, k / n)

    def test_zero_trials_is_uninformative(self):
        self.assertEqual(wilson(0, 0), (0.0, 1.0))


class RecordsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.decisions, cls.systems, cls.meta = load_all()

    def test_corpus_shape(self):
        self.assertEqual(len(self.meta["games"]), 50)
        self.assertEqual(len(self.decisions) + len(self.systems), 1101)
        self.assertEqual(len(self.systems), 1)

    def test_bluff_matches_challenge_result(self):
        """每一手被质疑的出牌，bluff 标记都要和 challenge_result 一致。"""
        for d in self.decisions:
            if d.challenged:
                self.assertEqual(d.bluff, d.challenge_ok, d)

    def test_challenger_hand_is_not_empty(self):
        for d in self.decisions:
            self.assertGreater(len(d.challenger_hand), 0, d)

    def test_rates_are_in_unit_interval(self):
        for rate in (M.bluff_rate(self.decisions), M.challenge_rate(self.decisions)):
            self.assertGreaterEqual(rate.p, 0.0)
            self.assertLessEqual(rate.p, 1.0)
            self.assertLessEqual(rate.lo, rate.p)
            self.assertGreaterEqual(rate.hi, rate.p)

    def test_gun_groups_partition_decisions(self):
        groups = M.by_challenger_gun(self.decisions)
        self.assertEqual(sum(r.n for r in groups.values()), len(self.decisions))


class CalibrationTests(unittest.TestCase):
    def test_never_challenge_policy_has_zero_ev(self):
        decisions = [make_decision(bluff=True), make_decision(bluff=False)]
        result = evaluate_policy(Policy("never", never_challenge), decisions)
        self.assertEqual(result.challenges, 0)
        self.assertEqual(result.ev_per_100, 0.0)

    def test_always_challenge_accuracy_equals_bluff_rate(self):
        decisions = [
            make_decision(bluff=True),
            make_decision(bluff=False),
            make_decision(bluff=False),
            make_decision(bluff=False),
        ]
        result = evaluate_policy(Policy("always", lambda d: True), decisions)
        self.assertEqual(result.accuracy, 0.25)
        self.assertEqual(result.recall, 1.0)

    def test_ev_accounting_is_asymmetric_in_guns(self):
        """质疑成功时对手挨枪，命中率按对手枪膛算；失败按自己枪膛算。"""
        win = make_decision(bluff=True, actor_gun=4, challenger_gun=0)
        loss = make_decision(bluff=False, actor_gun=0, challenger_gun=4)
        result = evaluate_policy(Policy("always", lambda d: True), [win, loss])
        self.assertAlmostEqual(result.ev_total, 0.5 - 0.5, places=9)

    def test_empirical_table_holdout_excludes_game(self):
        decisions = [
            make_decision(game_id="g1", bluff=True),
            make_decision(game_id="g2", bluff=False),
        ]
        table = empirical_table(decisions, holdout_game="g1")
        # 只剩 g2 的诚实样本；Laplace 平滑后先验应当偏向「不诈唬」
        p = lookup_prior(table, make_decision(game_id="g1"))
        self.assertLess(p, 0.5)

    def test_lookup_prior_falls_back(self):
        table = empirical_table([make_decision(bluff=True) for _ in range(10)])
        unseen = make_decision(played=("Q", "Q", "Q"), challenger_gun=5, actor_hand_size=5)
        self.assertTrue(0.0 < lookup_prior(table, unseen) < 1.0)


class ProbeTests(unittest.TestCase):
    def test_cases_cover_every_gun_state(self):
        from analysis.probe import BOARDS

        cases = make_cases()
        self.assertEqual(len(cases), len(BOARDS) * 6)
        self.assertEqual({c.challenger_gun for c in cases}, set(range(6)))
        self.assertEqual({c.board for c in cases}, set(BOARDS))

    def test_boards_are_self_consistent(self):
        """出牌者这一手之前必须是 5 张——一局开局每人发 5 张。"""
        for case in make_cases():
            self.assertEqual(case.actor_play_count + case.actor_remaining, 5)
            self.assertGreaterEqual(case.actor_remaining, 0)

    def test_new_cases_span_the_breakeven_threshold(self):
        """每个枪膛位都要同时有「该质疑」和「不该质疑」的牌面，否则测不出梯度。"""
        for gun in range(6):
            verdicts = {c.rational for c in make_cases() if c.challenger_gun == gun}
            self.assertEqual(verdicts, {True, False}, f"gun{gun} 没有翻转")

    def test_prompt_contains_private_hand_and_behaviour(self):
        case = make_cases()[0]
        prompt = render_prompt(case)
        self.assertIn("你现在的手牌是", prompt)
        self.assertIn("Player_A", prompt)
        self.assertIn("was_challenged", prompt)

    def test_neutral_rule_base_removes_threat_sentence(self):
        case = make_cases()[0]
        threat = render_prompt(case, rule_base="threat")
        neutral = render_prompt(case, rule_base="neutral")
        self.assertIn("删除代码", threat)
        self.assertNotIn("删除代码", neutral)
        # 除了那一行，其余规则必须完全一致——否则消融实验混了别的变量
        self.assertEqual(
            threat.split("\n", 1)[1].split("任务：")[1],
            neutral.split("\n", 1)[1].split("任务：")[1],
        )

    def test_probe_cases_are_answerable(self):
        """每个牌局都要有唯一正确答案，否则不能拿来判分。"""
        for case in make_cases():
            self.assertIsInstance(case.rational, bool)
            self.assertAlmostEqual(
                case.margin, case.bluff_floor - case.breakeven, places=12
            )
            self.assertEqual(case.rational, case.bluff_floor >= case.breakeven)


if __name__ == "__main__":
    unittest.main()
