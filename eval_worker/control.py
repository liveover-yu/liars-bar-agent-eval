"""Small JSON client with explicit retries for idempotent worker operations.

Only a caller that can repeat an operation safely should request retries. In
particular, reserving a model call uses the same attempt ID on every transport
retry; a timed-out reservation must never turn into a second reservation.
"""

from __future__ import annotations

import json
import socket
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


class ControlPlaneError(RuntimeError):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code


class ControlClient:
    """Thread-safe HTTP adapter shared by the evaluator and heartbeat thread."""

    def __init__(
        self,
        server: str,
        api_key: str,
        *,
        timeout: float = 10.0,
        retries: int = 3,
        retry_delay: float = 0.25,
        opener=urlopen,
        sleep=time.sleep,
    ):
        parsed = urlparse(server)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("server must be an http(s) URL")
        if parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ValueError("server must not contain credentials, query or fragment")
        if not api_key:
            raise ValueError("control plane API key must not be empty")
        if timeout <= 0 or retries < 0 or retry_delay < 0:
            raise ValueError("invalid HTTP timeout or retry settings")
        self.server = server.rstrip("/")
        self._api_key = api_key
        self.timeout = timeout
        self.retries = retries
        self.retry_delay = retry_delay
        self._opener = opener
        self._sleep = sleep

    def request(self, method: str, path: str, body=None, *, retry: bool = False):
        if not path.startswith("/api/"):
            raise ValueError("worker requests must target /api/ paths")
        # Serialize once so retrying cannot introduce a new UUID/timestamp or
        # change the idempotency fingerprint checked by the server.
        data = None if body is None else json.dumps(
            body, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        request_id = str(uuid.uuid4())
        headers = {
            "Accept": "application/json",
            "X-API-Key": self._api_key,
            "X-Request-Id": request_id,
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        attempts = 1 + (self.retries if retry else 0)
        for attempt in range(attempts):
            request = Request(self.server + path, data=data, headers=headers, method=method)
            try:
                with self._opener(request, timeout=self.timeout) as response:
                    if response.status == 204:
                        return None
                    raw = response.read(4 * 1024 * 1024 + 1)
                    if len(raw) > 4 * 1024 * 1024:
                        raise ControlPlaneError(0, "INVALID_RESPONSE", "control response is too large")
                    if not raw:
                        return None
                    try:
                        return json.loads(raw)
                    except (ValueError, UnicodeDecodeError) as exc:
                        raise ControlPlaneError(
                            response.status, "INVALID_RESPONSE", "control plane returned invalid JSON"
                        ) from exc
            except HTTPError as exc:
                raw = exc.read(32768)
                try:
                    payload = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    payload = {}
                if not isinstance(payload, dict):
                    payload = {}
                error = ControlPlaneError(
                    exc.code,
                    str(payload.get("code", "HTTP_ERROR")),
                    str(payload.get("message", f"control plane HTTP {exc.code}")),
                )
                transient = exc.code == 429 or 500 <= exc.code <= 599
                if not transient or attempt + 1 == attempts:
                    raise error from exc
            except (URLError, TimeoutError, socket.timeout, ConnectionError, OSError) as exc:
                if attempt + 1 == attempts:
                    raise ControlPlaneError(
                        0, "CONTROL_UNAVAILABLE", "control plane connection failed"
                    ) from exc
            self._sleep(min(self.retry_delay * (2 ** attempt), 2.0))
        raise AssertionError("unreachable")
