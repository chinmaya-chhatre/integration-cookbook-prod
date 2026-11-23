"""Exercise the packaged SMS runtime over real HTTP and graceful termination."""

import importlib.util
import os
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import pytest

from common.fixtures import SECRETS, twilio_inbound_sms

ROOT = Path(__file__).parents[1] / "recipes/twilio-sms-to-support-ticket"
PUBLIC = "https://public.example.test/hooks/twilio"


@pytest.fixture
def process(tmp_path, monkeypatch):
    for key in ("ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        monkeypatch.delenv(key, raising=False)
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
        SUPPORT_TICKET_URL="http://127.0.0.1:1/tickets",
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
