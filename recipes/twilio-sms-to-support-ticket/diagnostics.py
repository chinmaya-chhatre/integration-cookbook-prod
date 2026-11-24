"""Read aggregate SMS delivery health without exposing ticket contents."""

import argparse
import json
import math
import sqlite3
from contextlib import closing
from pathlib import Path
from time import time


def snapshot(path: Path, now: float) -> dict:
    """Read one consistent snapshot. Never initialize or change outbox storage."""
    if not math.isfinite(now):
        raise ValueError("finite sample time required")
    uri = path.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=1)) as db:
        db.execute("BEGIN")
        states = {"pending": 0, "delivered": 0, "review": 0}
        for state, count in db.execute("SELECT state, COUNT(*) FROM tickets GROUP BY state"):
            if state not in states:
                raise ValueError("unrecognized delivery state")
            states[state] = count
        retries = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'ticket_retries'"
        ).fetchone()
        if retries:
            due = db.execute(
                "SELECT COUNT(*) FROM tickets t LEFT JOIN ticket_retries r ON t.identity = r.identity "
                "WHERE t.state = 'pending' AND (r.due IS NULL OR r.due <= ?)",
                (now,),
            ).fetchone()[0]
            maximum = db.execute(
                "SELECT COALESCE(MAX(r.attempts), 0) FROM ticket_retries r "
                "JOIN tickets t ON t.identity = r.identity WHERE t.state = 'pending'"
            ).fetchone()[0]
        else:
            due, maximum = states["pending"], 0
    return {
        "sampled_at_epoch": now,
        "states": states,
        "pending_due": due,
        "pending_scheduled": states["pending"] - due,
        "max_pending_attempts": maximum,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        report = snapshot(args.database, time())
    except (OSError, sqlite3.Error, ValueError):
        # Database errors can contain paths or schema text. Emit neither.
        print("Cannot read outbox health. Check the database, schema and read permissions.")
        return 2
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
