"""Manual SQS acknowledgement after a durable target result and ledger commit."""

import random
from threading import Event

import httpx

from .contract import unpack
from .state import Ledger


def process(message: str, namespace: str, ledger: Ledger, send) -> str:
    key, digest, data = unpack(message, namespace)
    with ledger.transaction() as conn:
        existing = conn.execute("SELECT * FROM outcomes WHERE key = ?", (key,)).fetchone()
        if existing:
            return existing["state"] if existing["digest"] == digest else "conflict"
        payload = {"external_reference": key, "body_sha256": digest, "data": data}
        try:
            response = send(key, payload)
        except httpx.HTTPError:
            return "retry"
        if response.status_code in {200, 201}:
            try:
                result = response.json()
                confirmed = (
                    result.get("completed") is True
                    and result.get("key") == key
                    and result.get("digest") == digest
                )
            except (ValueError, AttributeError):
                confirmed = False
            if not confirmed:
                return "retry"
            state = "completed"
        elif response.status_code in {408, 425, 429} or response.status_code >= 500:
            return "retry"
        else:
            state = "blocked"
        Ledger.record(conn, key, digest, state, payload)
    return state


def run(sqs, url: str, namespace: str, ledger: Ledger, target: str, token: str, stop: Event):
    with httpx.Client(timeout=3, follow_redirects=False, trust_env=False) as http:

        def send(key, payload):
            return http.post(
                target,
                json=payload,
                headers={
                    "Authorization": "Bearer " + token,
                    "Idempotency-Key": key,
                },
            )

        while not stop.is_set():
            try:
                response = sqs.receive_message(
                    QueueUrl=url,
                    MaxNumberOfMessages=1,
                    WaitTimeSeconds=2,
                    AttributeNames=["ApproximateReceiveCount"],
                )
                for message in response.get("Messages", []):
                    if stop.is_set():
                        return
                    try:
                        outcome = process(message["Body"], namespace, ledger, send)
                    except (ValueError, KeyError, TypeError, RecursionError):
                        outcome = "invalid"
                    if outcome == "completed":
                        sqs.delete_message(QueueUrl=url, ReceiptHandle=message["ReceiptHandle"])
                    else:
                        count = int(message.get("Attributes", {}).get("ApproximateReceiveCount", "1"))
                        delay = min(30, 2 ** min(count, 5)) + random.randrange(3)
                        sqs.change_message_visibility(
                            QueueUrl=url,
                            ReceiptHandle=message["ReceiptHandle"],
                            VisibilityTimeout=delay,
                        )
            except Exception:
                # Keep receipts undeleted on queue, storage, or transport failures.
                # Do not log exception messages that could contain payloads or credentials.
                print("worker iteration failed; message remains recoverable", flush=True)
                stop.wait(1)
