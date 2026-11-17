"""One bounded model call (or a free deterministic mock), without hidden retries."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import logging
from pathlib import Path
import random
import time
from urllib.error import URLError


SUITE_VERSION = "probe-v2"

FINGERPRINT_FILES = (
    "analysis/probe.py", "analysis/risk.py", "player.py", "game_record.py",
    "llm_client.py", "contracts/probe-suite-v2.json",
)


def source_fingerprint(root: Path | None = None) -> str:
    """Hash only executable sources/templates, never environment or run data."""
    root = root or Path(__file__).resolve().parents[1]
    paths = {root / name for name in FINGERPRINT_FILES}
    paths.update((root / "eval_worker").glob("*.py"))
    paths.update((root / "prompt").glob("*.txt"))
    digest = hashlib.sha256()
    for path in sorted(paths, key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(relative + b"\0" + str(len(content)).encode("ascii") + b"\0" + content + b"\0")
    return digest.hexdigest()


class SuiteMismatch(ValueError):
    """The worker cannot execute the immutable suite requested by the server."""


@dataclass(frozen=True)
class PreparedCase:
    probe_id: str
    prompt: str
    reference_decision: bool

    @property
    def prompt_hash(self) -> str:
        return hashlib.sha256(self.prompt.encode("utf-8")).hexdigest()


def parse_choice(content: str) -> bool:
    """Share the exact challenge contract used by the game and probe runner."""
    from player import parse_action

    return parse_action(content, "challenge")["was_challenged"]


def classify_error(exc: Exception) -> tuple[str, bool, str]:
    """Classify public SDK/transport attributes without logging credential text."""
    status = getattr(exc, "status_code", None)
    name = type(exc).__name__
    if name == "OpenAIError" and "api_key" in str(exc):
        return "AUTH", False, "provider API key is not configured"
    if status in (401, 403):
        return "AUTH", False, f"provider rejected credentials (HTTP {status})"
    if status == 429:
        return "RATE_LIMIT", True, "provider rate limit exceeded"
    if status in (408, 504) or isinstance(exc, TimeoutError) or name == "APITimeoutError":
        return "TIMEOUT", True, "provider request timed out"
    if isinstance(exc, (ConnectionError, URLError)) or name == "APIConnectionError":
        return "NETWORK", True, "provider connection failed"
    if isinstance(status, int):
        return "PROVIDER", status >= 500, f"provider rejected request (HTTP {status})"
    return "INTERNAL", False, f"worker could not execute request ({name})"


def _token_count(usage: dict, key: str) -> int:
    value = usage.get(key, 0)
    return value if type(value) is int and value >= 0 else 0


class CaseEvaluator:
    def __init__(self, *, client_factory=None, cases=None, render=None, clock=time.monotonic):
        self._client_factory = client_factory
        self._cases = cases
        self._render = render
        self._clock = clock
        self._code_hash = None

    def prepare(self, config: dict, spec: dict) -> PreparedCase:
        if self._cases is None:
            from analysis import probe

            if getattr(probe, "SUITE_VERSION", None) != SUITE_VERSION:
                raise SuiteMismatch("installed probe suite is not probe-v2")
            self._cases = {case.probe_id: case for case in probe.make_cases()}
            self._render = probe.render_prompt
        try:
            case = self._cases[spec["probeId"]]
        except KeyError as exc:
            raise SuiteMismatch("unknown probe ID in run manifest") from exc
        reference = case.reference_decision
        if type(reference) is not bool:
            raise SuiteMismatch("probe reference must be boolean")
        prompt = self._render(case, config["ruleBase"])
        return PreparedCase(spec["probeId"], prompt, reference)

    def evaluate(self, prepared: PreparedCase, config: dict, spec: dict) -> dict:
        """The caller reserves a durable attempt before invoking this method."""
        started = self._clock()
        content = ""
        metadata = {}
        model_version = config["model"]
        if self._code_hash is None:
            self._code_hash = source_fingerprint()
        outcome = {
            "outcome": "SUCCESS",
            "retryable": False,
            "wasChallenged": None,
            "referenceDecision": prepared.reference_decision,
            "errorType": None,
            "errorMessage": None,
            "promptHash": prepared.prompt_hash,
            "prompt": prepared.prompt,
            "workerCodeHash": self._code_hash,
            "suiteVersion": SUITE_VERSION,
        }
        if config["mode"] == "mock":
            # A seeded coin exercises both results. It is not a real model score
            # and deliberately does not just copy the reference answer.
            choice = random.Random(spec["seed"]).random() < 0.5
            content = json.dumps({
                "was_challenged": choice,
                "challenge_reason": "seeded mock; no model API request was made",
            }, ensure_ascii=False)
            model_version = "mock-policy-v1"
            outcome["wasChallenged"] = choice
        elif config["mode"] == "llm":
            client = None
            try:
                factory = self._client_factory
                if factory is None:
                    from llm_client import LLMClient
                    factory = LLMClient
                client = factory(
                    temperature=config["temperature"],
                    seed=spec["seed"],
                    max_retries=0,
                    timeout=config["timeoutSeconds"],
                    max_output_tokens=config["maxOutputTokens"],
                )
                content, _ = client.chat(
                    [{"role": "user", "content": prepared.prompt}], model=config["model"]
                )
                metadata = getattr(client, "last_response", None) or {}
                try:
                    outcome["wasChallenged"] = parse_choice(content)
                except ValueError as exc:
                    outcome.update(
                        outcome="ERROR", retryable=True, errorType="FORMAT", errorMessage=str(exc)
                    )
            except Exception as exc:
                if client is not None:
                    metadata = getattr(client, "last_response", None) or {}
                category, retryable, message = classify_error(exc)
                outcome.update(
                    outcome="ERROR", retryable=retryable, errorType=category, errorMessage=message
                )
            finally:
                close = getattr(client, "close", None)
                if close is not None:
                    try:
                        close()
                    except Exception as exc:
                        # Cleanup is not an inference failure: repeating the
                        # call would spend budget for an answer already known.
                        logging.getLogger("eval_worker").warning(json.dumps({
                            "event": "provider_close_failed", "exceptionType": type(exc).__name__,
                        }))
        else:
            raise SuiteMismatch("unknown evaluation mode")
        usage = metadata.get("usage") or {}
        if not isinstance(usage, dict):
            usage = {}
        outcome.update(
            latencyMs=max(0, round((self._clock() - started) * 1000)),
            promptTokens=_token_count(usage, "prompt_tokens"),
            completionTokens=_token_count(usage, "completion_tokens"),
            modelVersion=str(metadata.get("model") or model_version),
            requestId=metadata.get("request_id"),
            content=content,
        )
        return outcome
