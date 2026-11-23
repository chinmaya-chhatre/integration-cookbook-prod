"""Exercise the packaged SMS runtime over real HTTP and graceful termination."""

import importlib.util
import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock, Thread
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl
from urllib.request import Request, urlopen

import pytest

from common import signing
from common.fixtures import SECRETS, twilio_inbound_sms

ROOT = Path(__file__).parents[1] / "recipes/twilio-sms-to-support-ticket"
PUBLIC = "https://public.example.test/hooks/twilio"


@pytest.fixture
def launch(tmp_path, monkeypatch):
    for key in ("ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        monkeypatch.delenv(key, raising=False)

    @contextmanager
    def start(target_url):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        database = tmp_path / "tickets.db"
        env = dict(
            os.environ,
            PORT=str(port),
            TICKET_STATE_DB=str(database),
            TWILIO_PUBLIC_URL=PUBLIC,
            TWILIO_AUTH_TOKEN=SECRETS["twilio"],
            SUPPORT_TICKET_URL=target_url,
            SUPPORT_TOKEN="demo",
        )
        child = subprocess.Popen(
            [sys.executable, str(ROOT / "runtime.py")],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        base = f"http://127.0.0.1:{port}"
        try:
            for _ in range(100):
                if child.poll() is not None:
                    pytest.fail("runtime exited before becoming healthy")
                try:
                    with urlopen(base + "/healthz", timeout=0.2) as response:
                        assert response.read() == b"ok"
                    break
                except (URLError, TimeoutError):
                    time.sleep(0.05)
            else:
                pytest.fail("runtime did not become healthy")
            yield child, base, database
        finally:
            if child.poll() is None:
                child.terminate()
            try:
                child.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                child.kill()
                child.communicate()
                pytest.fail("runtime did not stop within its bounded request timeout")

    return start


@pytest.fixture
def process(launch):
    with launch("http://127.0.0.1:1/tickets") as running:
        yield running


def post(base, path, request, signature=None):
    headers = request.all_headers()
    headers["X-Twilio-Signature"] = signature or headers["X-Twilio-Signature"]
    message = Request(base + path, data=request.body, headers=headers, method="POST")
    try:
        with urlopen(message, timeout=12) as response:
            return response.status, response.read()
    except HTTPError as error:
        with error:
            return error.code, error.read()


def test_http_accepts_duplicate_durably_during_target_outage(process):
    child, base, database = process
    request = twilio_inbound_sms(PUBLIC, body="नमस्कार + &")
    assert post(base, "/twilio/sms", request) == (200, b"<Response/>")
    assert post(base, "/twilio/sms", request) == (200, b"<Response/>")
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT state FROM tickets").fetchall() == [("pending",)]
    for _ in range(100):
        with sqlite3.connect(database) as db:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE name = 'ticket_retries'").fetchone()
            attempted = exists and db.execute("SELECT COUNT(*) FROM ticket_retries").fetchone()[0]
        if attempted:
            break
        time.sleep(0.05)
    else:
        pytest.fail("worker never attempted the accepted ticket")
    child.terminate()
    child.wait(timeout=15)
    assert child.returncode == 0


def test_http_rejects_wrong_routes_queries_and_signature(process):
    _, base, database = process
    request = twilio_inbound_sms(PUBLIC)
    assert post(base, "/unexpected", request)[0] == 404
    assert post(base, "/twilio/sms?unexpected=1", request)[0] == 404
    assert post(base, "/twilio/sms", request, signature="wrong")[0] == 403
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 0


def test_missing_secret_fails_before_listening(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(ROOT))
    spec = importlib.util.spec_from_file_location("sms_runtime", ROOT / "runtime.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("TICKET_STATE_DB", str(tmp_path / "tickets.db"))
    monkeypatch.setenv("TWILIO_PUBLIC_URL", PUBLIC)
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "")
    try:
        with pytest.raises(ValueError, match="auth token"):
            module.main()
    finally:
        for name in ("delivery", "ingress", "sender", "transform", "worker"):
            sys.modules.pop(name, None)


@pytest.fixture
def support():
    """A vendor-neutral HTTP contract double with stable-key deduplication."""
    tickets = {}
    attempts = []
    lock = Lock()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            payload = json.loads(raw)
            key = self.headers.get("Idempotency-Key")
            if self.path != "/tickets" or self.headers.get("Authorization") != "Bearer demo":
                self.send_error(403)
                return
            if not key or key != payload.get("external_reference"):
                self.send_error(400)
                return
            with lock:
                attempts.append((key, payload, self.headers.get("Content-Type")))
                previous = tickets.get(key)
                if previous is not None and previous != payload:
                    self.send_error(409)
                    return
                tickets[key] = payload
            self.send_response(200 if previous is not None else 201)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/tickets", tickets, attempts
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def wait_for_delivery(database, expected):
    for _ in range(200):
        with sqlite3.connect(database) as db:
            rows = db.execute("SELECT identity, state FROM tickets ORDER BY identity").fetchall()
        if len(rows) == expected and all(state == "delivered" for _, state in rows):
            return rows
        time.sleep(0.05)
    pytest.fail(f"expected {expected} delivered tickets; last persisted states: {rows}")


@pytest.mark.parametrize("text", ["My order never arrived", "नमस्कार 🌍 + & = %", ""])
def test_signed_http_delivery_preserves_text_and_target_contract(launch, support, text):
    endpoint, tickets, attempts = support
    request = twilio_inbound_sms(PUBLIC, body=text)
    identity = "twilio:AC00000000000000000000000000000001:SM00000000000000000000000000000001"
    with launch(endpoint) as (_, base, database):
        assert post(base, "/twilio/sms", request) == (200, b"<Response/>")
        assert wait_for_delivery(database, 1) == [(identity, "delivered")]
    assert tickets == {
        identity: {
            "external_reference": identity,
            "summary": "Inbound SMS SM00000000000000000000000000000001",
            "description": text,
            "contact": "+16175550123",
            "destination": "+18005551212",
            "provider": "twilio",
        }
    }
    assert attempts == [(identity, tickets[identity], "application/json")]


def test_signed_redelivery_and_restart_create_one_ticket_then_accept_new_message(launch, support):
    endpoint, tickets, attempts = support
    first = twilio_inbound_sms(PUBLIC, body="First message")
    with launch(endpoint) as (_, base, database):
        assert post(base, "/twilio/sms", first)[0] == 200
        wait_for_delivery(database, 1)
        assert post(base, "/twilio/sms", first)[0] == 200
    # Restart the actual ingress and worker against the same database.
    with launch(endpoint) as (_, base, database):
        assert post(base, "/twilio/sms", first)[0] == 200
        fields = dict(parse_qsl(first.body.decode(), keep_blank_values=True))
        fields["MessageSid"] = "SM00000000000000000000000000000002"
        fields["Body"] = "Second message from the same sender"
        second = signing.twilio(fields, SECRETS["twilio"], PUBLIC)
        assert post(base, "/twilio/sms", second) == (200, b"<Response/>")
        rows = wait_for_delivery(database, 2)
    assert len(tickets) == len(attempts) == 2
    assert {payload["description"] for payload in tickets.values()} == {
        "First message",
        "Second message from the same sender",
    }
    assert {key for key, _ in rows} == set(tickets)
