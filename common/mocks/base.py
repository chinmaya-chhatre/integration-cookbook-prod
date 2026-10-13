"""A tiny HTTP server for standing in for a customer's system, with failure injection.

Recipes forward webhooks to Jira-, CRM- or warehouse-style APIs. Real ones are slow to set up,
rate-limited and hard to make fail on demand, so the tests use these. Each mock keeps every
request it receives and can be told to misbehave for the next few requests:

    with JiraMock() as jira:
        jira.fail_next(2)                    # next two requests answer 500
        jira.rate_limit_next(1, retry_after=1)  # then one 429 with Retry-After: 1
        jira.slow_next(1, seconds=0.5)       # then one request that takes 0.5 s

Faults are consumed in the order they were queued, one per request, before the real handler
runs. Only the standard library is used.
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


@dataclass(frozen=True)
class Fault:
    """One scripted misbehaviour. ``status=None`` means "delay, then behave normally"."""

    status: int | None = None
    delay: float = 0.0
    retry_after: int | None = None
    body: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RecordedRequest:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes
    faulted: bool = False

    def json(self) -> Any:
        return json.loads(self.body)


@dataclass(frozen=True)
class Reply:
    status: int
    body: Any = None
    headers: dict[str, str] = field(default_factory=dict)


class MockServer:
    """Base class: subclasses implement ``handle``; everything else is shared."""

    def __init__(self) -> None:
        self.requests: list[RecordedRequest] = []
        self._faults: deque[Fault] = deque()
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> MockServer:
        mock = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:  # keep test output quiet
                pass

            def _serve(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                headers = {k.lower(): v for k, v in self.headers.items()}
                reply = mock._dispatch(self.command, self.path, headers, body)
                payload = b"" if reply.body is None else json.dumps(reply.body).encode()
                self.send_response(reply.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                for name, value in reply.headers.items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _serve

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
        if self._thread:
            self._thread.join(timeout=5)
        self._server = self._thread = None

    def __enter__(self) -> MockServer:
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    @property
    def url(self) -> str:
        assert self._server, "start the mock first"
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    # ------------------------------------------------------------ failure injection

    def fail_next(self, count: int = 1, status: int = 500) -> None:
        """The next ``count`` requests answer ``status`` (default 500) without being handled."""
        self._queue(Fault(status=status, body={"error": f"injected {status}"}), count)

    def rate_limit_next(self, count: int = 1, retry_after: int = 1) -> None:
        """The next ``count`` requests answer 429 with a ``Retry-After`` header."""
        self._queue(Fault(status=429, retry_after=retry_after, body={"error": "rate limited"}), count)

    def slow_next(self, count: int = 1, seconds: float = 1.0) -> None:
        """The next ``count`` requests wait ``seconds`` and are then handled normally."""
        self._queue(Fault(delay=seconds), count)

    def clear_faults(self) -> None:
        with self._lock:
            self._faults.clear()

    def _queue(self, fault: Fault, count: int) -> None:
        with self._lock:
            self._faults.extend([fault] * count)

    # ------------------------------------------------------------------- dispatching

    def _dispatch(self, method: str, path: str, headers: dict[str, str], body: bytes) -> Reply:
        with self._lock:
            fault = self._faults.popleft() if self._faults else None
        if fault and fault.delay:
            time.sleep(fault.delay)
        faulted = bool(fault and fault.status)
        with self._lock:
            self.requests.append(RecordedRequest(method, path, headers, body, faulted))
        if fault and fault.status:
            extra = {"Retry-After": str(fault.retry_after)} if fault.retry_after is not None else {}
            return Reply(fault.status, fault.body, extra)
        with self._lock:  # the mocks keep plain dicts; one request at a time keeps them consistent
            return self.handle(method, path, headers, body)

    def handle(self, method: str, path: str, headers: dict[str, str], body: bytes) -> Reply:
        raise NotImplementedError

    # --------------------------------------------------------------------- inspection

    def handled(self) -> list[RecordedRequest]:
        """Requests that reached the real handler (not swallowed by an injected failure)."""
        return [r for r in self.requests if not r.faulted]

    @staticmethod
    def parse(body: bytes) -> Any:
        try:
            return json.loads(body) if body else {}
        except ValueError:
            return None
