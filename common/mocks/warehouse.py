"""A warehouse-style order intake: strict about the schema, and unforgiving of duplicates.

``POST /v1/orders`` needs an ``order_id`` and at least one line with ``sku`` and a positive
integer ``qty``. A second order with the same id answers 409, as many warehouse systems do, so
recipes must decide what a duplicate delivery means rather than assume it is harmless.
"""

from __future__ import annotations

import re
from typing import Any

from .base import MockServer, Reply


class WarehouseMock(MockServer):
    def __init__(self) -> None:
        super().__init__()
        self.orders: dict[str, dict[str, Any]] = {}

    def handle(self, method: str, path: str, headers: dict[str, str], body: bytes) -> Reply:
        if method == "POST" and path == "/v1/orders":
            return self._create(self.parse(body))
        order = re.fullmatch(r"/v1/orders/([^/]+)", path)
        if method == "GET" and order:
            found = self.orders.get(order.group(1))
            return Reply(200, found) if found else Reply(404, {"error": "order not found"})
        return Reply(404, {"error": f"no route for {method} {path}"})

    def _create(self, payload: Any) -> Reply:
        problems = validate_order(payload)
        if problems:
            return Reply(422, {"error": "invalid order", "problems": problems})
        order_id = str(payload["order_id"])
        if order_id in self.orders:
            return Reply(409, {"error": "duplicate order", "order_id": order_id})
        self.orders[order_id] = {**payload, "status": "received"}
        return Reply(201, {"order_id": order_id, "status": "received"})


def validate_order(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return ["body must be a JSON object"]
    problems = []
    if not payload.get("order_id"):
        problems.append("order_id is required")
    lines = payload.get("lines")
    if not isinstance(lines, list) or not lines:
        problems.append("lines must be a non-empty list")
        return problems
    for index, line in enumerate(lines):
        if not isinstance(line, dict) or not line.get("sku"):
            problems.append(f"lines[{index}].sku is required")
        qty = line.get("qty") if isinstance(line, dict) else None
        if not isinstance(qty, int) or isinstance(qty, bool) or qty < 1:
            problems.append(f"lines[{index}].qty must be a positive integer")
    return problems
