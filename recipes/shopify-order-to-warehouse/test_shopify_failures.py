"""Signed deliveries through HookScope, the real handler and a fault-injected warehouse."""

import json
from pathlib import Path
from threading import Thread

import pytest
from handler.app import Handler, Settings, make_server

from common.fixtures import SECRETS, dumps, shopify_order_created
from common.mocks.warehouse import WarehouseMock
from common.signing import shopify


@pytest.fixture
def delivery(tmp_path):
    pytest.importorskip("hookscope", reason="install HookScope to run the end-to-end tests")
    from fastapi.testclient import TestClient
    from hookscope.main import create_app
    from hookscope.rules import parse_rules
    from hookscope.store import EventStore

    rules = json.loads(Path(__file__).with_name("rules.json").read_text())
    with WarehouseMock() as warehouse:
        handler = Handler(Settings(warehouse.url, "", frozenset({"paid"}), 2))
        with make_server(handler, 0) as server:
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                rules["rules"][0]["target_url"] = (
                    f"http://127.0.0.1:{server.server_port}/shopify/order-created"
                )
                app = create_app(
                    store=EventStore(str(tmp_path / "events.db")),
                    secrets={"shopify": SECRETS["shopify"]},
                    rules=parse_rules(rules),
                    sleep=lambda seconds: None,
                    admin_password=None,
                )
                with TestClient(app) as client:
                    yield client, warehouse, rules["rules"][0]["retry"]["attempts"]
            finally:
                server.shutdown()
                thread.join()


def send(client, **changes):
    payload = json.loads(shopify_order_created().body)
    payload.update(changes)
    request = shopify(dumps(payload), SECRETS["shopify"], topic="orders/create")
    response = client.post("/hooks/shopify", content=request.body, headers=request.all_headers())
    assert response.status_code == 202
    assert response.json()["verification"] == "valid"
    return response


def statuses(client, event_id=1):
    response = client.get(f"/api/events/{event_id}/forwards")
    assert response.status_code == 200
    return [attempt["status_code"] for attempt in response.json()]


@pytest.mark.parametrize("upstream", [429, 500])
def test_transient_warehouse_failure_retries_without_losing_the_order(delivery, upstream):
    client, warehouse, _ = delivery
    warehouse.fail_next(status=upstream)
    send(client)
    assert statuses(client) == [502, 200]
    assert len(warehouse.requests) == 2
    assert len(warehouse.orders) == 1
    assert next(iter(warehouse.orders.values()))["lines"][0]["qty"] == 2
    assert client.get("/api/dead-letters").json() == []


def test_duplicate_deliveries_create_only_one_warehouse_order(delivery):
    client, warehouse, _ = delivery
    send(client)
    send(client)
    assert statuses(client, 1) == statuses(client, 2) == [200]
    assert len(warehouse.requests) == 2
    assert len(warehouse.orders) == 1
    assert client.get("/api/dead-letters").json() == []


def test_distinct_orders_arriving_out_of_creation_order_are_both_delivered(delivery):
    client, warehouse, _ = delivery
    send(client, id=2002, created_at="2026-10-14T09:15:00-04:00")
    send(client, id=2001, created_at="2026-10-13T09:15:00-04:00")
    assert list(warehouse.orders) == ["2002", "2001"]
    assert statuses(client, 1) == statuses(client, 2) == [200]


def test_late_unpaid_delivery_cannot_remove_a_fulfilled_order(delivery):
    client, warehouse, _ = delivery
    send(client)
    original = dict(warehouse.orders)
    send(client, financial_status="pending")
    assert warehouse.orders == original
    assert len(warehouse.requests) == 1
    assert statuses(client, 2) == [200]


@pytest.mark.parametrize("upstream", [429, 500])
def test_exhausted_retries_dead_letter_and_replay_after_recovery(delivery, upstream):
    client, warehouse, attempts = delivery
    warehouse.fail_next(attempts, status=upstream)
    send(client)
    assert statuses(client) == [502] * attempts
    assert len(warehouse.requests) == attempts
    assert warehouse.orders == {}
    [dead] = client.get("/api/dead-letters").json()
    assert (dead["event_id"], dead["rule"], dead["resendable"]) == (1, "order-created", True)

    warehouse.clear_faults()
    replay = client.post("/api/events/1/forwards/order-created/resend")
    assert replay.status_code == 200
    assert replay.json()["status_code"] == 200
    assert statuses(client) == [502] * attempts + [200]
    assert len(warehouse.orders) == 1
    assert client.get("/api/dead-letters").json() == []

    again = client.post("/api/events/1/forwards/order-created/resend")
    assert again.status_code == 200 and again.json()["status_code"] == 200
    assert len(warehouse.orders) == 1
    assert len(warehouse.requests) == attempts + 2
    assert statuses(client) == [502] * attempts + [200, 200]
