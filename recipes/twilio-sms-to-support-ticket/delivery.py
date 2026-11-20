"""Durable ticket outbox with a target-supplied idempotency contract.

The injected sender receives (payload, stable_key) and returns an HTTP status.
It must honor the stable key across response loss and process crashes.
"""

import json
import sqlite3
from collections.abc import Callable
from contextlib import closing


class Outbox:
    def __init__(self, path: str):
        self.path = path
        with closing(sqlite3.connect(path)) as db, db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS tickets "
                "(identity TEXT PRIMARY KEY, payload TEXT NOT NULL, state TEXT NOT NULL)"
            )

    def enqueue(self, identity: str, payload: dict) -> bool:
        if not identity or payload.get("external_reference") != identity:
            raise ValueError("payload must carry its stable external reference")
        encoded = json.dumps(payload, sort_keys=True)
        with closing(sqlite3.connect(self.path)) as db, db:
            existing = db.execute("SELECT payload FROM tickets WHERE identity = ?", (identity,)).fetchone()
            if existing:
                if existing[0] != encoded:
                    raise ValueError("identity reused with different content")
                return False
            db.execute("INSERT INTO tickets VALUES (?, ?, 'pending')", (identity, encoded))
        return True

    def deliver(self, identity: str, send: Callable[[dict, str], int]) -> str:
        # A single writer serializes claims. The target key closes the crash-after-send window.
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT payload, state FROM tickets WHERE identity = ?", (identity,)).fetchone()
            if row is None:
                raise KeyError(identity)
            if row[1] != "pending":
                return row[1]
            try:
                status = send(json.loads(row[0]), identity)
            except OSError:
                return "pending"
            state = (
                "delivered"
                if 200 <= status < 300
                else "pending"
                if status in {408, 425, 429} or 500 <= status < 600
                else "review"
            )
            db.execute("UPDATE tickets SET state = ? WHERE identity = ?", (state, identity))
            return state
