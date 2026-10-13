"""A CRM-style API: contacts and subscriptions, upserted by an id the caller chooses.

Upserts return 201 when the record is new and 200 when it already existed, so a recipe can tell
a replayed event from a first delivery. Subscription updates carry a ``version``; sending an
older one than stored answers 409, which is how recipe R4 detects out-of-order events.
"""

from __future__ import annotations

import re
from typing import Any

from .base import MockServer, Reply


class CrmMock(MockServer):
    def __init__(self) -> None:
        super().__init__()
        self.contacts: dict[str, dict[str, Any]] = {}
        self.subscriptions: dict[str, dict[str, Any]] = {}

    def handle(self, method: str, path: str, headers: dict[str, str], body: bytes) -> Reply:
        contact = re.fullmatch(r"/contacts/([^/]+)", path)
        if contact and method == "PUT":
            return self._upsert(self.contacts, contact.group(1), self.parse(body))
        if contact and method == "GET":
            return self._get(self.contacts, contact.group(1))
        subscription = re.fullmatch(r"/subscriptions/([^/]+)", path)
        if subscription and method == "PUT":
            return self._put_subscription(subscription.group(1), self.parse(body))
        if subscription and method == "GET":
            return self._get(self.subscriptions, subscription.group(1))
        return Reply(404, {"error": f"no route for {method} {path}"})

    @staticmethod
    def _get(store: dict[str, dict[str, Any]], key: str) -> Reply:
        return Reply(200, store[key]) if key in store else Reply(404, {"error": "not found"})

    @staticmethod
    def _upsert(store: dict[str, dict[str, Any]], key: str, payload: Any) -> Reply:
        if not isinstance(payload, dict):
            return Reply(400, {"error": "body must be a JSON object"})
        created = key not in store
        store[key] = {**store.get(key, {}), **payload, "id": key}
        return Reply(201 if created else 200, store[key])

    def _put_subscription(self, key: str, payload: Any) -> Reply:
        if not isinstance(payload, dict) or not isinstance(payload.get("version"), int):
            return Reply(400, {"error": "'version' (integer) is required"})
        current = self.subscriptions.get(key)
        if current and payload["version"] < current["version"]:
            return Reply(409, {"error": "stale update", "current_version": current["version"]})
        return self._upsert(self.subscriptions, key, payload)
