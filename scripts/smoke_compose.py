"""Verify an already running Compose stack using free mock tasks over HTTP.

This script does not build/start/stop containers, claim work, contact a model
provider, or remove historical data. Run two normal workers before invoking it.
The worker count is deliberately not inferred from this HTTP-only smoke test.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
import uuid


ROOT = Path(__file__).resolve().parents[1]
RUN_COUNT = 8
TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED"}
HASH_PATTERN = re.compile(r"[a-fA-F0-9]{64}\Z")


class SmokeFailure(RuntimeError):
    pass


class Api:
    def __init__(self, server: str, api_key: str):
        parsed = urlparse(server)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("server must be an http(s) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("server must not contain credentials, query or fragment")
        if not api_key:
            raise ValueError("API key must not be empty")
        self.server = server.rstrip("/")
        self.api_key = api_key

    def request(self, method, path, body=None, *, key=None, expected=200, timeout=5.0):
        headers = {"X-API-Key": self.api_key, "Accept": "application/json"}
        if key is not None:
            headers["Idempotency-Key"] = key
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(self.server + path, data=data, method=method, headers=headers)
        try:
            with urlopen(request, timeout=timeout) as response:
                status, raw = response.status, response.read(4 * 1024 * 1024 + 1)
        except HTTPError as error:
            status, raw = error.code, error.read(32768)
        except (URLError, TimeoutError, OSError) as error:
            raise SmokeFailure(f"{method} {path}: control plane unavailable ({type(error).__name__})") from error
        if len(raw) > 4 * 1024 * 1024:
            raise SmokeFailure(f"{method} {path}: oversized response")
        try:
            payload = json.loads(raw) if raw else None
        except (ValueError, UnicodeDecodeError) as error:
            raise SmokeFailure(f"{method} {path}: invalid JSON response") from error
        if status != expected:
            code = payload.get("code") if isinstance(payload, dict) else None
            # Do not echo bodies, headers or command-line arguments: the key is
            # never useful evidence for a smoke-test failure.
            raise SmokeFailure(f"{method} {path}: HTTP {status}, code={code}, expected={expected}")
        return payload


class Checks:
    def __init__(self):
        self.passed = 0

    def require(self, condition, message):
        if not condition:
            raise SmokeFailure(message)
        self.passed += 1


def wait_for_terminal(api: Api, run_ids: list[str], *, timeout_seconds: float) -> dict[str, dict]:
    """Poll just this invocation's tasks, with a single bounded batch deadline."""
    deadline = time.monotonic() + timeout_seconds
    pending = set(run_ids)
    final = {}
    with ThreadPoolExecutor(max_workers=min(8, len(run_ids))) as pool:
        while pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SmokeFailure(f"tasks did not finish in {timeout_seconds:g}s: {','.join(sorted(pending))}")
            futures = {
                run_id: pool.submit(
                    api.request, "GET", f"/api/runs/{run_id}", timeout=max(0.1, min(5.0, remaining))
                )
                for run_id in sorted(pending)
            }
            for run_id, future in futures.items():
                state = future.result()
                if state.get("status") in TERMINAL:
                    final[run_id] = state
                    pending.remove(run_id)
            if pending:
                time.sleep(max(0, min(0.25, deadline - time.monotonic())))
    return final


def inspect_success(api: Api, state: dict, checks: Checks) -> int:
    run_id = state["runId"]
    expected = len(state["config"]["boards"]) * 6 * state["config"]["repeats"]
    checks.require(state["status"] == "SUCCEEDED", f"{run_id}: expected SUCCEEDED")
    checks.require(state["totalCases"] == expected, f"{run_id}: totalCases mismatch")
    checks.require(state["completedCases"] == expected, f"{run_id}: incomplete checkpoints")
    checks.require(state["successCases"] == expected and state["failureCases"] == 0,
                   f"{run_id}: mock case unexpectedly failed")
    checks.require(state["callCount"] == expected, f"{run_id}: duplicate or missing call reservations")
    report = api.request("GET", f"/api/runs/{run_id}/report")
    checks.require(report["mode"] == "mock" and report["mock"] is True,
                   f"{run_id}: report is not explicitly mock")
    checks.require(report["totalTokens"] == 0, f"{run_id}: mock reported provider tokens")
    checks.require(report["callCount"] == expected and report["completedCases"] == expected,
                   f"{run_id}: report disagrees with task counters")
    checks.require(report["unfinishedAttempts"] == 0, f"{run_id}: dangling call reservation")
    page = api.request("GET", f"/api/runs/{run_id}/results?page=0&size=1000")
    items = page["items"]
    checks.require(page["total"] == len(items) == expected, f"{run_id}: result count mismatch")
    checks.require(len({item["caseId"] for item in items}) == expected,
                   f"{run_id}: duplicate case checkpoint")
    checks.require(len({item["attemptId"] for item in items}) == expected,
                   f"{run_id}: duplicate attempt ID")
    for item in items:
        case_id = item["caseId"]
        prompt = item.get("prompt")
        checks.require(isinstance(prompt, str) and len(prompt) > 0,
                       f"{run_id}/{case_id}: raw prompt missing")
        digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        checks.require(item.get("promptHash") == digest,
                       f"{run_id}/{case_id}: raw prompt hash mismatch")
        source_hash = item.get("workerCodeHash")
        checks.require(isinstance(source_hash, str) and HASH_PATTERN.fullmatch(source_hash) is not None,
                       f"{run_id}/{case_id}: source fingerprint missing or invalid")
        checks.require(item["suiteVersion"] == "probe-v2" and item["outcome"] == "SUCCESS",
                       f"{run_id}/{case_id}: unexpected suite or outcome")
        checks.require(item["promptTokens"] == item["completionTokens"] == 0,
                       f"{run_id}/{case_id}: mock result contains provider usage")
        checks.require(type(item["wasChallenged"]) is bool and type(item["referenceDecision"]) is bool,
                       f"{run_id}/{case_id}: decision contract missing")
    return expected


def run_smoke(api: Api, checks: Checks, summary: dict, *, timeout_seconds: float):
    health = api.request("GET", "/actuator/health")
    checks.require(health.get("status") == "UP", "control plane health is not UP")
    batch = uuid.uuid4().hex
    first_seed = int(batch[:7], 16)
    requests = []
    for index in range(RUN_COUNT):
        boards = ["QQJ_3"] if index % 2 == 0 else ["QQQ_2", "noQ_3"]
        body = {"mode": "mock", "boards": boards, "repeats": 1, "seed": first_seed + index,
                "maxAttempts": 2, "maxCalls": len(boards) * 6}
        requests.append((body, f"compose-smoke-{batch}-{index}"))
    # Submit a batch without waiting for earlier jobs to complete. Two already
    # running workers can consume independent runs concurrently.
    created = []
    with ThreadPoolExecutor(max_workers=RUN_COUNT) as pool:
        futures = [pool.submit(api.request, "POST", "/api/runs", body, key=key, expected=201)
                   for body, key in requests]
        for future in futures:
            state = future.result()
            created.append(state)
            summary["runIds"].append(state["runId"])
    run_ids = [state["runId"] for state in created]
    checks.require(len(set(run_ids)) == RUN_COUNT, "distinct submissions produced duplicate run IDs")
    checks.require(len({state["config"]["seed"] for state in created}) == RUN_COUNT,
                   "batch seeds are not distinct")

    body, key = requests[0]
    replay = api.request("POST", "/api/runs", body, key=key)
    checks.require(replay["runId"] == run_ids[0], "idempotent replay returned a different run")
    conflict = api.request("POST", "/api/runs", dict(body, seed=first_seed + 100), key=key, expected=409)
    checks.require(conflict.get("code") == "IDEMPOTENCY_CONFLICT", "different payload did not conflict")
    summary["idempotencyChecksPassed"] = 2

    final = wait_for_terminal(api, run_ids, timeout_seconds=timeout_seconds)
    cases = sum(inspect_success(api, final[run_id], checks) for run_id in run_ids)
    summary.update(successfulRuns=RUN_COUNT, successfulCases=cases, rawResultsVerified=cases)

    limited = api.request("POST", "/api/runs", {
        "mode": "mock", "boards": ["QQJ_3"], "repeats": 1, "seed": first_seed + RUN_COUNT,
        "maxAttempts": 2, "maxCalls": 1,
    }, key=f"compose-smoke-{batch}-budget", expected=201)
    limited_id = limited["runId"]
    summary["runIds"].append(limited_id)
    summary["budgetRunId"] = limited_id
    limited_final = wait_for_terminal(api, [limited_id], timeout_seconds=timeout_seconds)[limited_id]
    checks.require(limited_final["status"] == "FAILED", "budget-limited run did not fail")
    checks.require(limited_final["callCount"] == 1, "budget-limited run exceeded one reservation")
    checks.require(limited_final["completedCases"] == 1, "budget-limited run lost its first checkpoint")
    limited_report = api.request("GET", f"/api/runs/{limited_id}/report")
    checks.require(limited_report["mock"] is True and limited_report["totalTokens"] == 0,
                   "budget-limited report is not a zero-token mock")
    checks.require("BUDGET_EXHAUSTED" in (limited_report.get("lastError") or ""),
                   "budget-limited failure reason does not identify exhausted budget")
    summary.update(budgetFailedRuns=1, budgetReservedCalls=1, taskCount=len(summary["runIds"]))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", default=os.environ.get("LIARS_CONTROL_SERVER", "http://127.0.0.1:8080"))
    parser.add_argument("--api-key", default=os.environ.get("LIARS_CONTROL_API_KEY", "local-dev-key-change-me"))
    parser.add_argument("--out", type=Path, default=ROOT / ".local" / "compose-smoke.json")
    parser.add_argument("--timeout-seconds", type=float, default=45.0,
                        help="maximum wait for each batch to reach terminal state (default: 45)")
    args = parser.parse_args(argv)
    if not 1 <= args.timeout_seconds <= 300:
        parser.error("timeout-seconds must be between 1 and 300")
    checks = Checks()
    started = time.monotonic()
    summary = {
        "status": "RUNNING", "test": "compose-http-mock", "runIds": [],
        "startedAt": datetime.now(timezone.utc).isoformat(),
        "workerCountVerified": False,
        "workerCountNote": "HTTP-only verification; confirm distinct workers separately using Docker logs or SQL",
    }
    try:
        api = Api(args.server, args.api_key)
        run_smoke(api, checks, summary, timeout_seconds=args.timeout_seconds)
        summary["status"] = "PASSED"
    except (SmokeFailure, ValueError, KeyError, TypeError) as error:
        summary["status"] = "FAILED"
        summary["error"] = str(error)
    summary["checksPassed"] = checks.passed
    summary["elapsedSeconds"] = round(time.monotonic() - started, 3)
    rendered = json.dumps(summary, ensure_ascii=False, indent=2)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if summary["status"] == "PASSED" else 1


if __name__ == "__main__":
    sys.exit(main())
