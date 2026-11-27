"""Persistent outcome records shared across process restarts."""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager

from .contract import TargetJob


class Ledger:
    def __init__(self, path: str) -> None:
        self.path = path
        with self.transaction() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS outcomes "
                "(key TEXT PRIMARY KEY, digest TEXT NOT NULL, state TEXT NOT NULL, payload TEXT NOT NULL)"
            )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self.path, timeout=5)) as conn, conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            yield conn

    @staticmethod
    def record(conn: sqlite3.Connection, key: str, digest: str, state: str, payload: TargetJob) -> None:
        conn.execute(
            "INSERT INTO outcomes VALUES (?, ?, ?, ?)",
            (key, digest, state, json.dumps(payload)),
        )

    def jobs(self) -> list[TargetJob]:
        with self.transaction() as conn:
            return [json.loads(row["payload"]) for row in conn.execute("SELECT payload FROM outcomes")]
