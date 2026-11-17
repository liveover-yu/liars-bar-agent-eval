"""Run ``python -m eval_worker --once`` for a free local mock task."""

from __future__ import annotations

import argparse
import logging
import os
import socket
import sys
import time
import uuid

from .control import ControlClient, ControlPlaneError
from .runner import Worker, event


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Python worker for the Java evaluation control plane")
    parser.add_argument("--server", default=os.environ.get(
        "LIARS_CONTROL_SERVER", os.environ.get("LIARS_CONTROL_URL", "http://127.0.0.1:8080")
    ))
    parser.add_argument("--api-key", default=os.environ.get("LIARS_CONTROL_API_KEY", "local-dev-key-change-me"))
    parser.add_argument("--worker-id", default=f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}")
    parser.add_argument("--once", action="store_true", help="process at most one claimed run, then exit")
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument("--retry-delay", type=float, default=0.5)
    args = parser.parse_args(argv)
    if args.poll_seconds <= 0 or args.retry_delay < 0:
        parser.error("poll-seconds must be positive and retry-delay must be nonnegative")
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        worker = Worker(
            ControlClient(args.server, args.api_key), args.worker_id, retry_delay=args.retry_delay
        )
        while True:
            try:
                status = worker.run_once()
            except ControlPlaneError as exc:
                event("worker_control_error", code=exc.code, status=exc.status)
                if args.once or exc.status in (400, 401, 403, 404):
                    return 1
                time.sleep(args.poll_seconds)
                continue
            if args.once:
                return 0 if status in ("IDLE", "SUCCEEDED") else 1
            if status == "IDLE":
                time.sleep(args.poll_seconds)
    except ValueError as exc:
        parser.error(str(exc))
    except KeyboardInterrupt:
        event("worker_stopped")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
