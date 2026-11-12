"""Core state-machine tests using the CRM mock and durable SQLite files."""

import importlib.util
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Thread
from urllib.request import Request, urlopen

import pytest

from common.mocks.crm import CrmMock

SPEC = importlib.util.spec_from_file_location(
    "subscription_handler", Path(__file__).parent / "handler" / "app.py"
)
app = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = app
SPEC.loader.exec_module(app)


def event(version=100, status="active", event_id="evt_first", topic="updated"):
    return json.dumps(
        {
            "id": event_id,
            "created": version,
            "type": f"customer.subscription.{topic}",
            "data": {
                "object": {
                    "object": "subscription",
                    "id": "sub_demo",
                    "customer": "cus_demo",
                    "status": status,
                }
            },
        }
    ).encode()


@pytest.fixture
def target(tmp_path):
    with CrmMock() as crm:
        settings = app.Settings(crm.url, "demo-token", str(tmp_path / "state.db"), 2)
        yield app.Handler(settings), crm, settings


def test_lifecycle_and_durable_terminal_state(target):
    handler, crm, settings = target
    for version, status, stage in [
        (100, "trialing", "trial"),
        (101, "past_due", "at_risk"),
        (102, "active", "customer"),
        (103, "canceled", "inactive"),
    ]:
        topic = "deleted" if status == "canceled" else "updated"
        assert handler.handle(event(version, status, f"evt_{version}", topic)) == (200, {"result": "applied"})
        assert crm.subscriptions["sub_demo"]["stage"] == stage
    assert app.Handler(settings).handle(event(104, "active", "evt_late")) == (200, {"result": "terminal"})
    assert crm.subscriptions["sub_demo"]["status"] == "canceled"
    assert crm.requests[0].headers["authorization"] == "Bearer demo-token"


def test_duplicate_stale_and_timestamp_collision(target):
    handler, crm, _ = target
    assert handler.handle(event())[0] == 200
    count = len(crm.requests)
    assert handler.handle(event()) == (200, {"result": "duplicate"})
    assert handler.handle(event(99, "trialing", "evt_old")) == (200, {"result": "stale"})
    assert handler.handle(event(100, "past_due", "evt_collision"))[0] == 422
    assert len(crm.requests) == count
    assert crm.subscriptions["sub_demo"]["status"] == "active"


def test_remote_acceptance_before_local_commit(target):
    handler, crm, _ = target
    crm.subscriptions["sub_demo"] = app.snapshot(json.loads(event()))
    assert handler.handle(event()) == (200, {"result": "duplicate"})
    assert [r.method for r in crm.requests] == ["GET"]


@pytest.mark.parametrize("code", [429, 500])
def test_failed_write_remains_retryable(target, monkeypatch, code):
    handler, crm, _ = target
    original = handler.request

    def fail_write(method, key, payload=None):
        if method == "PUT":
            crm.fail_next(status=code)
        return original(method, key, payload)

    monkeypatch.setattr(handler, "request", fail_write)
    assert handler.handle(event())[0] == 502
    assert crm.subscriptions == {}
    monkeypatch.setattr(handler, "request", original)
    assert handler.handle(event()) == (200, {"result": "applied"})


def test_shared_database_serializes_deliveries(target):
    handler, crm, settings = target
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda h: h.handle(event()), [handler, app.Handler(settings)]))
    assert sorted(r[1]["result"] for r in results) == ["applied", "duplicate"]
    assert len([r for r in crm.requests if r.method == "PUT"]) == 1


@pytest.mark.parametrize("field,value", [("created", True), ("created", -1), ("id", "evt_bad/path")])
def test_invalid_envelope_never_calls_crm(target, field, value):
    handler, crm, _ = target
    payload = json.loads(event())
    payload[field] = value
    assert handler.handle(json.dumps(payload).encode())[0] == 422
    assert crm.requests == []


def test_invalid_status_and_contradictory_delete(target):
    handler, crm, _ = target
    assert handler.handle(event(status="unknown"))[0] == 422
    assert handler.handle(event(topic="deleted"))[0] == 422
    assert crm.requests == []


def test_http_endpoint_and_ignored_topic(target):
    handler, crm, _ = target
    with app.make_server(handler, 0) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            with urlopen(base + "/healthz", timeout=2) as response:
                assert response.status == 200
            with urlopen(Request(base + "/stripe/subscription", data=event()), timeout=2) as response:
                assert json.load(response) == {"result": "applied"}
        finally:
            server.shutdown()
            thread.join()
    assert handler.handle(b'{"type":"invoice.paid"}') == (200, {"result": "skipped"})
    assert handler.handle(b"{")[0] == 400
    assert len(crm.subscriptions) == 1
