"""Read-only diagnostics report backlog health without leaking SMS contents."""

import importlib.util
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
RECIPE = ROOT / "recipes/twilio-sms-to-support-ticket"
SPEC = importlib.util.spec_from_file_location("sms_diagnostics", RECIPE / "diagnostics.py")
DIAGNOSTICS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DIAGNOSTICS)


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "personal # data?.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE tickets (identity TEXT PRIMARY KEY, payload TEXT, state TEXT)")
        db.executemany(
            "INSERT INTO tickets VALUES (?, ?, ?)",
            [
                ("secret-message-1", '{"body":"private SMS + phone + token"}', "pending"),
                ("secret-message-2", "private", "pending"),
                ("secret-message-3", "private", "pending"),
                ("secret-message-4", "private", "delivered"),
                ("secret-message-5", "private", "review"),
            ],
        )
    return path


def test_snapshot_before_worker_initializes_retry_table(database):
    assert DIAGNOSTICS.snapshot(database, 100) == {
        "sampled_at_epoch": 100,
        "states": {"pending": 3, "delivered": 1, "review": 1},
        "pending_due": 3,
        "pending_scheduled": 0,
        "max_pending_attempts": 0,
    }


def test_due_boundaries_and_counts_are_consistent_without_payloads(database):
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE ticket_retries (identity TEXT PRIMARY KEY, attempts INTEGER, due REAL)")
        db.executemany(
            "INSERT INTO ticket_retries VALUES (?, ?, ?)",
            [
                ("secret-message-1", 2, 100),
                ("secret-message-2", 4, 101),
                ("secret-message-4", 5, 1),
                ("secret-message-5", 5, 1),
            ],
        )
    before = database.read_bytes()
    report = DIAGNOSTICS.snapshot(database, 100)
    assert report["pending_due"] == 2
    assert report["pending_scheduled"] == 1
    assert report["max_pending_attempts"] == 4
    assert report["states"] == {"pending": 3, "delivered": 1, "review": 1}
    assert database.read_bytes() == before
    serialized = json.dumps(report)
    assert all(word not in serialized for word in ("private", "secret-message", "phone", "token"))
    assert DIAGNOSTICS.snapshot(database, 101)["pending_due"] == 3


def test_empty_outbox_reports_zeroes(database):
    with sqlite3.connect(database) as db:
        db.execute("DELETE FROM tickets")
    report = DIAGNOSTICS.snapshot(database, 0)
    assert report["states"] == {"pending": 0, "delivered": 0, "review": 0}
    assert report["pending_due"] == report["pending_scheduled"] == 0


def test_connection_is_read_only_even_if_sql_attempts_a_write(database, monkeypatch):
    connect = sqlite3.connect

    def probe(*args, **kwargs):
        db = connect(*args, **kwargs)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            db.execute("DELETE FROM tickets")
        db.rollback()
        return db

    monkeypatch.setattr(DIAGNOSTICS.sqlite3, "connect", probe)
    assert DIAGNOSTICS.snapshot(database, 100)["states"]["pending"] == 3


def test_cli_emits_only_aggregate_json(database):
    result = subprocess.run(
        [sys.executable, str(RECIPE / "diagnostics.py"), "--database", str(database)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stderr == ""
    report = json.loads(result.stdout)
    assert report["states"]["pending"] == 3
    assert report["sampled_at_epoch"] > 0
    assert "private" not in result.stdout
    assert str(database) not in result.stdout


@pytest.mark.parametrize("kind", ["missing", "empty", "corrupt", "unexpected_state"])
def test_cli_fails_closed_without_creating_storage_or_printing_details(tmp_path, kind):
    path = tmp_path / "SECRET-token-phone.sqlite3"
    if kind == "empty":
        with sqlite3.connect(path):
            pass
    elif kind == "corrupt":
        path.write_text("private SMS data")
    elif kind == "unexpected_state":
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE tickets (state TEXT)")
            db.execute("INSERT INTO tickets VALUES ('private SMS data')")
    before = path.read_bytes() if path.exists() else None
    result = subprocess.run(
        [sys.executable, str(RECIPE / "diagnostics.py"), "--database", str(path)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert result.stdout.strip() == (
        "Cannot read outbox health. Check the database, schema and read permissions."
    )
    assert result.stderr == ""
    assert (path.read_bytes() if path.exists() else None) == before


@pytest.mark.parametrize("now", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_sample_time_is_rejected(database, now):
    with pytest.raises(ValueError, match="finite"):
        DIAGNOSTICS.snapshot(database, now)
