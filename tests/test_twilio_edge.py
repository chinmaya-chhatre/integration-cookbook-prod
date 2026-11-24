"""The TwiML edge requires a confirmed durable forward, not just a capture receipt."""

import importlib.util
import io
import json
import sys
from urllib.error import HTTPError

import pytest

from common.fixtures import twilio_inbound_sms

PUBLIC = "https://sms.example.test/hooks/twilio"
HANDLER = "http://handler:8080/twilio/sms"


@pytest.fixture
def modules(monkeypatch):
    from pathlib import Path

    root = Path(__file__).parents[1] / "recipes/twilio-sms-to-support-ticket"
    monkeypatch.syspath_prepend(str(root))
    loaded = []
    for name in ("gateway", "support_mock"):
        spec = importlib.util.spec_from_file_location("sms_" + name, root / (name + ".py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        loaded.append(module)
    yield loaded
    for name in ("runtime", "sender", "delivery", "ingress", "transform", "worker"):
        sys.modules.pop(name, None)


def invoke(app, raw=b"", path="/hooks/twilio", method="POST", signature="signed", **extra):
    environ = {
        "PATH_INFO": path,
        "QUERY_STRING": "",
        "REQUEST_METHOD": method,
        "CONTENT_TYPE": "application/x-www-form-urlencoded",
        "CONTENT_LENGTH": str(len(raw)),
        "HTTP_X_TWILIO_SIGNATURE": signature,
        "wsgi.input": io.BytesIO(raw),
        **extra,
    }
    response = []
    body = b"".join(app(environ, lambda status, headers: response.append((status, headers))))
    return int(response[0][0].split()[0]), body


def backend(gateway):
    return gateway.HookScope(PUBLIC, "http://hookscope:8000", "private-test-admin", HANDLER)


def test_edge_waits_for_matching_handler_forward_and_fixes_signed_url_headers(modules, monkeypatch):
    gateway, _ = modules
    scope = backend(gateway)
    calls, polls = [], []

    def read(request):
        calls.append(request)
        if request.get_method() == "POST":
            return {"id": 12, "verification": "valid", "forwarded_to": ["inbound-sms"]}
        polls.append(1)
        return [] if len(polls) == 1 else [{"rule": "inbound-sms", "target_url": HANDLER, "status_code": 200}]

    monkeypatch.setattr(scope, "read", read)
    monkeypatch.setattr(gateway.time, "sleep", lambda _: None)
    request = twilio_inbound_sms(PUBLIC)
    assert invoke(
        gateway.application(PUBLIC, scope),
        request.body,
        signature=request.all_headers()["X-Twilio-Signature"],
        HTTP_HOST="attacker.test",
        HTTP_X_FORWARDED_PROTO="http",
    ) == (200, b"<Response/>")
    assert len(polls) == 2
    assert calls[0].full_url == "http://hookscope:8000/hooks/twilio"
    headers = {k.lower(): v for k, v in calls[0].header_items()}
    assert headers["host"] == "sms.example.test"
    assert headers["x-forwarded-proto"] == "https"
    assert "authorization" not in headers
    assert calls[1].get_header("Authorization") == "Bearer private-test-admin"
    assert calls[1].full_url.endswith("/api/events/12/forwards")


@pytest.mark.parametrize(
    "row",
    [
        None,
        {"rule": "other", "target_url": HANDLER, "status_code": 200},
        {"rule": "inbound-sms", "target_url": "http://other/", "status_code": 200},
        {"rule": "inbound-sms", "target_url": HANDLER, "status_code": None},
    ],
)
def test_capture_alone_never_acknowledges_durable_acceptance(modules, monkeypatch, row):
    gateway, _ = modules
    scope = backend(gateway)
    times = iter([0, 0, 3])
    monkeypatch.setattr(gateway.time, "monotonic", lambda: next(times))
    monkeypatch.setattr(gateway.time, "sleep", lambda _: None)
    monkeypatch.setattr(
        scope,
        "read",
        lambda request: (
            {"id": 1, "verification": "valid", "forwarded_to": ["inbound-sms"]}
            if request.get_method() == "POST"
            else ([] if row is None else [row])
        ),
    )
    assert scope.accept(b"body", "signature") == 503


@pytest.mark.parametrize(
    "receipt",
    [
        {"id": 1, "verification": "no_secret", "forwarded_to": ["inbound-sms"]},
        {"id": 1, "verification": "valid", "forwarded_to": []},
        {"id": "../admin", "verification": "valid", "forwarded_to": ["inbound-sms"]},
    ],
)
def test_unverified_unmatched_or_invalid_receipts_fail_closed(modules, monkeypatch, receipt):
    gateway, _ = modules
    scope = backend(gateway)
    monkeypatch.setattr(scope, "read", lambda request: receipt)
    assert scope.accept(b"body", "signature") == 503


@pytest.mark.parametrize("status", [400, 403, 409, 413, 415])
def test_permanent_handler_rejections_reach_the_provider(modules, monkeypatch, status):
    gateway, _ = modules
    scope = backend(gateway)
    monkeypatch.setattr(
        scope,
        "read",
        lambda request: (
            {"id": 1, "verification": "valid", "forwarded_to": ["inbound-sms"]}
            if request.get_method() == "POST"
            else [{"rule": "inbound-sms", "target_url": HANDLER, "status_code": status}]
        ),
    )
    assert scope.accept(b"body", "signature") == status


@pytest.mark.parametrize(
    "error,status",
    [
        (HTTPError("http://hookscope/", 401, "invalid", {}, None), 403),
        (OSError("unreachable"), 503),
        (ValueError("bad JSON"), 503),
    ],
)
def test_backend_errors_never_produce_success(modules, monkeypatch, error, status):
    gateway, _ = modules
    scope = backend(gateway)

    def fail(request):
        raise error

    monkeypatch.setattr(scope, "read", fail)
    assert scope.accept(b"body", "signature") == status


@pytest.mark.parametrize(
    "extra,status",
    [
        ({"path": "/wrong"}, 404),
        ({"QUERY_STRING": "unexpected=1"}, 404),
        ({"method": "GET"}, 405),
        ({"CONTENT_TYPE": "application/json"}, 415),
        ({"signature": ""}, 403),
        ({"CONTENT_LENGTH": "999999"}, 413),
    ],
)
def test_edge_rejects_invalid_requests_before_backend_calls(modules, monkeypatch, extra, status):
    gateway, _ = modules
    scope = backend(gateway)
    monkeypatch.setattr(scope, "accept", lambda *_: pytest.fail("must not reach backend"))
    assert invoke(gateway.application(PUBLIC, scope), twilio_inbound_sms(PUBLIC).body, **extra)[0] == status


def test_edge_rejects_duplicate_form_fields_before_backend_calls(modules, monkeypatch):
    gateway, _ = modules
    scope = backend(gateway)
    monkeypatch.setattr(scope, "accept", lambda *_: pytest.fail("must not reach backend"))
    raw = twilio_inbound_sms(PUBLIC).body + b"&Body=other"
    assert invoke(gateway.application(PUBLIC, scope), raw)[0] == 400


@pytest.mark.parametrize(
    "values",
    [
        ("http://sms.test/hooks/twilio", "http://hookscope", "token", HANDLER),
        ("https://sms.test/wrong", "http://hookscope", "token", HANDLER),
        (PUBLIC, "http://hookscope/other", "token", HANDLER),
        (PUBLIC, "http://hookscope", "", HANDLER),
        (PUBLIC + "?route=1", "http://hookscope", "token", HANDLER),
    ],
)
def test_invalid_edge_configuration_is_rejected(modules, values):
    gateway, _ = modules
    with pytest.raises(ValueError):
        gateway.HookScope(*values)


def test_admin_poll_authentication_error_is_backend_failure(modules, monkeypatch):
    gateway, _ = modules
    scope = backend(gateway)

    def read(request):
        if request.get_method() == "POST":
            return {"id": 1, "verification": "valid", "forwarded_to": ["inbound-sms"]}
        raise HTTPError(request.full_url, 401, "invalid admin token", {}, None)

    monkeypatch.setattr(scope, "read", read)
    assert scope.accept(b"body", "signature") == 503


def test_support_mock_persists_idempotency_and_requires_its_credential(modules, tmp_path):
    _, support = modules
    path = str(tmp_path / "support.db")
    payload = {
        "external_reference": "twilio:account:message",
        "summary": "SMS",
        "description": "",
        "contact": "+100",
        "destination": "+200",
        "provider": "twilio",
    }

    def post(app, value=payload, token="support-test"):
        return invoke(
            app,
            json.dumps(value).encode(),
            path="/tickets",
            CONTENT_TYPE="application/json",
            HTTP_AUTHORIZATION="Bearer " + token,
            HTTP_IDEMPOTENCY_KEY=value["external_reference"],
        )[0]

    app = support.application(path, "support-test")
    assert post(app, token="wrong") == 403
    assert post(app) == 201
    restarted = support.application(path, "support-test")
    assert post(restarted) == 200
    assert post(restarted, {**payload, "description": "different"}) == 409
    code, body = invoke(
        restarted, path="/_mock/tickets", method="GET", HTTP_AUTHORIZATION="Bearer support-test"
    )
    assert code == 200 and json.loads(body) == [payload]
