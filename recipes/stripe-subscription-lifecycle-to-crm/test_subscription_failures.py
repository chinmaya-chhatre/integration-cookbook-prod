"""Signed deliveries under CRM faults, redelivery, reordering and manual replay."""

import json
from pathlib import Path

import pytest

from common.fixtures import stripe_subscription_lifecycle
from common.mocks.base import Reply

RULE = "subscription-lifecycle"
ATTEMPTS = json.loads(Path(__file__).with_name("rules.json").read_text())["rules"][0]["retry"]["attempts"]


def send(client, **kwargs):
    request = stripe_subscription_lifecycle(**kwargs)
    response = client.post("/hooks/stripe", content=request.body, headers=request.all_headers())
    assert response.status_code == 202
    assert response.json()["verification"] == "valid"
    assert response.json()["forwarded_to"] == [RULE]


def statuses(client, event_id=1):
    response = client.get(f"/api/events/{event_id}/forwards")
    assert response.status_code == 200
    return [f["status_code"] for f in response.json()]


def writes(crm):
    return [r for r in crm.requests if r.method == "PUT"]


def resend(client, event_id=1):
    response = client.post(f"/api/events/{event_id}/forwards/{RULE}/resend")
    assert response.status_code == 200
    assert response.json()["status_code"] == 200
    assert response.json()["ok"] is True


def test_duplicate_delivery_and_resend_do_not_write_twice(lifecycle):
    client, crm = lifecycle
    send(client)
    send(client)
    resend(client)
    assert statuses(client, 1) == [200, 200]
    assert statuses(client, 2) == [200]
    assert len(writes(crm)) == 1
    assert len(crm.subscriptions) == 1


def test_older_creation_cannot_replace_newer_update(lifecycle):
    client, crm = lifecycle
    send(client, action="updated", status="active", created=200, event_id="evt_new")
    send(client, created=100, event_id="evt_old")
    assert statuses(client, 2) == [200]
    assert crm.subscriptions["sub_demo"]["event_id"] == "evt_new"
    assert crm.subscriptions["sub_demo"]["stage"] == "customer"
    assert len(writes(crm)) == 1


def test_cancellation_cannot_be_revived_by_later_snapshot(lifecycle):
    client, crm = lifecycle
    send(client, action="deleted", status="canceled", created=200, event_id="evt_end")
    send(client, action="updated", status="active", created=201, event_id="evt_revive")
    assert statuses(client, 2) == [200]
    assert crm.subscriptions["sub_demo"]["status"] == "canceled"
    assert len(writes(crm)) == 1


@pytest.mark.parametrize("method", ["GET", "PUT"])
@pytest.mark.parametrize("code", [429, 500])
def test_transient_crm_read_or_write_is_retried(lifecycle, monkeypatch, method, code):
    client, crm = lifecycle
    original = crm.handle
    remaining = 1

    def fault(verb, path, headers, body):
        nonlocal remaining
        if verb == method and remaining:
            remaining -= 1
            return Reply(code, {"error": "temporary failure"}, {"Retry-After": "1"})
        return original(verb, path, headers, body)

    monkeypatch.setattr(crm, "handle", fault)
    send(client)
    assert statuses(client) == [502, 200]
    assert crm.subscriptions["sub_demo"]["stage"] == "trial"
    assert len(writes(crm)) == (2 if method == "PUT" else 1)
    assert client.get("/api/dead-letters").json() == []


@pytest.mark.parametrize("code", [429, 500])
def test_outage_exhausts_retries_then_replay_recovers_once(lifecycle, code):
    client, crm = lifecycle
    crm.fail_next(ATTEMPTS, status=code)
    send(client)
    assert statuses(client) == [502] * ATTEMPTS
    assert crm.subscriptions == {}
    assert len(crm.requests) == ATTEMPTS
    [dead] = client.get("/api/dead-letters").json()
    assert (dead["event_id"], dead["rule"], dead["resendable"]) == (1, RULE, True)
    crm.clear_faults()
    resend(client)
    resend(client)
    assert statuses(client) == [502] * ATTEMPTS + [200, 200]
    assert len(writes(crm)) == 1
    assert crm.subscriptions["sub_demo"]["stage"] == "trial"
    assert client.get("/api/dead-letters").json() == []


def test_replaying_old_dead_letter_cannot_regress_newer_state(lifecycle):
    client, crm = lifecycle
    crm.fail_next(ATTEMPTS)
    send(client, created=100, event_id="evt_old")
    assert statuses(client) == [502] * ATTEMPTS
    send(client, action="updated", status="active", created=200, event_id="evt_new")
    resend(client)
    assert statuses(client) == [502] * ATTEMPTS + [200]
    assert crm.subscriptions["sub_demo"]["event_id"] == "evt_new"
    assert crm.subscriptions["sub_demo"]["stage"] == "customer"
    assert len(writes(crm)) == 1
    assert client.get("/api/dead-letters").json() == []


def test_accepted_write_with_error_response_is_reconciled_on_retry(lifecycle, monkeypatch):
    client, crm = lifecycle
    original = crm.handle

    def accepted_but_failed_response(method, path, headers, body):
        reply = original(method, path, headers, body)
        if method == "PUT":
            return Reply(500, {"error": "acceptance response unavailable"})
        return reply

    monkeypatch.setattr(crm, "handle", accepted_but_failed_response)
    send(client)
    assert statuses(client) == [502, 200]
    assert len(writes(crm)) == 1
    assert crm.subscriptions["sub_demo"]["event_id"] == "evt_subscriptionDemo"
    assert client.get("/api/dead-letters").json() == []


def test_timestamp_collision_is_permanent_and_never_overwrites(lifecycle):
    client, crm = lifecycle
    send(client, event_id="evt_first")
    send(client, action="updated", status="active", event_id="evt_sameSecond")
    assert statuses(client, 2) == [422]
    [dead] = client.get("/api/dead-letters").json()
    assert (dead["event_id"], dead["status_code"]) == (2, 422)
    assert crm.subscriptions["sub_demo"]["event_id"] == "evt_first"
    assert len(writes(crm)) == 1


def test_permanent_crm_validation_failure_is_not_retried(lifecycle, monkeypatch):
    client, crm = lifecycle
    original = crm.handle

    def reject(method, path, headers, body):
        return (
            Reply(422, {"error": "invalid CRM fields"})
            if method == "PUT"
            else original(method, path, headers, body)
        )

    monkeypatch.setattr(crm, "handle", reject)
    send(client)
    assert statuses(client) == [422]
    assert len(writes(crm)) == 1
    assert crm.subscriptions == {}
    [dead] = client.get("/api/dead-letters").json()
    assert dead["status_code"] == 422


@pytest.mark.parametrize("tampered", [False, True])
def test_invalid_signature_is_stored_and_never_forwarded(lifecycle, tampered):
    client, crm = lifecycle
    request = stripe_subscription_lifecycle()
    body = request.body + b" " if tampered else request.body
    headers = request.all_headers()
    if not tampered:
        headers["Stripe-Signature"] = headers["Stripe-Signature"].split(",v1=")[0] + ",v1=" + "0" * 64
    response = client.post("/hooks/stripe", content=body, headers=headers)
    assert response.status_code == 401
    assert response.json()["verification"] == "invalid"
    assert statuses(client) == []
    assert client.get("/api/events/1").json()["verification"] == "invalid"
    assert crm.requests == []
