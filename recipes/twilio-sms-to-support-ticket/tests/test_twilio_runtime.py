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
from threading import Event, Lock, Thread
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl
from urllib.request import Request, urlopen

import pytest

from common import signing
from common.fixtures import SECRETS, twilio_inbound_sms

ROOT = Path(__file__).parents[1]
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
def fault_support():
    """A target retaining accepted tickets while the client process is restarted."""
    tickets = {}
    attempts = []
    lock = Lock()
    faults = {"statuses": [], "hold_after_accept": False}
    accepted, release = Event(), Event()

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
                status = faults["statuses"].pop(0) if faults["statuses"] else None
                if status is not None:
                    self.send_response(status)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                previous = tickets.get(key)
                if previous is not None and previous != payload:
                    self.send_error(409)
                    return
                tickets[key] = payload
                hold = faults["hold_after_accept"]
                faults["hold_after_accept"] = False
            if hold:
                accepted.set()
                release.wait(timeout=10)
                self.close_connection = True
                return
            self.send_response(200 if previous is not None else 201)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/tickets", tickets, attempts, faults, accepted, release
    finally:
        release.set()
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.fixture
def support(fault_support):
    return fault_support[:3]


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


def wait_for_checkpoint(database, state):
    """Observe a committed worker result without altering time or retry storage."""
    for _ in range(200):
        with sqlite3.connect(database) as db:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE name = 'ticket_retries'").fetchone()
            rows = db.execute("SELECT state FROM tickets").fetchall()
            attempts = db.execute("SELECT attempts FROM ticket_retries").fetchall() if exists else []
        if rows == [(state,)] and attempts:
            return attempts
        time.sleep(0.05)
    pytest.fail(f"worker never committed {state}; states={rows}, attempts={attempts}")


@pytest.mark.parametrize("status", [429, 500])
def test_runtime_retries_target_failure_across_restart(launch, fault_support, status):
    endpoint, tickets, attempts, faults, _, _ = fault_support
    faults["statuses"] = [status]
    request = twilio_inbound_sms(PUBLIC, body="Retain this message during the outage")
    with launch(endpoint) as (_, base, database):
        assert post(base, "/twilio/sms", request) == (200, b"<Response/>")
        assert wait_for_checkpoint(database, "pending") == [(1,)]
        assert tickets == {}
        assert len(attempts) == 1
    with launch(endpoint) as (_, base, database):
        # Provider redelivery must not reset the persisted retry budget or identity.
        assert post(base, "/twilio/sms", request) == (200, b"<Response/>")
        wait_for_delivery(database, 1)
        assert wait_for_checkpoint(database, "delivered") == [(2,)]
    assert len(tickets) == 1
    assert len(attempts) == 2
    assert attempts[0] == attempts[1]
    assert attempts[1][0] in tickets


def test_permanent_target_failure_stays_in_review_after_restart(launch, fault_support):
    endpoint, tickets, attempts, faults, _, _ = fault_support
    faults["statuses"] = [400]
    request = twilio_inbound_sms(PUBLIC)
    with launch(endpoint) as (_, base, database):
        assert post(base, "/twilio/sms", request)[0] == 200
        assert wait_for_checkpoint(database, "review") == [(1,)]
    with launch(endpoint) as (_, base, database):
        assert post(base, "/twilio/sms", request)[0] == 200
        time.sleep(0.75)
        assert wait_for_checkpoint(database, "review") == [(1,)]
    assert tickets == {}
    assert len(attempts) == 1


def test_crash_after_target_acceptance_reuses_identity_without_duplicate_ticket(launch, fault_support):
    endpoint, tickets, attempts, faults, accepted, release = fault_support
    faults["hold_after_accept"] = True
    request = twilio_inbound_sms(PUBLIC, body="Accepted before worker crash")
    with launch(endpoint) as (child, base, database):
        assert post(base, "/twilio/sms", request) == (200, b"<Response/>")
        assert accepted.wait(timeout=5), "target never accepted the request"
        assert len(tickets) == len(attempts) == 1
        # Kill while the sender is awaiting its response, before SQLite can commit delivery.
        child.kill()
        child.wait(timeout=5)
        release.set()
        with sqlite3.connect(database) as db:
            assert db.execute("SELECT state FROM tickets").fetchall() == [("pending",)]
    with launch(endpoint) as (_, base, database):
        wait_for_delivery(database, 1)
        assert post(base, "/twilio/sms", request)[0] == 200
    assert len(tickets) == 1
    assert len(attempts) == 2
    assert attempts[0] == attempts[1]
