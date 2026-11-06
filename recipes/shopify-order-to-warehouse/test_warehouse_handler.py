import json
import unittest
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from handler.app import Handler, Settings, make_server

from common.fixtures import shopify_order_created
from common.mocks.warehouse import WarehouseMock


class WarehouseHandlerTest(unittest.TestCase):
    def test_http_delivery_and_duplicate(self) -> None:
        with WarehouseMock() as warehouse:
            handler = Handler(Settings(warehouse.url, "demo-key", frozenset({"paid"}), 2))
            with make_server(handler, 0) as server:
                thread = Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    base = f"http://127.0.0.1:{server.server_port}"
                    with urlopen(f"{base}/healthz", timeout=2) as response:
                        self.assertEqual(response.status, 200)
                    body = shopify_order_created().body
                    for expected in ("sent", "already_received"):
                        request = Request(f"{base}/shopify/order-created", data=body)
                        with urlopen(request, timeout=2) as response:
                            self.assertEqual(response.status, 200)
                            self.assertEqual(json.load(response), {"result": expected})
                    request = Request(f"{base}/shopify/order-created", data=b"{")
                    with self.assertRaises(HTTPError) as invalid:
                        urlopen(request, timeout=2)
                    with invalid.exception:
                        self.assertEqual(invalid.exception.code, 400)
                finally:
                    server.shutdown()
                    thread.join()

            self.assertEqual(
                warehouse.orders,
                {
                    "820982911946154508": {
                        "order_id": "820982911946154508",
                        "reference": "#1001",
                        "lines": [{"sku": "TEE-BLK-M", "qty": 2, "title": "Black tee"}],
                        "ship_to": {
                            "name": "Jane Doe",
                            "address1": "1 Main St",
                            "city": "Boston",
                            "region": "MA",
                            "postal_code": "02110",
                            "country": "US",
                        },
                        "status": "received",
                    }
                },
            )
            self.assertEqual(warehouse.requests[0].headers["authorization"], "Bearer demo-key")

    def test_skips_and_invalid_lines_do_not_call_warehouse(self) -> None:
        with WarehouseMock() as warehouse:
            handler = Handler(Settings(warehouse.url, "", frozenset({"paid"}), 2))
            event = json.loads(shopify_order_created().body)
            event["financial_status"] = "pending"
            self.assertEqual(handler.handle(json.dumps(event).encode())[1]["result"], "skipped")
            event["financial_status"] = "paid"
            event["line_items"][0]["sku"] = None
            status, response = handler.handle(json.dumps(event).encode())
            self.assertEqual(status, 422)
            self.assertIn("sku", response["reason"])
            self.assertEqual(warehouse.requests, [])

    def test_warehouse_response_mapping(self) -> None:
        body = shopify_order_created().body
        with WarehouseMock() as warehouse:
            handler = Handler(Settings(warehouse.url, "", frozenset({"paid"}), 2))
            for upstream, expected in ((422, 422), (429, 502), (500, 502)):
                with self.subTest(upstream=upstream):
                    warehouse.fail_next(status=upstream)
                    self.assertEqual(handler.handle(body)[0], expected)
            self.assertEqual(handler.handle(body), (200, {"result": "sent"}))
