"""Persistent outcome records shared across process restarts."""

import json
import sqlite3
from contextlib import contextmanager


class Ledger:
    def __init__(self, path: str):
        self.path = path
        with self.transaction() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS outcomes "
                "(key TEXT PRIMARY KEY, digest TEXT NOT NULL, state TEXT NOT NULL, payload TEXT NOT NULL)"
            )

    @contextmanager
    def transaction(self):
        with sqlite3.connect(self.path, timeout=5) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            yield conn

    @staticmethod
    def record(conn, key: str, digest: str, state: str, payload: dict):
        conn.execute(
            "INSERT INTO outcomes VALUES (?, ?, ?, ?)",
            (key, digest, state, json.dumps(payload)),
        )

    def jobs(self) -> list[dict]:
        with self.transaction() as conn:
            return [json.loads(row["payload"]) for row in conn.execute("SELECT payload FROM outcomes")]
