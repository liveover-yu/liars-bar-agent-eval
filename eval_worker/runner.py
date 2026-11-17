"""Run-level leases, durable per-call reservations and per-case checkpoints."""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid

from .control import ControlClient, ControlPlaneError
from .decisions import CaseEvaluator, SUITE_VERSION, SuiteMismatch


LOG = logging.getLogger("eval_worker")


def event(name: str, **fields):
    LOG.info(json.dumps({"event": name, **fields}, ensure_ascii=False, sort_keys=True))


class LeaseLost(RuntimeError):
    pass


class RunFailure(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class LeaseGuard:
    """Renew while a potentially slow provider request is in flight.

    Local expiry prevents new calls during an outage. The server remains the
    authority and fences every write using its token and database deadline.
    """

    def __init__(self, client: ControlClient, claim: dict, *, clock=time.monotonic):
        self.client = client
        self.run_id = claim["runId"]
        self.token = claim["leaseToken"]
        self.seconds = float(claim["leaseSeconds"])
        if self.seconds <= 0:
            raise ValueError("leaseSeconds must be positive")
        self._clock = clock
        self._deadline = claim.get("_leaseStartedAt", clock()) + self.seconds
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._thread = threading.Thread(target=self._renew, name="lease-heartbeat", daemon=True)

    def start(self):
        self._thread.start()
        return self

    @property
    def lost(self) -> bool:
        if self._clock() >= self._deadline:
            self._lost.set()
        return self._lost.is_set()

    def check(self):
        if self.lost:
            raise LeaseLost("worker lease is no longer valid")

    def wait(self, seconds: float):
        self._lost.wait(seconds)
        self.check()

    def stop(self):
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=self.client.timeout + 1)

    def _renew(self):
        interval = min(5.0, self.seconds / 3)
        while not self._stop.wait(interval):
            if self.lost:
                return
            try:
                sent_at = self._clock()
                response = self.client.request(
                    "POST", f"/api/worker/runs/{self.run_id}/heartbeat",
                    {"leaseToken": self.token}, retry=False,
                )
                self.seconds = float(response["leaseSeconds"])
                if self.seconds <= 0:
                    self._lost.set()
                    return
                # A slow response does not extend the server's deadline. Using
                # send time is conservative even when server processing lagged.
                self._deadline = sent_at + self.seconds
            except ControlPlaneError as exc:
                if exc.status in (400, 401, 403, 404, 409):
                    self._lost.set()
                    return
                event("heartbeat_unavailable", runId=self.run_id, code=exc.code)
            except (KeyError, TypeError, ValueError):
                self._lost.set()
                return


class Worker:
    def __init__(
        self,
        client: ControlClient,
        worker_id: str,
        *,
        evaluator=None,
        retry_delay: float = 0.5,
        guard_factory=LeaseGuard,
    ):
        if not worker_id.strip():
            raise ValueError("worker ID must not be empty")
        if retry_delay < 0:
            raise ValueError("retry delay must not be negative")
        self.client = client
        self.worker_id = worker_id
        self.evaluator = evaluator or CaseEvaluator()
        self.retry_delay = retry_delay
        self.guard_factory = guard_factory

    def run_once(self) -> str:
        # Claim is deliberately not retried. If its response is lost, the
        # unexecuted lease is recovered by expiry instead of grabbing more runs.
        claim_started_at = time.monotonic()
        claim = self.client.request(
            "POST", "/api/worker/claims", {"workerId": self.worker_id}, retry=False
        )
        if claim is None:
            return "IDLE"
        claim["_leaseStartedAt"] = claim_started_at
        guard = self.guard_factory(self.client, claim).start()
        run_id = claim["runId"]
        event("run_claimed", runId=run_id, workerId=self.worker_id)
        try:
            if claim.get("suiteVersion") != SUITE_VERSION:
                return self._fail(claim, guard, "SUITE_MISMATCH", "worker does not support requested suite")
            config = claim["config"]
            completed = set(claim["completedCaseIds"])
            for spec in claim["cases"]:
                guard.check()
                if spec["caseId"] in completed:
                    continue
                prepared = self.evaluator.prepare(config, spec)
                self._run_case(claim, spec, prepared, guard)
            guard.check()
            try:
                response = self.client.request(
                    "POST", f"/api/worker/runs/{run_id}/complete",
                    {"leaseToken": claim["leaseToken"]}, retry=True,
                )
            except ControlPlaneError as exc:
                # A response can be lost after commit. A terminal read confirms
                # success without issuing another provider request.
                if exc.code == "LEASE_LOST":
                    response = self.client.request("GET", f"/api/runs/{run_id}", retry=True)
                    if response.get("status") not in ("SUCCEEDED", "FAILED"):
                        raise LeaseLost("lease lost before completing run") from exc
                else:
                    raise
            status = response.get("status") if isinstance(response, dict) else None
            if status not in ("SUCCEEDED", "FAILED"):
                response = self.client.request("GET", f"/api/runs/{run_id}", retry=True)
                status = response["status"]
            event("run_completed", runId=run_id, status=status)
            return status
        except LeaseLost:
            event("lease_lost", runId=run_id)
            return "LEASE_LOST"
        except ControlPlaneError as exc:
            if exc.code == "LEASE_LOST":
                event("lease_lost", runId=run_id)
                return "LEASE_LOST"
            if exc.code in ("BUDGET_EXHAUSTED", "ATTEMPTS_EXHAUSTED"):
                return self._fail(claim, guard, exc.code, "durable attempt budget exhausted")
            # An uncertain checkpoint must not trigger another model call.
            # Let the lease expire; a replacement reads committed checkpoints.
            event("control_error", runId=run_id, code=exc.code, status=exc.status)
            raise
        except SuiteMismatch as exc:
            return self._fail(claim, guard, "SUITE_MISMATCH", str(exc))
        except RunFailure as exc:
            return self._fail(claim, guard, exc.code, str(exc))
        except (KeyError, TypeError, ValueError) as exc:
            return self._fail(claim, guard, "INVALID_MANIFEST", f"invalid run manifest ({type(exc).__name__})")
        finally:
            guard.stop()

    def _run_case(self, claim: dict, spec: dict, prepared, guard):
        run_id, token, config = claim["runId"], claim["leaseToken"], claim["config"]
        while True:
            guard.check()
            attempt_id = str(uuid.uuid4())
            try:
                reservation = self.client.request(
                    "POST", f"/api/worker/runs/{run_id}/attempts",
                    {"leaseToken": token, "caseId": spec["caseId"], "attemptId": attempt_id},
                    retry=True,
                )
            except ControlPlaneError as exc:
                if exc.code == "CASE_COMPLETED":
                    return
                raise
            # Cancellation or a failed heartbeat may have happened while the
            # reservation request was being retried. Do not start a new call.
            guard.check()
            outcome = self.evaluator.evaluate(prepared, config, spec)
            guard.check()
            self.client.request(
                "POST", f"/api/worker/runs/{run_id}/attempts/{attempt_id}/finish",
                {"leaseToken": token, **outcome}, retry=True,
            )
            event(
                "attempt_finished", runId=run_id, caseId=spec["caseId"],
                attemptId=attempt_id,
                attempt=reservation["attemptNumber"], outcome=outcome["outcome"],
                errorType=outcome["errorType"],
            )
            if outcome["errorType"] == "AUTH":
                raise RunFailure("AUTH", "provider credentials are invalid; remaining cases were not called")
            if (
                outcome["outcome"] == "SUCCESS"
                or not outcome["retryable"]
                or reservation["attemptNumber"] >= config["maxAttempts"]
                or reservation["totalCalls"] >= config["maxCalls"]
            ):
                return
            guard.wait(min(self.retry_delay * 2 ** (reservation["attemptNumber"] - 1), 5.0))

    def _fail(self, claim: dict, guard, code: str, message: str) -> str:
        run_id = claim["runId"]
        if guard.lost:
            event("lease_lost", runId=run_id)
            return "LEASE_LOST"
        try:
            self.client.request(
                "POST", f"/api/worker/runs/{run_id}/fail",
                {"leaseToken": claim["leaseToken"], "code": code, "message": message}, retry=True,
            )
        except ControlPlaneError as exc:
            if exc.code != "LEASE_LOST":
                raise
            state = self.client.request("GET", f"/api/runs/{run_id}", retry=True)
            if state.get("status") != "FAILED":
                event("lease_lost", runId=run_id)
                return "LEASE_LOST"
        event("run_failed", runId=run_id, code=code)
        return "FAILED"
