"""Run a bounded, real-model experiment through the Java control plane.

Without --execute this prints the plan only. Provider credentials stay in workers;
this client exports task configuration, reports and raw case results, never keys.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
import uuid

ROOT = Path(__file__).resolve().parents[1]
TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def collection_status(runs: list[dict], output: Path) -> str:
    reports = [json.loads((output / item["reportFile"]).read_text(encoding="utf-8")) for item in runs]
    complete = bool(reports) and all(
        report["completedCases"] == report["totalCases"] and report["unfinishedAttempts"] == 0
        for report in reports
    )
    if complete:
        return "SUCCEEDED" if all(item["status"] == "SUCCEEDED" for item in runs) else "COMPLETED_WITH_ERRORS"
    return "INCOMPLETE"


class ControlAPI:
    def __init__(self, server: str):
        self.server = server.rstrip("/")
        self.key = os.environ.get("LIARS_CONTROL_API_KEY", "local-dev-key-change-me")

    def request(self, method: str, path: str, body=None, idempotency_key=None):
        headers = {"X-API-Key": self.key, "Content-Type": "application/json"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        encoded = None if body is None else json.dumps(body).encode("utf-8")
        request = Request(self.server + path, data=encoded, headers=headers, method=method)
        try:
            with urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise RuntimeError(f"control API returned HTTP {exc.code} for {method} {path}") from None
        except (URLError, TimeoutError):
            raise RuntimeError(f"control API unavailable for {method} {path}") from None


def snapshot(api: ControlAPI, item: dict, output: Path) -> tuple[dict, dict]:
    run_id = item["runId"]
    run = api.request("GET", f"/api/runs/{run_id}")
    report = api.request("GET", f"/api/runs/{run_id}/report")
    result = api.request("GET", f"/api/runs/{run_id}/results?page=0&size=1000")
    if result["total"] != len(result["items"]):
        raise RuntimeError("result export exceeded one page; refusing an incomplete export")
    save(output / item["runFile"], run)
    save(output / item["reportFile"], report)
    save(output / item["resultsFile"], result)
    item["status"] = run["status"]
    return run, report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="submit real-model tasks; may incur provider charges")
    parser.add_argument("--server", default="http://127.0.0.1:8080")
    parser.add_argument("--model", default="deepseek-chat")
    parser.add_argument("--out-dir", type=Path, default=ROOT / "analysis/experiments/deepseek-v2-20261118")
    parser.add_argument("--deadline-seconds", type=int, default=1800)
    args = parser.parse_args()
    if args.deadline_seconds < 1:
        parser.error("deadline-seconds must be positive")
    contract = json.loads((ROOT / "contracts/probe-suite-v2.json").read_text(encoding="utf-8"))
    configs = [{
        "mode": "llm", "model": args.model, "boards": contract["boards"],
        "ruleBase": rule, "repeats": 1, "seed": 42, "temperature": 0,
        "maxAttempts": 1, "maxCalls": 48, "timeoutSeconds": 60, "maxOutputTokens": 1024,
    } for rule in ("neutral", "threat")]
    plan = {
        "ruleBases": [c["ruleBase"] for c in configs], "boards": contract["boards"],
        "repeats": 1, "seed": 42, "temperature": 0, "casesPerRun": 48,
        "maxCallsPerRun": 48, "totalCallLimit": 96, "maxAttempts": 1,
        "timeoutSeconds": 60, "maxOutputTokens": 1024,
        "note": "one response per fixed state; descriptive pilot, no significance or generalization claim",
    }
    print(json.dumps({"execute": args.execute, "model": args.model, "plan": plan}, ensure_ascii=False), flush=True)
    if not args.execute:
        return 0
    output = args.out_dir.resolve()
    if (output / "experiment.json").exists():
        parser.error("experiment already exists; use a new out-dir to preserve existing evidence")
    api = ControlAPI(args.server)
    experiment = {
        "schemaVersion": "liars-live-experiment-v1", "mode": "llm", "suiteVersion": contract["suiteVersion"],
        "requestedModel": args.model, "providerHost": urlsplit(os.environ.get("LIARS_API_BASE_URL", "")).hostname,
        "startedAt": now(), "finishedAt": None, "status": "SUBMITTING", "plan": plan, "runs": [],
    }
    manifest = output / "experiment.json"
    save(manifest, experiment)
    try:
        for config in configs:
            rule = config["ruleBase"]
            item = {
                "ruleBase": rule, "idempotencyKey": f"live-{uuid.uuid4()}",
                "reportFile": f"{rule}/report.json", "resultsFile": f"{rule}/results.json",
                "runFile": f"{rule}/run.json", "requestedConfig": config,
            }
            experiment["runs"].append(item)
            save(manifest, experiment)
            run = api.request("POST", "/api/runs", config, item["idempotencyKey"])
            item.update(runId=run["runId"], status=run["status"])
            save(manifest, experiment)
            print(json.dumps({"event": "submitted", "ruleBase": rule, "runId": run["runId"]}), flush=True)
        experiment["status"] = "RUNNING"
        deadline = time.monotonic() + args.deadline_seconds
        last = None
        while True:
            state = []
            abort_reason = None
            for item in experiment["runs"]:
                run, report = snapshot(api, item, output)
                state.append({"ruleBase": item["ruleBase"], "status": run["status"],
                              "completed": report["completedCases"], "calls": report["callCount"],
                              "success": report["successCases"], "errors": report["errorCounts"]})
                if report["successCases"] == 0 and report["failureCases"] >= 3:
                    abort_reason = "three final failures before any success; stopped to avoid repeated provider failures"
            save(manifest, experiment)
            if state != last:
                print(json.dumps({"event": "progress", "runs": state}), flush=True)
                last = state
            if all(s["status"] in TERMINAL for s in state):
                break
            if time.monotonic() >= deadline:
                abort_reason = "experiment deadline reached"
            if abort_reason:
                experiment["earlyStopReason"] = abort_reason
                for item in experiment["runs"]:
                    if item["status"] not in TERMINAL:
                        api.request("POST", f"/api/runs/{item['runId']}/cancel", {})
                        snapshot(api, item, output)
                break
            time.sleep(5)
        experiment["finishedAt"] = now()
        experiment["status"] = collection_status(experiment["runs"], output)
        experiment["statusMeaning"] = "Collection status; original task statuses and failed cases are preserved."
        save(manifest, experiment)
        print(json.dumps({"event": "exported", "status": experiment["status"], "path": str(output)}), flush=True)
        return 0 if experiment["status"] in {"SUCCEEDED", "COMPLETED_WITH_ERRORS"} else 1
    except (RuntimeError, OSError, ValueError, KeyError) as exc:
        experiment["status"] = "CLIENT_ERROR"
        experiment["clientError"] = str(exc)
        save(manifest, experiment)
        print(f"Experiment client stopped: {exc}; existing tasks and evidence retained", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
