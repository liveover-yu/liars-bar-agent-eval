"""Run a real Java/Python smoke test without contacting any LLM provider.

Build first: mvn -f control-plane/pom.xml package
The test owns its Java processes and a temporary H2 database. It also supports
a dedicated PostgreSQL database via --database-url/username/password.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url")
    parser.add_argument("--database-username", default="sa")
    parser.add_argument("--database-password", default="")
    args = parser.parse_args()
    jars = [p for p in (ROOT / "control-plane" / "target").glob("*.jar")
            if not p.name.startswith("original-")]
    if len(jars) != 1:
        raise RuntimeError("Build control-plane first; expected one executable jar in target/.")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    api_key = "isolated-smoke-" + uuid.uuid4().hex
    environment = dict(os.environ, LIARS_CONTROL_API_KEY=api_key)
    # Mock must work with no provider credential at all.
    environment.pop("LIARS_API_KEY", None)
    process = None

    def request(method, path, body=None, key=None, expected=200):
        headers = {"X-API-Key": api_key}
        if key:
            headers["Idempotency-Key"] = key
        if body is not None:
            headers["Content-Type"] = "application/json"
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                status, raw = response.status, response.read()
        except urllib.error.HTTPError as error:
            status, raw = error.code, error.read()
        payload = json.loads(raw) if raw else None
        assert status == expected, (method, path, status, payload, "expected", expected)
        return payload

    with tempfile.TemporaryDirectory(prefix="liars-platform-smoke-") as directory:
        db_url = args.database_url or (
            "jdbc:h2:file:" + str(Path(directory) / "tasks").replace("\\", "/")
            + ";MODE=PostgreSQL;DATABASE_TO_LOWER=TRUE;DB_CLOSE_ON_EXIT=FALSE;WRITE_DELAY=0"
        )
        log_path = Path(directory) / "server.log"
        command = ["java", "-jar", str(jars[0]), f"--server.port={port}",
                   "--server.address=127.0.0.1", "--eval.lease-seconds=5",
                   f"--spring.datasource.url={db_url}",
                   f"--spring.datasource.username={args.database_username}",
                   f"--spring.datasource.password={args.database_password}"]
        if db_url.startswith("jdbc:postgresql:"):
            command.append("--spring.profiles.active=postgres")

        def stop_server():
            nonlocal process
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            process = None

        def start_server():
            nonlocal process
            with log_path.open("a", encoding="utf-8") as log:
                process = subprocess.Popen(command, cwd=ROOT, env=environment,
                                           stdout=log, stderr=subprocess.STDOUT)
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError(log_path.read_text(encoding="utf-8", errors="replace")[-7000:])
                try:
                    health = request("GET", "/actuator/health")
                    if health["status"] == "UP":
                        return
                except (OSError, AssertionError):
                    pass
                time.sleep(0.2)
            raise RuntimeError("Control plane did not become healthy in 45 seconds")

        def run_worker():
            return subprocess.run(
                [sys.executable, "-m", "eval_worker", "--server", base,
                 "--api-key", api_key, "--worker-id", "smoke-worker", "--once"],
                cwd=ROOT, env=environment, timeout=60, capture_output=True,
                text=True, encoding="utf-8", errors="replace",
            )

        try:
            start_server()
            body = {"mode": "mock", "boards": ["QQJ_3"], "repeats": 1, "seed": 42}
            idempotency_key = str(uuid.uuid4())
            created = request("POST", "/api/runs", body, idempotency_key, 201)
            run_id = created["runId"]
            replay = request("POST", "/api/runs", body, idempotency_key)
            assert replay["runId"] == run_id
            conflict = request("POST", "/api/runs", dict(body, seed=43), idempotency_key, 409)
            assert conflict["code"] == "IDEMPOTENCY_CONFLICT", conflict

            claim = request("POST", "/api/worker/claims", {"workerId": "checkpoint-owner"})
            assert claim["runId"] == run_id
            token = claim["leaseToken"]
            first = claim["cases"][0]
            attempt_id = str(uuid.uuid4())
            worker_path = f"/api/worker/runs/{run_id}"
            reserve = {"leaseToken": token, "caseId": first["caseId"], "attemptId": attempt_id}
            reserved = request("POST", worker_path + "/attempts", reserve, expected=201)
            duplicate = request("POST", worker_path + "/attempts", reserve)
            assert reserved["totalCalls"] == duplicate["totalCalls"] == 1

            from analysis.probe import make_cases, render_prompt
            case = next(c for c in make_cases() if c.probe_id == first["probeId"])
            prompt = render_prompt(case, claim["config"]["ruleBase"])
            finish = {
                "leaseToken": token, "outcome": "SUCCESS", "retryable": False,
                "wasChallenged": False, "referenceDecision": case.reference_decision,
                "errorType": None, "errorMessage": None, "latencyMs": 1,
                "promptTokens": 0, "completionTokens": 0,
                "promptHash": hashlib.sha256(prompt.encode()).hexdigest(),
                "modelVersion": "mock-policy", "suiteVersion": "probe-v2",
                "content": '{"was_challenged":false,"challenge_reason":"smoke checkpoint"}',
            }
            endpoint = worker_path + f"/attempts/{attempt_id}/finish"
            request("POST", endpoint, finish)
            request("POST", endpoint, finish)
            cancelled = request("POST", f"/api/runs/{run_id}/cancel", {})
            assert cancelled["status"] == "CANCELLED" and cancelled["completedCases"] == 1, cancelled
            stale = request("POST", worker_path + "/heartbeat", {"leaseToken": token}, expected=409)
            assert stale["code"] == "LEASE_LOST", stale
            stop_server()
            start_server()
            restored = request("GET", f"/api/runs/{run_id}")
            assert restored["completedCases"] == 1 and restored["status"] == "CANCELLED", restored
            request("POST", f"/api/runs/{run_id}/resume", {})
            worker = run_worker()
            assert worker.returncode == 0, (worker.stdout, worker.stderr)
            final = request("GET", f"/api/runs/{run_id}")
            assert final["status"] == "SUCCEEDED", (final, worker.stdout, worker.stderr)
            assert final["completedCases"] == final["totalCases"] == 6, final
            assert final["callCount"] == 6, final
            report = request("GET", f"/api/runs/{run_id}/report")
            assert report["totalTokens"] == 0 and report["mode"] == "mock", report
            results = request("GET", f"/api/runs/{run_id}/results?page=0&size=100")
            assert results["total"] == len(results["items"]) == 6, results
            assert len({r["caseId"] for r in results["items"]}) == 6
            print("PASS: Java restart + persisted checkpoint + idempotency + stale lease fencing + Python mock worker")

            limited = request("POST", "/api/runs", dict(body, maxCalls=1), str(uuid.uuid4()), 201)
            run_worker()
            exhausted = request("GET", f"/api/runs/{limited['runId']}")
            assert exhausted["status"] == "FAILED" and exhausted["callCount"] == 1, exhausted
            print("PASS: persisted request budget stops a run before extra model calls")

            # Simulate a worker disappearing between a reservation and finish.
            # A real wall-clock lease expires; a replacement must preserve the
            # completed case and charge the unknown attempt against the budget.
            recoverable = request("POST", "/api/runs", body, str(uuid.uuid4()), 201)
            crashed = request("POST", "/api/worker/claims", {"workerId": "crashed-owner"})
            assert crashed["runId"] == recoverable["runId"]
            crash_path = f"/api/worker/runs/{crashed['runId']}"
            saved_attempt = str(uuid.uuid4())
            request("POST", crash_path + "/attempts", {
                "leaseToken": crashed["leaseToken"], "caseId": crashed["cases"][0]["caseId"],
                "attemptId": saved_attempt,
            }, expected=201)
            request("POST", crash_path + f"/attempts/{saved_attempt}/finish",
                    dict(finish, leaseToken=crashed["leaseToken"]))
            request("POST", crash_path + "/attempts", {
                "leaseToken": crashed["leaseToken"], "caseId": crashed["cases"][1]["caseId"],
                "attemptId": str(uuid.uuid4()),
            }, expected=201)
            time.sleep(crashed["leaseSeconds"] + 0.1)
            replacement = run_worker()
            assert replacement.returncode == 0, (replacement.stdout, replacement.stderr)
            recovered = request("GET", f"/api/runs/{crashed['runId']}/report")
            assert recovered["status"] == "SUCCEEDED" and recovered["completedCases"] == 6, recovered
            assert recovered["callCount"] == 7 and recovered["unfinishedAttempts"] == 1, recovered
            old_owner = request("POST", crash_path + "/heartbeat",
                                {"leaseToken": crashed["leaseToken"]}, expected=409)
            assert old_owner["code"] == "LEASE_LOST", old_owner
            print("PASS: expired lease takeover preserves checkpoints and charges unknown attempts")
        except BaseException:
            print(log_path.read_text(encoding="utf-8", errors="replace")[-7000:], file=sys.stderr)
            raise
        finally:
            stop_server()


if __name__ == "__main__":
    main()
