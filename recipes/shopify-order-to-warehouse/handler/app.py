"""Map forwarded Shopify orders to the warehouse intake API."""

from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

log = logging.getLogger(__name__)
ADDRESS_FIELDS = {
    "name": "name",
    "address1": "address1",
    "city": "city",
    "region": "province_code",
    "postal_code": "zip",
    "country": "country_code",
}


@dataclass(frozen=True)
class Settings:
    base_url: str
    api_key: str
    statuses: frozenset[str]
    timeout: float

    @classmethod
    def from_env(cls) -> Settings:
        base_url = os.environ.get("WAREHOUSE_BASE_URL", "").rstrip("/")
        url = urlsplit(base_url)
        if url.scheme not in {"http", "https"} or not url.hostname or url.query or url.fragment:
            raise ValueError("WAREHOUSE_BASE_URL must be an HTTP(S) base URL")
        statuses = frozenset(
            value.strip()
            for value in os.environ.get("FULFIL_FINANCIAL_STATUSES", "paid").split(",")
            if value.strip()
        )
        timeout = float(os.environ.get("WAREHOUSE_TIMEOUT_SECONDS", "10"))
        if not statuses or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("configure at least one financial status and a positive warehouse timeout")
        return cls(base_url, os.environ.get("WAREHOUSE_API_KEY", ""), statuses, timeout)


def warehouse_order(event: dict[str, Any]) -> dict[str, Any]:
    order_id = event.get("id")
    if type(order_id) is not int or order_id <= 0:
        raise ValueError("id must be a positive integer")
    items = event.get("line_items")
    if not isinstance(items, list) or not items:
        raise ValueError("line_items must be a non-empty list")
    lines = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"line_items[{index}] must be an object")
        sku = item.get("sku")
        qty = item.get("quantity")
        if not isinstance(sku, str) or not sku.strip():
            raise ValueError(f"line_items[{index}].sku is required")
        if type(qty) is not int or qty < 1:
            raise ValueError(f"line_items[{index}].quantity must be a positive integer")
        lines.append({"sku": sku, "qty": qty, "title": item.get("title")})

    address = event.get("shipping_address")
    if not isinstance(address, dict):
        raise ValueError("shipping_address must be an object")
    return {
        "order_id": str(order_id),
        "reference": event.get("name"),
        "lines": lines,
        "ship_to": {target: address.get(source) for target, source in ADDRESS_FIELDS.items()},
    }


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A redirect must not send warehouse credentials to a different endpoint.
        return None


def post_order(settings: Settings, order: dict[str, Any]) -> int:
    headers = {"Content-Type": "application/json"}
    if settings.api_key:
        headers["Authorization"] = f"Bearer {settings.api_key}"
    request = Request(f"{settings.base_url}/v1/orders", data=json.dumps(order).encode(), headers=headers)
    try:
        with build_opener(NoRedirect()).open(request, timeout=settings.timeout) as response:
            return response.status
    except HTTPError as exc:
        with exc:
            return exc.code


class Handler:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def handle(self, body: bytes) -> tuple[int, dict[str, str]]:
        try:
            event = json.loads(body)
        except ValueError:
            return 400, {"result": "unreadable"}
        if not isinstance(event, dict) or not isinstance(status := event.get("financial_status"), str):
            return 400, {"result": "unreadable", "reason": "financial_status must be a string"}
        if status not in self.settings.statuses:
            log.info("order %s skipped: financial status is not configured for fulfilment", event.get("id"))
            return 200, {"result": "skipped", "reason": "financial status is not configured for fulfilment"}
        try:
            order = warehouse_order(event)
        except ValueError as exc:
            return 422, {"result": "invalid_order", "reason": str(exc)}
        try:
            status = post_order(self.settings, order)
        except OSError:
            log.warning("order %s: warehouse request failed", order["order_id"])
            return 502, {"result": "warehouse_failed"}
        if status == 201:
            return 200, {"result": "sent"}
        if status == 409:
            return 200, {"result": "already_received"}
        if status == 422:
            return 422, {"result": "invalid_order", "reason": "warehouse rejected the order"}
        log.warning("order %s: warehouse returned %s", order["order_id"], status)
        return 502, {"result": "warehouse_failed"}


def make_server(handler: Handler, port: int) -> ThreadingHTTPServer:
    class OrderRequest(BaseHTTPRequestHandler):
        def reply(self, status: int, payload: dict[str, str]) -> None:
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            if self.path == "/healthz":
                self.reply(200, {"status": "ok"})
            else:
                self.reply(404, {"result": "not_found"})

        def do_POST(self) -> None:
            if self.path != "/shopify/order-created":
                self.reply(404, {"result": "not_found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if length <= 0:
                self.reply(400, {"result": "unreadable"})
                return
            self.reply(*handler.handle(self.rfile.read(length)))

        def log_message(self, format: str, *args: Any) -> None:
            log.debug(format, *args)

    return ThreadingHTTPServer(("0.0.0.0", port), OrderRequest)


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s"
    )
    try:
        settings = Settings.from_env()
    except ValueError as exc:
        raise SystemExit(f"invalid configuration: {exc}") from exc
    with make_server(Handler(settings), 8080) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
