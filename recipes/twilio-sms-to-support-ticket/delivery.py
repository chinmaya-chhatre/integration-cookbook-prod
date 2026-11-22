"""Durable ticket outbox with a target-supplied idempotency contract.

The injected sender receives (payload, stable_key) and returns an HTTP status.
It must honor the stable key across response loss and process crashes.
"""

import json
import math
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
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            # Claim the write lock before reading so concurrent duplicates see the winner.
            db.execute("BEGIN IMMEDIATE")
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
            return self._deliver(db, identity, send)

    def _deliver(self, db, identity: str, send: Callable[[dict, str], int]) -> str:
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

    def work(self, send: Callable[[dict, str], int], now: float, limit: int = 100) -> list[str]:
        """Run a bounded batch of due tickets, persisting five-attempt retry budgets.

        Uses wall-clock Unix seconds so due times survive process restarts.
        Transactions serialize workers; target idempotency remains required.
        """
        if not math.isfinite(now) or type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("finite time and batch limit from 1 to 1000 required")
        results = []
        for _ in range(limit):
            with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
                db.execute("BEGIN IMMEDIATE")
                db.execute(
                    "CREATE TABLE IF NOT EXISTS ticket_retries "
                    "(identity TEXT PRIMARY KEY, attempts INTEGER NOT NULL, due REAL NOT NULL)"
                )
                row = db.execute(
                    "SELECT t.identity, COALESCE(r.attempts, 0) FROM tickets t "
                    "LEFT JOIN ticket_retries r ON t.identity = r.identity "
                    "WHERE t.state = 'pending' AND (r.due IS NULL OR r.due <= ?) "
                    "ORDER BY COALESCE(r.due, 0), t.identity LIMIT 1",
                    (now,),
                ).fetchone()
                if row is None:
                    break
                identity, previous = row
                attempt = previous + 1
                state = self._deliver(db, identity, send)
                if state == "pending" and attempt >= 5:
                    state = "review"
                    db.execute("UPDATE tickets SET state = 'review' WHERE identity = ?", (identity,))
                due = now + min(2**attempt, 60)
                db.execute(
                    "INSERT INTO ticket_retries VALUES (?, ?, ?) "
                    "ON CONFLICT(identity) DO UPDATE SET attempts = excluded.attempts, "
                    "due = excluded.due",
                    (identity, attempt, due),
                )
                results.append(state)
        return results
