"""The queue configuration must run all components and preserve acknowledgement boundaries."""

import hashlib
import hmac
import importlib
import json
import sys
from pathlib import Path
from threading import Event

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
RECIPE = ROOT / "recipes/webhook-to-queue-on-aws"
sys.path.insert(0, str(RECIPE))
contract = importlib.import_module("queue_recipe.contract")
worker = importlib.import_module("queue_recipe.worker")
runtime = importlib.import_module("queue_recipe.runtime")
Ledger = importlib.import_module("queue_recipe.state").Ledger
RAW = json.dumps({"event_id": "one", "kind": "support.job", "data": {"text": "café + &"}}).encode()
SECRET = "synthetic"
SIG = hmac.new(SECRET.encode(), RAW, hashlib.sha256).hexdigest()


@pytest.fixture(autouse=True)
def credentials(monkeypatch):
    monkeypatch.setenv("QUEUE_SOURCE_SECRET", SECRET)
    monkeypatch.setenv("QUEUE_ADMIN_TOKEN", "admin")
    monkeypatch.setenv("QUEUE_TARGET_TOKEN", "target")


class Queue:
    def __init__(self):
        self.messages = []
        self.fail = False

    def get_queue_url(self, **_):
        return {"QueueUrl": "private-queue"}

    def send_message(self, **kwargs):
        if self.fail:
            raise OSError("synthetic failure")
        self.messages.append(kwargs)
        return {"MessageId": "message"}


def test_signed_bridge_acceptance_after_queue_write():
    sqs = Queue()
    with TestClient(runtime.application("bridge", sqs)) as http:
        assert http.post("/enqueue", content=RAW, headers={"X-Signature": "bad"}).status_code == 403
        assert sqs.messages == []
        assert http.post("/enqueue", content=RAW, headers={"X-Signature": SIG}).status_code == 200
        key, digest, data = contract.unpack(sqs.messages[0]["MessageBody"], "cookbook")
        assert key == "cookbook:one"
        assert digest == hashlib.sha256(RAW).hexdigest()
        assert data == {"text": "café + &"}
        sqs.fail = True
        assert http.post("/enqueue", content=RAW, headers={"X-Signature": SIG}).status_code == 503
        assert len(sqs.messages) == 1


@pytest.mark.parametrize(
    "raw",
    [
        b"{}",
        b"[]",
        b'{"event_id":"a","event_id":"b"}',
        b'{"event_id":"a","kind":"other","data":{}}',
        b'{"event_id":"../a","kind":"support.job","data":{}}',
        b'{"event_id":"a","kind":"support.job","data":{"v":NaN}}',
        b" " * 65537,
        b"\xff",
    ],
)
def test_contract_rejects_unsupported_input(raw):
    with pytest.raises(ValueError):
        contract.document(raw)


def test_versioned_queue_integrity():
    message = json.loads(contract.envelope(RAW, "cookbook"))
    for key, value in (
        ("schema_version", True),
        ("schema_version", 2),
        ("body_sha256", "wrong"),
        ("event_id", "other"),
        ("namespace", "other"),
    ):
        with pytest.raises(ValueError):
            contract.unpack(json.dumps({**message, key: value}), "cookbook")


def test_worker_persists_completion_and_rejects_conflicting_reuse(tmp_path):
    ledger = Ledger(str(tmp_path / "ledger.db"))
    calls = []

    def send(key, payload):
        calls.append(payload)
        return httpx.Response(
            200,
            json={
                "completed": True,
                "key": key,
                "digest": payload["body_sha256"],
            },
        )

    message = contract.envelope(RAW, "cookbook")
    assert worker.process(message, "cookbook", ledger, send) == "completed"
    assert worker.process(message, "cookbook", Ledger(ledger.path), send) == "completed"
    conflicting = RAW.replace(b"caf", b"different-caf")
    assert worker.process(contract.envelope(conflicting, "cookbook"), "cookbook", ledger, send) == "conflict"
    assert len(calls) == len(ledger.jobs()) == 1


@pytest.mark.parametrize(
    "status,expected",
    [
        (202, "blocked"),
        (302, "blocked"),
        (400, "blocked"),
        (408, "retry"),
        (425, "retry"),
        (429, "retry"),
        (503, "retry"),
    ],
)
def test_worker_failure_boundary(status, expected, tmp_path):
    ledger = Ledger(str(tmp_path / "ledger.db"))
    assert (
        worker.process(
            contract.envelope(RAW, "cookbook"), "cookbook", ledger, lambda *_: httpx.Response(status)
        )
        == expected
    )
    assert len(ledger.jobs()) == (1 if expected == "blocked" else 0)


@pytest.mark.parametrize(
    "forwards,status",
    [
        ([], 503),
        ([{"rule": "other", "target_url": runtime.BRIDGE, "status_code": 200}], 503),
        ([{"rule": runtime.RULE, "target_url": "http://wrong", "status_code": 200}], 503),
        ([{"rule": runtime.RULE, "target_url": runtime.BRIDGE, "status_code": 500}], 503),
        ([{"rule": runtime.RULE, "target_url": runtime.BRIDGE, "status_code": 200}], 202),
    ],
)
def test_edge_requires_exact_confirmed_handoff(forwards, status, monkeypatch):
    requests = []
    original = httpx.AsyncClient

    def respond(request):
        requests.append(request)
        if request.method == "POST":
            assert request.content == RAW
            assert "authorization" not in request.headers
            return httpx.Response(
                200, json={"id": 7, "verification": "valid", "forwarded_to": [runtime.RULE]}
            )
        assert request.url.path == "/api/events/7/forwards"
        assert request.headers["authorization"] == "Bearer admin"
        return httpx.Response(200, json=forwards)

    monkeypatch.setattr(
        runtime.httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs),
    )
    with TestClient(runtime.application("edge")) as http:
        assert http.post("/hooks/generic", content=RAW, headers={"X-Signature": SIG}).status_code == status
    assert requests


def test_mock_target_durable_idempotency(tmp_path):
    path = str(tmp_path / "target.db")
    payload = {"external_reference": "cookbook:one", "body_sha256": "abc", "data": {}}
    headers = {"Authorization": "Bearer target", "Idempotency-Key": "cookbook:one"}
    for _ in range(2):
        with TestClient(runtime.application("target", ledger=Ledger(path))) as http:
            assert http.post("/jobs", json=payload, headers=headers).status_code == 200
            assert len(http.get("/jobs", headers=headers).json()) == 1
            assert (
                http.post("/jobs", json={**payload, "body_sha256": "different"}, headers=headers).status_code
                == 409
            )


def test_configuration_contains_complete_private_stack():
    config = yaml.safe_load((RECIPE / "docker-compose.yml").read_text())
    services = config["services"]
    assert set(services) == {"edge", "hookscope", "localstack", "bootstrap", "bridge", "worker", "target"}
    for name in set(services) - {"edge"}:
        assert not services[name].get("ports")
    assert services["worker"]["volumes"] == ["queue-ledger:/data"]
    assert services["target"]["volumes"] == ["queue-target:/data"]
    assert services["bridge"]["depends_on"]["bootstrap"]["condition"] == "service_completed_successfully"
    base = yaml.safe_load((ROOT / "docker-compose.base.yml").read_text())
    assert base["services"]["hookscope"]["build"]["context"].endswith(
        "b4e012c304ea69ca29fd544ab0ab2864e5cf11f6"
    )
    assert set(services["hookscope"]) <= {"environment", "depends_on"}
    rules = json.loads((RECIPE / "rules.json").read_text())["rules"]
    assert rules[0]["name"] == runtime.RULE
    assert rules[0]["target_url"] == runtime.BRIDGE
    assert (RECIPE / ".wip").exists()


@pytest.mark.parametrize("status", [200, 503])
def test_worker_deletes_only_confirmed_work_with_current_receipt(tmp_path, monkeypatch, status):
    stop = Event()
    deleted, released = [], []
    original = httpx.Client

    def receive(request):
        payload = json.loads(request.content)
        return httpx.Response(
            status,
            json={
                "completed": True,
                "key": request.headers["idempotency-key"],
                "digest": payload["body_sha256"],
            },
        )

    monkeypatch.setattr(
        worker.httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(receive), **kwargs)
    )

    class Receipts:
        def receive_message(self, **_):
            return {"Messages": [{"Body": contract.envelope(RAW, "cookbook"), "ReceiptHandle": "current"}]}

        def delete_message(self, **kwargs):
            deleted.append(kwargs["ReceiptHandle"])
            stop.set()

        def change_message_visibility(self, **kwargs):
            released.append(kwargs["ReceiptHandle"])
            stop.set()

    ledger = Ledger(str(tmp_path / "worker.db"))
    worker.run(Receipts(), "private", "cookbook", ledger, "http://target/jobs", "target", stop)
    assert deleted == (["current"] if status == 200 else [])
    assert released == (["current"] if status == 503 else [])
    assert len(ledger.jobs()) == (1 if status == 200 else 0)
