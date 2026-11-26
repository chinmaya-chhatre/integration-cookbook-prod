"""Private bridge and target, public acknowledgement edge, and worker health server."""

import asyncio
import json
import os
import signal
import sys
from contextlib import asynccontextmanager
from threading import Event, Thread

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request

from .contract import MAX_BODY, envelope, verify
from .queue import bootstrap, client, queue_url
from .state import Ledger
from .worker import run

BRIDGE = "http://bridge:8080/enqueue"
HOOKSCOPE = "http://hookscope:8000"
TARGET = "http://target:8080/jobs"
NAMESPACE = "cookbook"
RULE = "queue-job"


async def body(request: Request, limit: int = MAX_BODY) -> bytes:
    chunks = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise HTTPException(413, "body too large")
        chunks.append(chunk)
    return b"".join(chunks)


def application(role: str, sqs=None, ledger=None) -> FastAPI:
    stop = Event()
    thread = None
    url = queue_url(sqs) if sqs is not None else None
    secret = os.environ["QUEUE_SOURCE_SECRET"]
    admin = os.environ["QUEUE_ADMIN_TOKEN"]
    target_token = os.environ["QUEUE_TARGET_TOKEN"]
    if not all((secret, admin, target_token)):
        raise ValueError("demo credentials are required")

    @asynccontextmanager
    async def lifespan(_):
        nonlocal thread
        if role == "worker":
            thread = Thread(target=run, args=(sqs, url, NAMESPACE, ledger, TARGET, target_token, stop))
            thread.start()
        try:
            yield
        finally:
            stop.set()
            if thread:
                thread.join(timeout=20)

    app = FastAPI(lifespan=lifespan)

    @app.get("/healthz")
    def health():
        if role == "worker" and (thread is None or not thread.is_alive()):
            raise HTTPException(503, "worker stopped")
        return {"ok": True}

    @app.post("/hooks/generic")
    async def ingress(request: Request):
        if role != "edge":
            raise HTTPException(404)
        raw = await body(request)
        signature = request.headers.get("x-signature", "")
        try:
            verify(raw, signature, secret)
        except PermissionError as exc:
            raise HTTPException(403, "invalid signature") from exc
        except (ValueError, RecursionError) as exc:
            raise HTTPException(400, "invalid job") from exc
        async with httpx.AsyncClient(timeout=0.5, follow_redirects=False, trust_env=False) as http:
            try:
                receipt = await http.post(
                    HOOKSCOPE + "/hooks/generic",
                    content=raw,
                    headers={
                        "Content-Type": "application/json",
                        "X-Signature": signature,
                    },
                )
                receipt.raise_for_status()
                event = receipt.json()
                event_id = event["id"]
                if (
                    type(event_id) is not int
                    or event_id < 1
                    or event["verification"] != "valid"
                    or RULE not in event["forwarded_to"]
                ):
                    raise ValueError("unconfirmed capture")
                deadline = asyncio.get_running_loop().time() + 3
                while asyncio.get_running_loop().time() < deadline:
                    history = await http.get(
                        f"{HOOKSCOPE}/api/events/{event_id}/forwards",
                        headers={"Authorization": "Bearer " + admin},
                    )
                    history.raise_for_status()
                    for forward in history.json():
                        status = forward["status_code"]
                        if (
                            forward["rule"] == RULE
                            and forward["target_url"] == BRIDGE
                            and type(status) is int
                            and 200 <= status < 300
                        ):
                            from fastapi.responses import JSONResponse

                            return JSONResponse({"queued": True, "capture_id": event_id}, status_code=202)
                    await asyncio.sleep(0.05)
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                pass
        raise HTTPException(503, "queue handoff not confirmed; retry with the same event identity")

    @app.post("/enqueue")
    async def enqueue(request: Request):
        if role != "bridge":
            raise HTTPException(404)
        raw = await body(request)
        try:
            verify(raw, request.headers.get("x-signature", ""), secret)
            message = envelope(raw, NAMESPACE)
        except PermissionError as exc:
            raise HTTPException(403, "invalid signature") from exc
        except (ValueError, RecursionError) as exc:
            raise HTTPException(400, "invalid job") from exc
        try:
            result = await asyncio.to_thread(sqs.send_message, QueueUrl=url, MessageBody=message)
            if not result.get("MessageId"):
                raise ValueError("queue did not confirm acceptance")
        except Exception as exc:
            raise HTTPException(503, "queue unavailable") from exc
        return {"queued": True}

    def authenticate(request):
        if request.headers.get("authorization") != "Bearer " + target_token:
            raise HTTPException(401)

    @app.get("/jobs")
    def jobs(request: Request):
        if role != "target":
            raise HTTPException(404)
        authenticate(request)
        return ledger.jobs()

    @app.post("/jobs")
    async def accept_job(request: Request):
        if role != "target":
            raise HTTPException(404)
        authenticate(request)
        raw = await body(request, limit=128 * 1024)
        try:
            payload = json.loads(raw)
            key, digest = payload["external_reference"], payload["body_sha256"]
            if (
                not isinstance(key, str)
                or not isinstance(digest, str)
                or request.headers.get("idempotency-key") != key
                or not isinstance(payload["data"], dict)
            ):
                raise ValueError("invalid target request")
        except (ValueError, KeyError, TypeError) as exc:
            raise HTTPException(400, "invalid target request") from exc
        with ledger.transaction() as conn:
            old = conn.execute("SELECT * FROM outcomes WHERE key = ?", (key,)).fetchone()
            if old and old["digest"] != digest:
                raise HTTPException(409, "identity conflict")
            if not old:
                Ledger.record(conn, key, digest, "completed", payload)
        return {"completed": True, "key": key, "digest": digest}

    return app


def main():
    role = sys.argv[1]
    if role == "bootstrap":
        bootstrap(client())
        return
    if role not in {"edge", "bridge", "worker", "target"}:
        raise ValueError("unknown runtime role")
    sqs = client() if role in {"bridge", "worker"} else None
    ledger = Ledger("/data/outcomes.db") if role in {"worker", "target"} else None
    # Uvicorn handles SIGTERM. Worker shutdown uses the lifespan stop event.
    signal.signal(signal.SIGINT, signal.default_int_handler)
    uvicorn.run(application(role, sqs, ledger), host="0.0.0.0", port=8080, access_log=False)


if __name__ == "__main__":
    main()
