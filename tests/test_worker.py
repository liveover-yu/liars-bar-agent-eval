"""Worker failure injection tests. No Java server or model API is needed."""

from copy import deepcopy
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eval_worker.control import ControlClient, ControlPlaneError
from eval_worker.decisions import (
    CaseEvaluator, FINGERPRINT_FILES, PreparedCase, classify_error, parse_choice, source_fingerprint,
)
from eval_worker.runner import LeaseGuard, LeaseLost, Worker


def config(**overrides):
    result = {
        "mode": "mock", "model": "mock-policy", "ruleBase": "neutral",
        "temperature": 0.0, "maxAttempts": 3, "maxCalls": 9,
        "timeoutSeconds": 5, "maxOutputTokens": 128,
    }
    result.update(overrides)
    return result


def claim(**overrides):
    result = {
        "runId": "run-1", "leaseToken": "lease-1", "leaseSeconds": 30,
        "suiteVersion": "probe-v2", "config": config(), "completedCaseIds": [],
        "cases": [
            {"caseId": "noQ_1_gun0:0", "probeId": "noQ_1_gun0", "repeatIndex": 0, "seed": 42},
            {"caseId": "noQ_1_gun1:0", "probeId": "noQ_1_gun1", "repeatIndex": 0, "seed": 43},
        ],
    }
    result.update(overrides)
    return result


def successful_outcome():
    return {
        "outcome": "SUCCESS", "retryable": False, "wasChallenged": True,
        "referenceDecision": False, "errorType": None, "errorMessage": None,
        "latencyMs": 1, "promptTokens": 0, "completionTokens": 0,
        "promptHash": "a" * 64, "modelVersion": "fake-model", "suiteVersion": "probe-v2",
        "content": '{"was_challenged":true,"challenge_reason":"test"}',
    }


class Response:
    def __init__(self, body, status=200):
        self.status = status
        self.body = json.dumps(body).encode() if body is not None else b""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self, count=None):
        return self.body


class ControlClientTests(unittest.TestCase):
    def test_transport_retry_preserves_attempt_body_and_request_id(self):
        requests = []

        def opener(request, timeout):
            requests.append(request)
            if len(requests) == 1:
                raise URLError("response lost")
            return Response({"attemptNumber": 1})

        client = ControlClient("http://localhost:8080", "test-key", opener=opener, retry_delay=0)
        payload = {"attemptId": "stable-id", "leaseToken": "lease"}
        result = client.request("POST", "/api/worker/runs/run/attempts", payload, retry=True)
        self.assertEqual(result["attemptNumber"], 1)
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0].data, requests[1].data)
        self.assertEqual(requests[0].get_header("X-request-id"), requests[1].get_header("X-request-id"))
        self.assertEqual(json.loads(requests[0].data), payload)

    def test_lease_conflict_is_not_retried(self):
        requests = []

        def opener(request, timeout):
            requests.append(request)
            raise HTTPError(request.full_url, 409, "conflict", {}, io.BytesIO(
                b'{"code":"LEASE_LOST","message":"lease expired"}'
            ))

        client = ControlClient("http://localhost:8080", "test-key", opener=opener, retry_delay=0)
        with self.assertRaises(ControlPlaneError) as caught:
            client.request("POST", "/api/worker/runs/run/heartbeat", {}, retry=True)
        self.assertEqual(caught.exception.code, "LEASE_LOST")
        self.assertEqual(len(requests), 1)

    def test_claim_connection_failure_is_not_retried(self):
        requests = []

        def opener(request, timeout):
            requests.append(request)
            raise URLError("lost")

        client = ControlClient("http://localhost:8080", "test-key", opener=opener, retry_delay=0)
        with self.assertRaises(ControlPlaneError):
            client.request("POST", "/api/worker/claims", {"workerId": "w"})
        self.assertEqual(len(requests), 1)

    def test_invalid_server_credentials_are_rejected(self):
        with self.assertRaises(ValueError):
            ControlClient("http://user:password@localhost:8080", "key")


class EvaluatorTests(unittest.TestCase):
    prepared = PreparedCase("test-probe", "test prompt", False)
    spec = {"probeId": "test-probe", "seed": 42}

    def test_mock_is_repeatable_and_never_constructs_provider(self):
        def forbidden(**kwargs):
            self.fail("mock mode constructed model client")

        evaluator = CaseEvaluator(client_factory=forbidden)
        first = evaluator.evaluate(self.prepared, config(), self.spec)
        second = evaluator.evaluate(self.prepared, config(), self.spec)
        self.assertEqual(first["wasChallenged"], second["wasChallenged"])
        self.assertEqual(first["promptTokens"], 0)
        self.assertEqual(first["completionTokens"], 0)
        self.assertEqual(first["modelVersion"], "mock-policy-v1")
        self.assertEqual(len(first["promptHash"]), 64)
        self.assertEqual(first["prompt"], self.prepared.prompt)
        self.assertEqual(len(first["workerCodeHash"]), 64)
        self.assertIsNone(first["requestId"])

    def test_llm_receives_no_hidden_retries_and_preserves_usage(self):
        options = []
        closed = []

        class FakeLLM:
            last_response = {"model": "provider-version", "request_id": "request-42", "usage": {"prompt_tokens": 11, "completion_tokens": 7}}

            def __init__(self, **kwargs):
                options.append(kwargs)

            def chat(self, messages, model):
                self_test.assertEqual(messages[0]["content"], "test prompt")
                return '{"was_challenged":false,"challenge_reason":"valid"}', ""

            def close(self):
                closed.append(True)

        self_test = self
        outcome = CaseEvaluator(client_factory=FakeLLM).evaluate(
            self.prepared, config(mode="llm", model="alias"), self.spec
        )
        self.assertEqual(options[0]["max_retries"], 0)
        self.assertEqual(options[0]["seed"], 42)
        self.assertEqual(options[0]["timeout"], 5)
        self.assertEqual(options[0]["max_output_tokens"], 128)
        self.assertEqual(outcome["modelVersion"], "provider-version")
        self.assertEqual(outcome["promptTokens"], 11)
        self.assertEqual(outcome["completionTokens"], 7)
        self.assertEqual(outcome["wasChallenged"], False)
        self.assertEqual(closed, [True])
        self.assertEqual(outcome["requestId"], "request-42")

    def test_invalid_choice_type_is_format_error_with_usage_retained(self):
        class FakeLLM:
            last_response = {"usage": {"prompt_tokens": 5, "completion_tokens": 4}}

            def __init__(self, **kwargs):
                pass

            def chat(self, messages, model):
                return '{"was_challenged":"false","challenge_reason":"invalid"}', ""

        outcome = CaseEvaluator(client_factory=FakeLLM).evaluate(
            self.prepared, config(mode="llm"), self.spec
        )
        self.assertEqual(outcome["errorType"], "FORMAT")
        self.assertTrue(outcome["retryable"])
        self.assertEqual(outcome["promptTokens"], 5)
        self.assertIsNone(outcome["wasChallenged"])

    def test_cleanup_failure_does_not_replace_a_valid_answer(self):
        class FakeLLM:
            last_response = {"usage": {"prompt_tokens": 5}}

            def __init__(self, **kwargs):
                pass

            def chat(self, messages, model):
                return '{"was_challenged":false,"challenge_reason":"valid"}', ""

            def close(self):
                raise OSError("secret-provider-message")

        with self.assertLogs("eval_worker", level="WARNING") as logs:
            outcome = CaseEvaluator(client_factory=FakeLLM).evaluate(
                self.prepared, config(mode="llm"), self.spec
            )
        self.assertEqual(outcome["outcome"], "SUCCESS")
        self.assertFalse(outcome["wasChallenged"])
        self.assertNotIn("secret-provider-message", " ".join(logs.output))

    def test_auth_error_is_terminal_and_does_not_echo_secret(self):
        class ProviderError(Exception):
            status_code = 401

        category, retryable, message = classify_error(ProviderError("secret-api-key"))
        self.assertEqual(category, "AUTH")
        self.assertFalse(retryable)
        self.assertNotIn("secret-api-key", message)

    def test_timeout_is_retryable(self):
        self.assertEqual(classify_error(TimeoutError())[0:2], ("TIMEOUT", True))

    def test_parser_accepts_fence_but_rejects_array_and_numeric_choice(self):
        self.assertFalse(parse_choice('```json\n{"was_challenged":false,"challenge_reason":"ok"}\n```'))
        for value in ('[]', '{"was_challenged":0,"challenge_reason":"bad"}'):
            with self.assertRaises(ValueError):
                parse_choice(value)

    def test_real_suite_prepare_uses_v2_public_assumptions(self):
        prepared = CaseEvaluator().prepare(config(), {"probeId": "QQJ_3_gun5"})
        self.assertIsInstance(prepared.reference_decision, bool)
        self.assertIn("外生", prepared.prompt)
        self.assertNotIn("现在是第1轮", prepared.prompt)

    def test_code_fingerprint_excludes_credentials_and_results(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in (*FINGERPRINT_FILES, "eval_worker/runner.py", "prompt/rules.txt"):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(name, encoding="utf-8")
            first = source_fingerprint(root)
            (root / ".env").write_text("SECRET=must-not-be-hashed", encoding="utf-8")
            (root / "analysis/results").mkdir()
            (root / "analysis/results/private.jsonl").write_text("new result", encoding="utf-8")
            self.assertEqual(first, source_fingerprint(root))
            (root / "analysis/probe.py").write_text("changed source", encoding="utf-8")
            self.assertNotEqual(first, source_fingerprint(root))


class FakeGuard:
    def __init__(self, client, claim):
        self.client = client
        self.stopped = False

    def start(self):
        return self

    @property
    def lost(self):
        return self.client.lose_after_reserve and bool(self.client.reservations)

    def check(self):
        if self.lost:
            raise LeaseLost()

    def wait(self, seconds):
        self.check()

    def stop(self):
        self.stopped = True


class FakeControl:
    timeout = 1

    def __init__(self, payload=None):
        self.claim = claim() if payload is None else payload
        self.requests = []
        self.reservations = []
        self.finishes = []
        self.failures = []
        self.completed = set(self.claim.get("completedCaseIds", []))
        self.has_failed = False
        self.status = "RUNNING"
        self.lose_after_reserve = False
        self.reserve_error = None
        self.finish_error = None
        self.complete_response_lost = False

    def request(self, method, path, body=None, *, retry=False):
        self.requests.append((method, path, deepcopy(body), retry))
        if path == "/api/worker/claims":
            return deepcopy(self.claim)
        if method == "GET":
            return {"status": self.status}
        if path.endswith("/attempts"):
            if self.reserve_error:
                raise self.reserve_error
            self.reservations.append(deepcopy(body))
            number = sum(x["caseId"] == body["caseId"] for x in self.reservations)
            return {"attemptId": body["attemptId"], "attemptNumber": number, "totalCalls": len(self.reservations)}
        if path.endswith("/finish"):
            if self.finish_error:
                raise self.finish_error
            self.finishes.append(deepcopy(body))
            reservation = self.reservations[-1]
            number = sum(x["caseId"] == reservation["caseId"] for x in self.reservations)
            terminal = body["outcome"] == "SUCCESS" or not body["retryable"] or number >= self.claim["config"]["maxAttempts"]
            if terminal:
                self.completed.add(reservation["caseId"])
                self.has_failed |= body["outcome"] != "SUCCESS"
            return {"completed": terminal}
        if path.endswith("/complete"):
            if len(self.completed) != len(self.claim["cases"]):
                raise ControlPlaneError(409, "CASES_INCOMPLETE", "incomplete")
            self.status = "FAILED" if self.has_failed else "SUCCEEDED"
            if self.complete_response_lost:
                raise ControlPlaneError(409, "LEASE_LOST", "lost response after commit")
            return {"status": self.status}
        if path.endswith("/fail"):
            self.failures.append(deepcopy(body))
            self.status = "FAILED"
            return {"status": self.status}
        raise AssertionError(path)


class FakeEvaluator:
    def __init__(self, outcomes=None):
        self.calls = []
        self.outcomes = list(outcomes or [])

    def prepare(self, config, spec):
        return PreparedCase(spec["probeId"], "test prompt", False)

    def evaluate(self, prepared, config, spec):
        self.calls.append(deepcopy(spec))
        return self.outcomes.pop(0) if self.outcomes else successful_outcome()


class WorkerTests(unittest.TestCase):
    def make_worker(self, client, evaluator=None):
        evaluator = evaluator or FakeEvaluator()
        return Worker(client, "test-worker", evaluator=evaluator, guard_factory=FakeGuard, retry_delay=0), evaluator

    def test_completed_checkpoints_are_skipped_on_reclaim(self):
        client = FakeControl(claim(completedCaseIds=["noQ_1_gun0:0"]))
        worker, evaluator = self.make_worker(client)
        self.assertEqual(worker.run_once(), "SUCCEEDED")
        self.assertEqual([x["caseId"] for x in evaluator.calls], ["noQ_1_gun1:0"])
        self.assertEqual(evaluator.calls[0]["seed"], 43)
        self.assertEqual(len(client.reservations), 1)

    def test_transient_error_reserves_a_new_budgeted_attempt(self):
        failure = successful_outcome()
        failure.update(outcome="ERROR", retryable=True, errorType="RATE_LIMIT", wasChallenged=None)
        client = FakeControl()
        worker, evaluator = self.make_worker(client, FakeEvaluator([failure, successful_outcome()]))
        self.assertEqual(worker.run_once(), "SUCCEEDED")
        self.assertEqual(len(evaluator.calls), 3)
        self.assertEqual(len(client.reservations), 3)
        self.assertNotEqual(client.reservations[0]["attemptId"], client.reservations[1]["attemptId"])
        self.assertEqual(client.reservations[0]["caseId"], client.reservations[1]["caseId"])

    def test_auth_failure_is_checkpointed_and_stops_remaining_cases(self):
        failure = successful_outcome()
        failure.update(outcome="ERROR", retryable=False, errorType="AUTH", wasChallenged=None)
        client = FakeControl()
        worker, evaluator = self.make_worker(client, FakeEvaluator([failure]))
        self.assertEqual(worker.run_once(), "FAILED")
        self.assertEqual(len(evaluator.calls), 1)
        self.assertEqual(client.reservations[0]["caseId"], "noQ_1_gun0:0")
        self.assertEqual(client.failures[0]["code"], "AUTH")

    def test_lost_lease_after_reservation_prevents_model_call(self):
        client = FakeControl()
        client.lose_after_reserve = True
        worker, evaluator = self.make_worker(client)
        self.assertEqual(worker.run_once(), "LEASE_LOST")
        self.assertEqual(evaluator.calls, [])
        self.assertEqual(client.finishes, [])

    def test_budget_exhaustion_terminates_unfinished_run(self):
        client = FakeControl()
        client.reserve_error = ControlPlaneError(409, "BUDGET_EXHAUSTED", "no budget")
        worker, evaluator = self.make_worker(client)
        self.assertEqual(worker.run_once(), "FAILED")
        self.assertEqual(evaluator.calls, [])
        self.assertEqual(client.failures[0]["code"], "BUDGET_EXHAUSTED")

    def test_attempts_lost_in_crash_are_not_replaced_outside_budget(self):
        client = FakeControl()
        client.reserve_error = ControlPlaneError(409, "ATTEMPTS_EXHAUSTED", "no budget")
        worker, evaluator = self.make_worker(client)
        self.assertEqual(worker.run_once(), "FAILED")
        self.assertEqual(evaluator.calls, [])
        self.assertEqual(client.failures[0]["code"], "ATTEMPTS_EXHAUSTED")

    def test_uncertain_finish_does_not_repeat_model_or_mark_failed(self):
        client = FakeControl()
        client.finish_error = ControlPlaneError(0, "CONTROL_UNAVAILABLE", "lost response")
        worker, evaluator = self.make_worker(client)
        with self.assertRaises(ControlPlaneError):
            worker.run_once()
        self.assertEqual(len(evaluator.calls), 1)
        self.assertEqual(len(client.reservations), 1)
        self.assertEqual(client.failures, [])

    def test_lost_complete_response_is_confirmed_by_terminal_read(self):
        client = FakeControl()
        client.complete_response_lost = True
        worker, evaluator = self.make_worker(client)
        self.assertEqual(worker.run_once(), "SUCCEEDED")
        self.assertEqual(len(evaluator.calls), 2)
        self.assertTrue(any(method == "GET" for method, _, _, _ in client.requests))

    def test_suite_version_mismatch_does_not_invoke_model(self):
        client = FakeControl(claim(suiteVersion="probe-v1"))
        worker, evaluator = self.make_worker(client)
        self.assertEqual(worker.run_once(), "FAILED")
        self.assertEqual(evaluator.calls, [])
        self.assertEqual(client.failures[0]["code"], "SUITE_MISMATCH")


class HeartbeatTests(unittest.TestCase):
    def test_initial_lease_excludes_time_waiting_for_claim_response(self):
        class Client:
            timeout = 0.1

        payload = claim(leaseSeconds=30, _leaseStartedAt=50)
        guard = LeaseGuard(Client(), payload, clock=lambda: 81)
        self.assertTrue(guard.lost)

    def test_slow_heartbeat_does_not_add_response_delay_to_lease(self):
        clock = [0.0]

        class Client:
            timeout = 0.1

            def request(self, *args, **kwargs):
                clock[0] += 0.04
                return {"leaseSeconds": 0.03}

        guard = LeaseGuard(Client(), claim(leaseSeconds=0.03), clock=lambda: clock[0]).start()
        try:
            self.assertTrue(guard._lost.wait(0.3))
            self.assertTrue(guard.lost)
        finally:
            guard.stop()

    def test_revoked_lease_is_detected_during_provider_call(self):
        renewed = threading.Event()

        class Client:
            timeout = 0.1

            def request(self, *args, **kwargs):
                renewed.set()
                raise ControlPlaneError(409, "LEASE_LOST", "cancelled")

        payload = claim(leaseSeconds=0.03)
        guard = LeaseGuard(Client(), payload, clock=lambda: 0.0).start()
        try:
            self.assertTrue(guard._lost.wait(1))
            self.assertTrue(renewed.is_set())
            self.assertTrue(guard.lost)
            with self.assertRaises(LeaseLost):
                guard.check()
        finally:
            guard.stop()


if __name__ == "__main__":
    unittest.main()
