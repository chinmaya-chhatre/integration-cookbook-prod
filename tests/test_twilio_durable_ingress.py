import importlib.util
import io
import sqlite3
import sys
from pathlib import Path

import pytest

from common.fixtures import twilio_inbound_sms

ROOT = Path(__file__).parents[1] / "recipes/twilio-sms-to-support-ticket"


@pytest.fixture
def handler(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT))
    spec = importlib.util.spec_from_file_location("sms_ingress", ROOT / "ingress.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    yield module
    for name in ("delivery", "transform"):
        sys.modules.pop(name, None)


def invoke(app, request, **overrides):
    environ = {
        "REQUEST_METHOD": "POST",
        "CONTENT_TYPE": "application/x-www-form-urlencoded",
        "CONTENT_LENGTH": str(len(request.body)),
        "wsgi.input": io.BytesIO(request.body),
        "HTTP_X_TWILIO_SIGNATURE": request.all_headers()["X-Twilio-Signature"],
    }
    environ.update(overrides)
    statuses = []
    body = b"".join(app(environ, lambda status, headers: statuses.append(status)))
    return statuses[0], body


def test_durable_ack_duplicate_and_proxy_headers(handler, tmp_path):
    from common.fixtures import SECRETS

    path = str(tmp_path / "tickets.db")
    url = "https://public.example.com/hooks/twilio"
    app = handler.application(url, SECRETS["twilio"], path)
    request = twilio_inbound_sms(url, body="नमस्कार + &")
    for _ in range(2):
        assert invoke(app, request, HTTP_X_FORWARDED_HOST="attacker.example") == ("200 OK", b"<Response/>")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT state FROM tickets").fetchall() == [("pending",)]
    restored = handler.application(url, SECRETS["twilio"], path)
    assert invoke(restored, request)[0] == "200 OK"


@pytest.mark.parametrize("fault", ["signature", "url", "duplicate", "content_type"])
def test_rejected_requests_have_no_side_effects(handler, tmp_path, fault):
    from common.fixtures import SECRETS

    path = str(tmp_path / "tickets.db")
    url = "https://public.example.com/hooks/twilio"
    app = handler.application(url, SECRETS["twilio"], path)
    request = twilio_inbound_sms(url + "/" if fault == "url" else url)
    overrides = {}
    if fault == "signature":
        overrides["HTTP_X_TWILIO_SIGNATURE"] = "wrong"
    if fault == "duplicate":
        raw = request.body + b"&Body=changed"
        overrides.update({"wsgi.input": io.BytesIO(raw), "CONTENT_LENGTH": str(len(raw))})
    if fault == "content_type":
        overrides["CONTENT_TYPE"] = "application/json"
    assert not invoke(app, request, **overrides)[0].startswith("200")
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM tickets").fetchone()[0] == 0


def test_storage_failure_is_not_acknowledged(handler, tmp_path, monkeypatch):
    from common.fixtures import SECRETS

    url = "https://public.example.com/hooks/twilio"
    app = handler.application(url, SECRETS["twilio"], str(tmp_path / "tickets.db"))

    def fail(*args):
        raise sqlite3.OperationalError("disk full")

    monkeypatch.setattr(handler.Outbox, "enqueue", fail)
    assert invoke(app, twilio_inbound_sms(url))[0] == "503 Service Unavailable"
