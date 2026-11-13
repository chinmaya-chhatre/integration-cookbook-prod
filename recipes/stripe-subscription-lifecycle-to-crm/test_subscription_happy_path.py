"""Signed lifecycle snapshots through HookScope, the HTTP handler and the CRM mock."""

import json

import pytest

from common.fixtures import stripe_subscription_lifecycle


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
