import importlib.util
from pathlib import Path

import pytest

PATH = Path(__file__).parents[1] / "recipes/twilio-sms-to-support-ticket/delivery.py"
SPEC = importlib.util.spec_from_file_location("scheduled_sms", PATH)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def test_retry_due_time_survives_restart(tmp_path):
    path = str(tmp_path / "tickets.db")
    box = module.Outbox(path)
    body = {"external_reference": "sms-1", "description": "Help"}
    box.enqueue("sms-1", body)
    calls = []

    def unavailable(payload, key):
        calls.append((payload, key))
        return 429

    assert box.work(unavailable, 100) == ["pending"]
    restored = module.Outbox(path)
    assert restored.work(unavailable, 101) == []
    assert restored.work(lambda payload, key: calls.append((payload, key)) or 201, 102) == ["delivered"]
    assert restored.work(unavailable, 1000) == []
    assert calls == [(body, "sms-1"), (body, "sms-1")]


def test_retry_budget_survives_restarts_and_exhausts(tmp_path):
    path = str(tmp_path / "tickets.db")
    box = module.Outbox(path)
    box.enqueue("sms-1", {"external_reference": "sms-1"})
    for now, expected in [(0, "pending"), (2, "pending"), (6, "pending"), (14, "pending"), (30, "review")]:
        assert module.Outbox(path).work(lambda body, key: 500, now) == [expected]
    assert module.Outbox(path).work(lambda *args: pytest.fail("exhausted"), 1000) == []


def test_transport_loss_and_permanent_failure(tmp_path):
    box = module.Outbox(str(tmp_path / "tickets.db"))
    box.enqueue("a", {"external_reference": "a"})

    def lost(*args):
        raise OSError("response lost")

    assert box.work(lost, 0) == ["pending"]
    box.enqueue("b", {"external_reference": "b"})
    assert box.work(lambda *args: 400, 1) == ["review"]
    assert box.work(lambda *args: 201, 2) == ["delivered"]


def test_batch_limit_and_invalid_options(tmp_path):
    box = module.Outbox(str(tmp_path / "tickets.db"))
    for identity in ["a", "b", "c"]:
        box.enqueue(identity, {"external_reference": identity})
    assert box.work(lambda *args: 201, 0, limit=2) == ["delivered", "delivered"]
    assert box.work(lambda *args: 201, 0, limit=2) == ["delivered"]
    for now, limit in [(float("nan"), 1), (0, 0), (0, True), (0, 1.5)]:
        with pytest.raises(ValueError):
            box.work(lambda *args: 201, now, limit=limit)
