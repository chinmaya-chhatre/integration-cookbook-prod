import importlib.util
from pathlib import Path

import pytest

PATH = Path(__file__).parents[1] / "recipes/twilio-sms-to-support-ticket/delivery.py"
SPEC = importlib.util.spec_from_file_location("twilio_delivery", PATH)
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def payload(identity):
    return {"external_reference": identity, "description": "Help"}


@pytest.mark.parametrize("status", [429, 500, 503])
def test_transient_failure_survives_restart_and_replay(tmp_path, status):
    path = str(tmp_path / "state.db")
    box = module.Outbox(path)
    box.enqueue("sms-1", payload("sms-1"))
    assert box.deliver("sms-1", lambda body, key: status) == "pending"
    restored = module.Outbox(path)
    calls = []
    assert restored.deliver("sms-1", lambda body, key: calls.append(key) or 201) == "delivered"
    assert restored.deliver("sms-1", lambda body, key: pytest.fail("duplicate")) == "delivered"
    assert calls == ["sms-1"]


def test_response_loss_reuses_target_idempotency_key(tmp_path):
    box = module.Outbox(str(tmp_path / "state.db"))
    box.enqueue("sms-1", payload("sms-1"))
    target = set()

    def lost_response(body, key):
        target.add(key)
        raise OSError("connection lost after acceptance")

    assert box.deliver("sms-1", lost_response) == "pending"
    assert box.deliver("sms-1", lambda body, key: target.add(key) or 200) == "delivered"
    assert target == {"sms-1"}


def test_duplicates_and_out_of_order_messages_are_independent(tmp_path):
    box = module.Outbox(str(tmp_path / "state.db"))
    assert box.enqueue("sms-new", payload("sms-new"))
    assert box.enqueue("sms-old", payload("sms-old"))
    assert not box.enqueue("sms-new", payload("sms-new"))
    calls = []
    for identity in ("sms-old", "sms-new"):
        assert box.deliver(identity, lambda body, key: calls.append(key) or 201) == "delivered"
    assert calls == ["sms-old", "sms-new"]


def test_permanent_failure_requires_review_and_identity_conflicts_fail(tmp_path):
    box = module.Outbox(str(tmp_path / "state.db"))
    box.enqueue("sms-1", payload("sms-1"))
    assert box.deliver("sms-1", lambda body, key: 400) == "review"
    with pytest.raises(ValueError, match="different content"):
        box.enqueue("sms-1", {**payload("sms-1"), "description": "changed"})
