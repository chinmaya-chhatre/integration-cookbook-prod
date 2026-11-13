"""Signed lifecycle snapshots through HookScope, the HTTP handler and the CRM mock."""

import importlib.util
import json
import sys
from pathlib import Path
from threading import Thread

import pytest

from common.fixtures import SECRETS, stripe_subscription_lifecycle
from common.mocks.crm import CrmMock

RECIPE = Path(__file__).parent
SPEC = importlib.util.spec_from_file_location("subscription_happy_handler", RECIPE / "handler" / "app.py")
handler_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = handler_module
SPEC.loader.exec_module(handler_module)


@pytest.fixture
def lifecycle(tmp_path):
    pytest.importorskip("hookscope", reason="install HookScope to run the end-to-end tests")
    from fastapi.testclient import TestClient
    from hookscope.main import create_app
    from hookscope.rules import parse_rules
    from hookscope.store import EventStore

    rules = json.loads((RECIPE / "rules.json").read_text())
    with CrmMock() as crm:
        settings = handler_module.Settings(crm.url, "demo-token", str(tmp_path / "state.db"), 2)
        with handler_module.make_server(handler_module.Handler(settings), 0) as server:
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                rules["rules"][0]["target_url"] = f"http://127.0.0.1:{server.server_port}/stripe/subscription"
                app = create_app(
                    store=EventStore(str(tmp_path / "events.db")),
                    secrets={"stripe": SECRETS["stripe"]},
                    rules=parse_rules(rules),
                    sleep=lambda seconds: None,
                    admin_password=None,
                )
                with TestClient(app) as client:
                    yield client, crm
            finally:
                server.shutdown()
                thread.join()


def send(client, event_number=1, **kwargs):
    request = stripe_subscription_lifecycle(**kwargs)
    response = client.post("/hooks/stripe", content=request.body, headers=request.all_headers())
    assert response.status_code == 202
    assert response.json()["verification"] == "valid"
    assert response.json()["forwarded_to"] == ["subscription-lifecycle"]
    forwards = client.get(f"/api/events/{event_number}/forwards")
    assert forwards.status_code == 200
    assert [(f["rule"], f["status_code"], f["attempt"]) for f in forwards.json()] == [
        ("subscription-lifecycle", 200, 1)
    ]
    return json.loads(request.body)


@pytest.mark.parametrize(
    "action,status,stage",
    [
        ("created", "trialing", "trial"),
        ("updated", "active", "customer"),
        ("deleted", "canceled", "inactive"),
    ],
)
def test_each_configured_topic_reaches_crm(lifecycle, action, status, stage):
    client, crm = lifecycle
    source = send(client, action=action, status=status)
    assert crm.subscriptions == {
        "sub_demo": {
            "id": "sub_demo",
            "customer_id": "cus_demo",
            "status": status,
            "stage": stage,
            "event_id": source["id"],
            "version": source["created"],
        }
    }
    assert [(r.method, r.path) for r in crm.requests] == [
        ("GET", "/subscriptions/sub_demo"),
        ("PUT", "/subscriptions/sub_demo"),
    ]
    assert all(r.headers["authorization"] == "Bearer demo-token" for r in crm.requests)
    assert client.get("/api/dead-letters").json() == []


@pytest.mark.parametrize(
    "status,stage",
    [
        ("incomplete", "pending"),
        ("unpaid", "at_risk"),
        ("paused", "at_risk"),
        ("incomplete_expired", "inactive"),
    ],
)
def test_additional_supported_statuses_are_mapped(lifecycle, status, stage):
    client, crm = lifecycle
    send(client, action="updated", status=status)
    assert crm.subscriptions["sub_demo"]["stage"] == stage
    assert crm.subscriptions["sub_demo"]["status"] == status


def test_trial_payment_problem_recovery_and_cancellation(lifecycle):
    client, crm = lifecycle
    steps = [
        ("created", "trialing", "trial"),
        ("updated", "active", "customer"),
        ("updated", "past_due", "at_risk"),
        ("updated", "active", "customer"),
        ("deleted", "canceled", "inactive"),
    ]
    for number, (action, status, stage) in enumerate(steps, 1):
        send(
            client,
            event_number=number,
            action=action,
            status=status,
            created=1_760_000_000 + number,
            event_id=f"evt_step{number}",
        )
        assert crm.subscriptions["sub_demo"]["stage"] == stage
        assert crm.subscriptions["sub_demo"]["version"] == 1_760_000_000 + number
    assert len([r for r in crm.requests if r.method == "PUT"]) == len(steps)
    assert len(crm.subscriptions) == 1
    assert client.get("/api/dead-letters").json() == []


def test_distinct_subscriptions_keep_independent_records(lifecycle):
    client, crm = lifecycle
    send(client, subscription_id="sub_one", customer_id="cus_one", event_id="evt_one")
    send(
        client,
        event_number=2,
        subscription_id="sub_two",
        customer_id="cus_two",
        event_id="evt_two",
        action="updated",
        status="active",
    )
    assert set(crm.subscriptions) == {"sub_one", "sub_two"}
    assert crm.subscriptions["sub_one"]["customer_id"] == "cus_one"
    assert crm.subscriptions["sub_one"]["stage"] == "trial"
    assert crm.subscriptions["sub_two"]["customer_id"] == "cus_two"
    assert crm.subscriptions["sub_two"]["stage"] == "customer"
