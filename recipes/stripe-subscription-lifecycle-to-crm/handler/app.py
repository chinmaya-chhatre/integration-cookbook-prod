"""Apply ordered Stripe subscription snapshots to a versioned CRM intake."""

from __future__ import annotations

import json
import logging
import math
import os
import re
import sqlite3
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

log = logging.getLogger(__name__)
TOPICS = frozenset(f"customer.subscription.{action}" for action in ("created", "updated", "deleted"))
STAGES = {
    "incomplete": "pending",
    "trialing": "trial",
    "active": "customer",
    "past_due": "at_risk",
    "unpaid": "at_risk",
    "paused": "at_risk",
    "canceled": "inactive",
    "incomplete_expired": "inactive",
}
TERMINAL = frozenset({"canceled", "incomplete_expired"})


@dataclass(frozen=True)
class Settings:
    base_url: str
    api_key: str
    db_path: str
    timeout: float

    @classmethod
    def from_env(cls) -> Settings:
        base = os.environ.get("CRM_BASE_URL", "").rstrip("/")
        url = urlsplit(base)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError("CRM_BASE_URL must be an HTTP(S) base URL without credentials or query")
        timeout = float(os.environ.get("CRM_TIMEOUT_SECONDS", "5"))
        path = os.environ.get("STATE_DB_PATH", "/data/subscriptions.db")
        if not math.isfinite(timeout) or timeout <= 0 or not path or path == ":memory:":
            raise ValueError("configure a positive timeout and a persistent STATE_DB_PATH")
        return cls(base, os.environ.get("CRM_API_KEY", ""), path, timeout)


def identifier(value: Any, prefix: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(prefix + r"_[A-Za-z0-9]+", value):
        raise ValueError(f"expected a {prefix}_ identifier")
    return value


def snapshot(event: Any) -> dict[str, Any] | None:
    if not isinstance(event, dict) or not isinstance(event.get("type"), str):
        raise ValueError("event must have a type")
    if event["type"] not in TOPICS:
        return None
    event_id = identifier(event.get("id"), "evt")
    version = event.get("created")
    if type(version) is not int or version <= 0:
        raise ValueError("event.created must be a positive integer")
    data = event.get("data")
    obj = data.get("object") if isinstance(data, dict) else None
    if not isinstance(obj, dict) or obj.get("object") != "subscription":
        raise ValueError("data.object must be a full subscription snapshot")
    subscription = identifier(obj.get("id"), "sub")
    customer = obj.get("customer")
    customer_id = identifier(customer.get("id") if isinstance(customer, dict) else customer, "cus")
    status = obj.get("status")
    if not isinstance(status, str) or status not in STAGES:
        raise ValueError("unsupported subscription status")
    if event["type"].endswith(".deleted") and status != "canceled":
        raise ValueError("deleted subscription must be canceled")
    return {
        "id": subscription,
        "customer_id": customer_id,
        "status": status,
        "stage": STAGES[status],
        "event_id": event_id,
        "version": version,
    }


def decision(current: dict[str, Any] | None, incoming: dict[str, Any]) -> str | None:
    if not current:
        return None
    if incoming["version"] < current["version"]:
        return "stale"
    if incoming["version"] == current["version"]:
        if incoming["event_id"] == current["event_id"]:
            return "duplicate"
        raise ValueError("distinct events share a timestamp; reconcile against the source")
    if current["status"] in TERMINAL and incoming["status"] not in TERMINAL:
        return "terminal"
    return None


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Handler:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS subscriptions (id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
            )

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.settings.db_path, timeout=30)

    def request(self, method: str, key: str, payload: dict | None = None) -> tuple[int, Any]:
        headers = {"Content-Type": "application/json"}
        if self.settings.api_key:
            headers["Authorization"] = f"Bearer {self.settings.api_key}"
        request = Request(
            f"{self.settings.base_url}/subscriptions/{key}",
            data=None if payload is None else json.dumps(payload).encode(),
            headers=headers,
            method=method,
        )
        try:
            response = build_opener(NoRedirect()).open(request, timeout=self.settings.timeout)
        except HTTPError as exc:
            response = exc
        with response:
            body = response.read(1_000_001)
            if len(body) > 1_000_000:
                raise OSError("CRM response too large")
            try:
                result = json.loads(body)
            except ValueError:
                result = None
            return response.code, result

    def apply(self, incoming: dict[str, Any]) -> tuple[int, dict[str, str]]:
        # Hold a shared-database write transaction across the CRM call to serialize all writers.
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload FROM subscriptions WHERE id = ?", (incoming["id"],)).fetchone()
            current = json.loads(row[0]) if row else None
            if reason := decision(current, incoming):
                return 200, {"result": reason}
            code, remote = self.request("GET", incoming["id"])
            if code == 200:
                if (
                    not isinstance(remote, dict)
                    or remote.get("id") != incoming["id"]
                    or type(remote.get("version")) is not int
                    or remote["version"] <= 0
                    or not isinstance(remote.get("event_id"), str)
                    or not isinstance(remote.get("status"), str)
                    or remote["status"] not in STAGES
                ):
                    raise OSError("CRM returned an invalid versioned record")
                if reason := decision(remote, incoming):
                    db.execute(
                        "INSERT OR REPLACE INTO subscriptions VALUES (?, ?)",
                        (incoming["id"], json.dumps(remote)),
                    )
                    return 200, {"result": reason}
            elif code != 404:
                raise OSError(f"CRM read returned {code}")
            code, result = self.request("PUT", incoming["id"], incoming)
            if code == 409:
                version = result.get("current_version") if isinstance(result, dict) else None
                if type(version) is int and version > incoming["version"]:
                    return 200, {"result": "stale"}
            if code not in (200, 201):
                if code in (400, 422):
                    return 422, {"result": "crm_rejected"}
                raise OSError(f"CRM write returned {code}")
            db.execute(
                "INSERT OR REPLACE INTO subscriptions VALUES (?, ?)",
                (incoming["id"], json.dumps(incoming)),
            )
            return 200, {"result": "applied"}

    def handle(self, body: bytes) -> tuple[int, dict[str, str]]:
        try:
            event = json.loads(body)
        except ValueError:
            return 400, {"result": "unreadable"}
        try:
            incoming = snapshot(event)
            if incoming is None:
                return 200, {"result": "skipped"}
            result = self.apply(incoming)
            log.info(
                "subscription %s event %s: %s", incoming["id"], incoming["event_id"], result[1]["result"]
            )
            return result
        except ValueError as exc:
            return 422, {"result": "invalid_event", "reason": str(exc)}
        except (OSError, sqlite3.Error) as exc:
            log.warning("subscription delivery failed: %s", exc)
            return 502, {"result": "delivery_failed"}


def make_server(handler: Handler, port: int) -> ThreadingHTTPServer:
    class SubscriptionRequest(BaseHTTPRequestHandler):
        def reply(self, code: int, payload: dict[str, str]) -> None:
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path == "/healthz":
                self.reply(200, {"status": "ok"})
            else:
                self.reply(404, {"result": "not_found"})

        def do_POST(self) -> None:
            if self.path != "/stripe/subscription":
                self.reply(404, {"result": "not_found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if not 0 < length <= 1_000_000:
                self.reply(400, {"result": "invalid_length"})
                return
            self.reply(*handler.handle(self.rfile.read(length)))

        def log_message(self, format: str, *args: Any) -> None:
            log.debug(format, *args)

    return ThreadingHTTPServer(("0.0.0.0", port), SubscriptionRequest)


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    try:
        handler = Handler(Settings.from_env())
    except (ValueError, OSError, sqlite3.Error) as exc:
        raise SystemExit(f"invalid configuration: {exc}") from exc
    with make_server(handler, 8080) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
