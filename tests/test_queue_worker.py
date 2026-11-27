"""Core queue processing keeps identity, completion proof and receipts separate."""

import hashlib
import importlib
import json
import sqlite3
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "recipes/webhook-to-queue-on-aws"))
contract = importlib.import_module("queue_recipe.contract")
worker = importlib.import_module("queue_recipe.worker")
state = importlib.import_module("queue_recipe.state")
RAW = b'{"event_id":"job-42","kind":"support.job","data":{"nested":[null,true,3,{"x":"ok"}]}}'


def test_target_mapping_preserves_bytes_identity_and_nested_data():
    payload = contract.target_job(contract.envelope(RAW, "cookbook"), "cookbook")
    assert payload == {
        "external_reference": "cookbook:job-42",
        "body_sha256": hashlib.sha256(RAW).hexdigest(),
        "data": {"nested": [None, True, 3, {"x": "ok"}]},
    }
    # Equivalent JSON with different signed bytes is a conflicting reuse, not a new job.
    spaced = json.dumps(json.loads(RAW), indent=2).encode()
    other = contract.target_job(contract.envelope(spaced, "cookbook"), "cookbook")
    assert other["external_reference"] == payload["external_reference"]
    assert other["body_sha256"] != payload["body_sha256"]


@pytest.mark.parametrize(
    "status,proof,expected",
    [
        (200, {"completed": True, "key": "key", "digest": "digest"}, "completed"),
        (201, {"completed": True, "key": "key", "digest": "digest"}, "completed"),
        (200, {"completed": 1, "key": "key", "digest": "digest"}, "retry"),
        (200, {"completed": True, "key": "other", "digest": "digest"}, "retry"),
        (200, {"completed": True, "key": "key", "digest": "other"}, "retry"),
        (200, [], "retry"),
        (200, None, "retry"),
        (202, {"completed": True, "key": "key", "digest": "digest"}, "blocked"),
        (307, None, "blocked"),
        (401, None, "blocked"),
        (408, None, "retry"),
        (425, None, "retry"),
        (429, None, "retry"),
        (500, None, "retry"),
    ],
)
def test_target_completion_policy(status, proof, expected):
    payload = {"external_reference": "key", "body_sha256": "digest", "data": {}}
    assert worker.target_outcome(httpx.Response(status, json=proof), payload) == expected


def test_invalid_target_json_is_retryable():
    payload = {"external_reference": "key", "body_sha256": "digest", "data": {}}
    assert worker.target_outcome(httpx.Response(200, content=b"not json"), payload) == "retry"


@pytest.mark.parametrize(
    "count,jitter,expected",
    [
        ("1", 0, 2),
        ("2", 2, 6),
        ("5", 2, 32),
        ("99999", 2, 32),
        ("-100", 0, 2),
        ("0", 0, 2),
        ("bad", 1, 3),
        ("1", -1, 2),
        ("1", 100, 4),
    ],
)
def test_retry_delay_is_bounded(count, jitter, expected):
    assert worker.retry_delay(count, jitter) == expected


class Receipts:
    def __init__(self, ledger):
        self.ledger = ledger
        self.deleted = []
        self.released = []
        self.fail_delete = False

    def delete_message(self, **kwargs):
        # A fresh connection observes completion before the queue is acknowledged.
        assert len(state.Ledger(self.ledger.path).jobs()) == 1
        if self.fail_delete:
            raise OSError("queue unavailable")
        self.deleted.append(kwargs["ReceiptHandle"])

    def change_message_visibility(self, **kwargs):
        self.released.append(kwargs["ReceiptHandle"])


def message(receipt="current", raw=RAW):
    return {
        "Body": contract.envelope(raw, "cookbook"),
        "ReceiptHandle": receipt,
        "Attributes": {"ApproximateReceiveCount": "bad"},
    }


def confirm(key, payload):
    return httpx.Response(200, json={"completed": True, "key": key, "digest": payload["body_sha256"]})


def test_delete_failure_redelivery_uses_new_receipt_without_target_call(tmp_path):
    ledger = state.Ledger(str(tmp_path / "worker.db"))
    queue = Receipts(ledger)
    queue.fail_delete = True
    with pytest.raises(OSError, match="queue unavailable"):
        worker.handle_message(queue, "queue", message("old"), "cookbook", ledger, confirm)
    assert len(ledger.jobs()) == 1
    queue.fail_delete = False

    def no_send(*_):
        pytest.fail("a committed delivery must not call the target again")

    assert worker.handle_message(queue, "queue", message("new"), "cookbook", ledger, no_send) == "completed"
    assert queue.deleted == ["new"]


@pytest.mark.parametrize("outcome", ["invalid", "retry", "blocked", "conflict"])
def test_unconfirmed_work_retains_receipt(tmp_path, outcome):
    ledger = state.Ledger(str(tmp_path / "worker.db"))
    queue = Receipts(ledger)
    delivery = message()
    if outcome == "invalid":
        delivery["Body"] = "{}"
    if outcome == "conflict":
        worker.process(delivery["Body"], "cookbook", ledger, confirm)
        delivery = message(raw=RAW.replace(b'"ok"', b'"changed"'))
    status = 400 if outcome == "blocked" else 503
    assert (
        worker.handle_message(queue, "queue", delivery, "cookbook", ledger, lambda *_: httpx.Response(status))
        == outcome
    )
    assert queue.deleted == []
    assert queue.released == ["current"]


def test_ledger_rolls_back_and_closes_failed_transaction(tmp_path):
    ledger = state.Ledger(str(tmp_path / "worker.db"))
    payload = contract.target_job(contract.envelope(RAW, "cookbook"), "cookbook")
    with pytest.raises(RuntimeError):
        with ledger.transaction() as conn:
            state.Ledger.record(conn, "key", "digest", "completed", payload)
            raise RuntimeError("abort")
    assert ledger.jobs() == []
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        conn.execute("SELECT 1")


def test_ledger_commits_and_closes_successful_transaction(tmp_path):
    ledger = state.Ledger(str(tmp_path / "worker.db"))
    payload = contract.target_job(contract.envelope(RAW, "cookbook"), "cookbook")
    with ledger.transaction() as conn:
        state.Ledger.record(conn, "key", "digest", "completed", payload)
    assert state.Ledger(ledger.path).jobs() == [payload]
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        conn.execute("SELECT 1")
