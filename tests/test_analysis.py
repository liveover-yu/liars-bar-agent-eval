"""评估层的单元测试：不调 API，全部基于 demo_records 和手造样本。"""

from pathlib import Path
import sys
import unittest
import io
import json
import math
import tempfile
from contextlib import redirect_stdout
from itertools import combinations
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analysis import metrics as M  # noqa: E402
from analysis.calibration import (  # noqa: E402
    empirical_table,
    evaluate_policy,
    lookup_prior,
    never_challenge,
    Policy,
    hand_size_bucket,
    naive_bluff_prior,
)
from analysis import probe  # noqa: E402
from analysis.probe import make_cases, render_prompt, build_round_record  # noqa: E402
from analysis.records import Decision, load_all  # noqa: E402
from analysis.risk import breakeven_bluff_probability, challenge_ev, hit_probability, forced_bluff_floor  # noqa: E402
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
    def test_actor_hand_size_is_already_before_play(self):
        d = make_decision(actor_hand_size=5, played=("Q", "Q", "Q"),
                          challenger_hand=("Q", "Q", "Q", "Joker", "Joker"))
        # 未见 15 张中 3 张合法牌；诚实要求 5 张手牌包含全部 3 张。
        expected = 1 - math.comb(12, 2) / math.comb(15, 5)
        self.assertAlmostEqual(naive_bluff_prior(d), expected)
        self.assertEqual(hand_size_bucket(make_decision(actor_hand_size=2)), 2)

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

    def test_reference_probability_matches_enumerated_synthetic_deals(self):
        """独立枚举所有可能手牌，验证外生张数/诚实优先生成器下的概率。"""
        case = next(c for c in make_cases() if c.board == "QQJ_3")
        unseen = ["Q"] * 3 + ["K"] * 6 + ["A"] * 6
        forced = sum(sum(unseen[i] == "Q" for i in indices) < 3
                     for indices in combinations(range(15), 5))
        self.assertAlmostEqual(case.bluff_probability, forced / math.comb(15, 5))
        self.assertTrue(case.reference_decision)

    def test_reference_only_claims_conditional_single_step_optimality(self):
        for case in make_cases():
            prompt = render_prompt(case)
            self.assertIn("发牌前外生固定", prompt)
            self.assertIn("独立于所有玩家的手牌", prompt)
            self.assertIn("不会主动诈唬", prompt)
            self.assertIn("无额外信号", prompt)
            self.assertIn("不质疑的单步效用为 0", prompt)
            self.assertIn("不代表真实博弈或整局最优策略", prompt)
            self.assertIn(probe.SUITE_VERSION, prompt)
            self.assertIsInstance(case.reference_decision, bool)
            self.assertAlmostEqual(
                case.margin, case.bluff_floor - case.breakeven, places=12
            )
            self.assertEqual(case.rational, case.bluff_floor >= case.breakeven)
            self.assertEqual(probe.reference_decision(case), case.rational)

    def test_round_and_bullet_positions_are_physically_consistent(self):
        for case in make_cases():
            record = build_round_record(case, round_id=1)
            self.assertGreaterEqual(record.round_id, 1 + case.actor_gun + case.challenger_gun)
            for state in record.player_initial_states:
                self.assertGreaterEqual(state.bullet_position, state.current_gun_position)
                if state.player_name not in (case.actor, case.challenger):
                    self.assertEqual(state.current_gun_position, 0)

    def test_impossible_card_counts_are_rejected(self):
        with self.assertRaises(ValueError):
            forced_bluff_floor(["Joker"] * 3, "Q", 1, 5)


class ProbeResumeTests(unittest.TestCase):
    def record(self):
        case = make_cases()[0]
        prompt = render_prompt(case)
        tag = probe._log_tag(case, "threat", 0, "model-A", 9, 0.0, prompt)
        return dict(tag=tag, model="model-A", seed=9, temperature=0.0,
                    messages=[{"role": "user", "content": prompt}],
                    content='{"was_challenged":false,"challenge_reason":"reference"}')

    def test_resume_matches_model_seed_prompt_and_suite(self):
        record = self.record()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "log.jsonl"
            for changed in ({"model": "model-B"}, {"seed": 10}, {"temperature": 0.5},
                            {"tag": "probe:threat:noQ_1_gun0:0"},
                            {"messages": [{"role": "user", "content": "changed prompt"}]}):
                with self.subTest(changed=changed):
                    path.write_text(json.dumps({**record, **changed}) + "\n", encoding="utf-8")
                    self.assertEqual(probe.load_log(path), {})
            path.write_text(json.dumps(record) + "\n{unfinished", encoding="utf-8")
            self.assertEqual(probe.load_log(path), {record["tag"]: [False]})
            canonical = {**record, "requested_model": "model-A", "model": "model-A-version-2026"}
            path.write_text(json.dumps(canonical) + "\n", encoding="utf-8")
            self.assertEqual(probe.load_log(path), {record["tag"]: [False]})

    def test_full_cache_resume_does_not_initialize_paid_client(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "log.jsonl"
            path.write_text(json.dumps(self.record()) + "\n", encoding="utf-8")
            with patch("llm_client.LLMClient") as client:
                run = probe.run_probe(make_cases()[:1], "model-A", 1, log_path=path, seed=9)
            client.assert_not_called()
            self.assertEqual(run.total_calls, 0)
            self.assertEqual(run.resumed_calls, 1)
            self.assertEqual(run.by_case[make_cases()[0].probe_id], [False])

    def test_resume_preserves_broken_tail_and_separates_new_log_records(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "log.jsonl"
            path.write_text('{"partial":', encoding="utf-8")
            with patch("llm_client.LLMClient") as factory:
                factory.return_value.chat.return_value = ('{"was_challenged":false,"challenge_reason":"x"}', "")
                probe.run_probe(make_cases()[:1], "model-A", 1, log_path=path)
            self.assertEqual(path.read_bytes(), b'{"partial":\n')

    def test_one_request_per_sample_and_separate_failure_counts(self):
        with patch("llm_client.LLMClient") as factory:
            client = factory.return_value
            client.chat.side_effect = [("not json", ""), ConnectionError("offline")]
            run = probe.run_probe(make_cases()[:1], "mock-model", 2)
            self.assertEqual(client.chat.call_count, 2)
            self.assertEqual(run.format_failures, 1)
            self.assertEqual(run.api_failures, 1)
            self.assertEqual(run.total_calls, 2)
            self.assertEqual(run.by_case[make_cases()[0].probe_id], [])

    def test_cli_saves_manifest_and_result_in_new_run_directory(self):
        def fake_run(cases, model, repeats, *args):
            return probe.ProbeRun(model, repeats,
                                  by_case={c.probe_id: [c.reference_decision] for c in cases})
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "new-run"
            argv = ["probe", "--run-dir", str(run_dir), "--boards", "QQJ_3",
                    "--rule-base", "neutral", "--repeats", "1", "--seed", "17"]
            with patch.object(sys, "argv", argv), patch.object(probe, "run_probe", side_effect=fake_run), redirect_stdout(io.StringIO()):
                probe.main()
            manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
            payload = json.loads((run_dir / "probe.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["suite_version"], "probe-v2")
            self.assertEqual(manifest["repeat_seeds"], [17])
            self.assertEqual(manifest["status"], "completed")
            self.assertEqual(len(manifest["prompt_hashes"]["neutral"]), 6)
            self.assertEqual(payload["suite_version"], "probe-v2")

    def test_old_artifact_is_descriptive_history(self):
        from analysis.report import collect_probe
        payload = {"cases": [{"probe_id": "old", "rational": True}],
                   "runs": {"threat": {"model": "old-model", "repeats": 1,
                       "total_calls": 1, "format_failures": 0, "by_case": {"old": [False]}}}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            row = collect_probe([path])[0]
        self.assertFalse(row["conditional_reference"])
        self.assertIsNone(row["reference_agreement"])
        self.assertEqual(row["observed"]["p"], 0.0)


if __name__ == "__main__":
    unittest.main()
