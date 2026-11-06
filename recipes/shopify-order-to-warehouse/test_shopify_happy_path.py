import json
from pathlib import Path
from threading import Thread

import pytest
from handler.app import Handler, Settings, make_server

from common.fixtures import SECRETS, shopify_order_created
from common.mocks.warehouse import WarehouseMock


def test_signed_order_reaches_warehouse(tmp_path):
    pytest.importorskip("hookscope", reason="install HookScope to run the end-to-end test")
    from fastapi.testclient import TestClient
    from hookscope.main import create_app
    from hookscope.rules import parse_rules
    from hookscope.store import EventStore

    order_id = 820982911946154508
    request = shopify_order_created(order_id=order_id, quantity=3)
    rules = json.loads(Path(__file__).with_name("rules.json").read_text())

    with WarehouseMock() as warehouse:
        handler = Handler(Settings(warehouse.url, "", frozenset({"paid"}), 2))
        with make_server(handler, 0) as server:
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                rules["rules"][0]["target_url"] = rules["rules"][0]["target_url"].replace(
                    "http://handler:8080", f"http://127.0.0.1:{server.server_port}"
                )
                app = create_app(
                    store=EventStore(str(tmp_path / "events.db")),
                    secrets={"shopify": SECRETS["shopify"]},
                    rules=parse_rules(rules),
                    sleep=lambda seconds: None,
                    admin_password=None,
                )
                client = TestClient(app)
                response = client.post("/hooks/shopify", content=request.body, headers=request.all_headers())
                assert response.status_code == 202
                assert response.json()["verification"] == "valid"
                assert response.json()["forwarded_to"] == ["order-created"]
                forwards = client.get("/api/events/1/forwards").json()
                assert [(attempt["rule"], attempt["status_code"]) for attempt in forwards] == [
                    ("order-created", 200)
                ]
            finally:
                server.shutdown()
                thread.join()

        assert list(warehouse.orders) == [str(order_id)]
        order = warehouse.orders[str(order_id)]
        assert order["order_id"] == str(order_id)
        assert order["lines"] == [{"sku": "TEE-BLK-M", "qty": 3, "title": "Black tee"}]
        assert order["ship_to"]["postal_code"] == "02110"
