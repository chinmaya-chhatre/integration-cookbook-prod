"""Signed support jobs cross the public edge, pinned HookScope, queue and worker."""

import base64
import hashlib
import importlib
import json
import sys
from contextlib import ExitStack
from pathlib import Path
from threading import Event

import httpx
import pytest
from fastapi.testclient import TestClient
from hookscope.main import create_app
from hookscope.rules import parse_rules
from hookscope.store import EventStore

from common.fixtures import SECRETS
from common.signing import generic

RECIPE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RECIPE))
runtime = importlib.import_module("queue_recipe.runtime")
worker = importlib.import_module("queue_recipe.worker")
Ledger = importlib.import_module("queue_recipe.state").Ledger
FIXTURES = sorted((RECIPE / "fixtures").glob("*.json"))


class Queue:
    """SQS transport double. The Docker smoke check exercises real LocalStack."""

    def __init__(self):
        self.sent = []
        self.deleted = []
        self.stop = Event()
        self.received = False

    def get_queue_url(self, **_):
        return {"QueueUrl": "private-queue"}

    def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return {"MessageId": "message-1"}

    def receive_message(self, **_):
        if self.received:
            self.stop.set()
            return {}
        self.received = True
        assert len(self.sent) == 1
        return {
            "Messages": [
                {
                    "Body": self.sent[0]["MessageBody"],
                    "ReceiptHandle": "current-receipt",
                    "Attributes": {"ApproximateReceiveCount": "1"},
                }
            ]
        }

    def delete_message(self, **kwargs):
        self.deleted.append(kwargs)
        self.stop.set()


@pytest.mark.parametrize("fixture_path", FIXTURES, ids=lambda p: p.stem)
def test_signed_support_job_end_to_end(fixture_path, tmp_path, monkeypatch):
    monkeypatch.setenv("QUEUE_SOURCE_SECRET", SECRETS["generic"])
    monkeypatch.setenv("QUEUE_ADMIN_TOKEN", "demo-admin")
    monkeypatch.setenv("QUEUE_TARGET_TOKEN", "demo-target")
    raw = fixture_path.read_bytes()
    job = json.loads(raw)
    signed = generic(raw, SECRETS["generic"])
    digest = hashlib.sha256(raw).hexdigest()
    expected = {
        "external_reference": "cookbook:" + job["event_id"],
        "body_sha256": digest,
        "data": job["data"],
    }
    queue = Queue()
    worker_path = str(tmp_path / "worker.db")
    target_path = str(tmp_path / "target.db")
    ledger = Ledger(worker_path)
    captured = EventStore(str(tmp_path / "captures.db"))
    outbound = []
    http_client = httpx.Client
    async_client = httpx.AsyncClient

    with ExitStack() as stack:
        bridge = stack.enter_context(TestClient(runtime.application("bridge", queue)))
        target = stack.enter_context(TestClient(runtime.application("target", ledger=Ledger(target_path))))

        def forward(request):
            assert str(request.url) == runtime.BRIDGE
            assert request.content == raw
            assert request.headers["x-signature"] == signed.headers["X-Signature"]
            return bridge.post("/enqueue", content=request.content, headers=dict(request.headers))

        forwarding = stack.enter_context(http_client(transport=httpx.MockTransport(forward), trust_env=False))
        rules = parse_rules(json.loads((RECIPE / "rules.json").read_text()))
        hookscope = stack.enter_context(
            TestClient(
                create_app(
                    store=captured,
                    rules=rules,
                    secrets={"generic": SECRETS["generic"]},
                    admin_password="demo-admin",
                    http_client=forwarding,
                )
            )
        )

        def route_capture(request):
            assert str(request.url).startswith(runtime.HOOKSCOPE + "/")
            if request.method == "GET":
                assert request.headers["authorization"] == "Bearer demo-admin"
            else:
                assert "authorization" not in request.headers
            result = hookscope.request(
                request.method, request.url.path, content=request.content, headers=dict(request.headers)
            )
            return httpx.Response(result.status_code, content=result.content, headers=result.headers)

        monkeypatch.setattr(
            runtime.httpx,
            "AsyncClient",
            lambda **kwargs: async_client(transport=httpx.MockTransport(route_capture), **kwargs),
        )
        edge = stack.enter_context(TestClient(runtime.application("edge")))
        response = edge.post("/hooks/generic", content=signed.body, headers=signed.all_headers())
        assert response.status_code == 202
        capture_id = response.json()["capture_id"]
        assert response.json() == {"queued": True, "capture_id": capture_id}
        assert type(capture_id) is int and capture_id > 0
        capture = captured.get(capture_id)
        assert capture["verification"] == "valid"
        assert capture["source"] == "generic"
        assert capture["body"] == raw.decode("utf-8")
        assert [
            (x["rule"], x["target_url"], x["status_code"]) for x in captured.list_forwards(capture_id)
        ] == [(runtime.RULE, runtime.BRIDGE, 200)]
        assert len(queue.sent) == 1
        envelope = json.loads(queue.sent[0]["MessageBody"])
        assert envelope == {
            "schema_version": 1,
            "namespace": "cookbook",
            "event_id": job["event_id"],
            "body_base64": base64.b64encode(raw).decode(),
            "body_sha256": digest,
        }
        # Producer success precedes any target work or business completion.
        assert Ledger(target_path).jobs() == []
        assert ledger.jobs() == []
        assert queue.deleted == []

        def deliver(request):
            outbound.append(request)
            assert str(request.url) == runtime.TARGET
            assert request.headers["authorization"] == "Bearer demo-target"
            assert request.headers["idempotency-key"] == expected["external_reference"]
            assert "x-signature" not in request.headers
            assert json.loads(request.content) == expected
            result = target.post("/jobs", content=request.content, headers=dict(request.headers))
            assert result.json() == {
                "completed": True,
                "key": expected["external_reference"],
                "digest": digest,
            }
            return result

        with monkeypatch.context() as patch:
            patch.setattr(
                worker.httpx,
                "Client",
                lambda **kwargs: http_client(transport=httpx.MockTransport(deliver), **kwargs),
            )
            worker.run(queue, "private-queue", "cookbook", ledger, runtime.TARGET, "demo-target", queue.stop)

        assert len(outbound) == 1
        assert Ledger(worker_path).jobs() == [expected]
        assert Ledger(target_path).jobs() == [expected]
        assert queue.deleted == [{"QueueUrl": "private-queue", "ReceiptHandle": "current-receipt"}]
