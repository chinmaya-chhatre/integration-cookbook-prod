"""Typed job delivery and manual acknowledgement of the current SQS receipt."""

import random
from collections.abc import Callable
from threading import Event
from typing import Literal, NotRequired, Protocol, TypedDict

import httpx

from .contract import TargetJob, target_job
from .state import Ledger

Outcome = Literal["completed", "blocked", "retry", "conflict", "invalid"]
Send = Callable[[str, TargetJob], httpx.Response]


class Message(TypedDict):
    Body: str
    ReceiptHandle: str
    Attributes: NotRequired[dict[str, str]]


class Received(TypedDict, total=False):
    Messages: list[Message]


class Queue(Protocol):
    def receive_message(
        self, *, QueueUrl: str, MaxNumberOfMessages: int, WaitTimeSeconds: int, AttributeNames: list[str]
    ) -> Received: ...

    def delete_message(self, *, QueueUrl: str, ReceiptHandle: str) -> object: ...

    def change_message_visibility(
        self, *, QueueUrl: str, ReceiptHandle: str, VisibilityTimeout: int
    ) -> object: ...


def target_outcome(response: httpx.Response, payload: TargetJob) -> Outcome:
    """Only a matching durable completion proof permits acknowledgement."""
    if response.status_code in {200, 201}:
        try:
            result = response.json()
        except ValueError:
            return "retry"
        if (
            isinstance(result, dict)
            and result.get("completed") is True
            and result.get("key") == payload["external_reference"]
            and result.get("digest") == payload["body_sha256"]
        ):
            return "completed"
        return "retry"
    if response.status_code in {408, 425, 429} or response.status_code >= 500:
        return "retry"
    return "blocked"


def process(message: str, namespace: str, ledger: Ledger, send: Send) -> Outcome:
    payload = target_job(message, namespace)
    key, digest = payload["external_reference"], payload["body_sha256"]
    with ledger.transaction() as conn:
        existing = conn.execute("SELECT * FROM outcomes WHERE key = ?", (key,)).fetchone()
        if existing:
            if existing["digest"] != digest:
                return "conflict"
            if existing["state"] in {"completed", "blocked"}:
                return existing["state"]
            raise ValueError("unsupported stored outcome")
        try:
            outcome = target_outcome(send(key, payload), payload)
        except httpx.HTTPError:
            return "retry"
        if outcome in {"completed", "blocked"}:
            Ledger.record(conn, key, digest, outcome, payload)
    return outcome


def retry_delay(receive_count: str, jitter: int) -> int:
    """Bound the backoff even for malformed queue metadata; jitter is 0 to 2."""
    try:
        count = max(1, min(int(receive_count), 5))
    except (ValueError, TypeError):
        count = 1
    return min(30, 2**count) + max(0, min(jitter, 2))


def handle_message(
    sqs: Queue, url: str, message: Message, namespace: str, ledger: Ledger, send: Send
) -> Outcome:
    """Commit business evidence before deleting this delivery's receipt."""
    try:
        outcome = process(message["Body"], namespace, ledger, send)
    except (ValueError, KeyError, TypeError, RecursionError):
        outcome = "invalid"
    if outcome == "completed":
        sqs.delete_message(QueueUrl=url, ReceiptHandle=message["ReceiptHandle"])
    else:
        delay = retry_delay(
            message.get("Attributes", {}).get("ApproximateReceiveCount", "1"), random.randrange(3)
        )
        sqs.change_message_visibility(
            QueueUrl=url, ReceiptHandle=message["ReceiptHandle"], VisibilityTimeout=delay
        )
    return outcome


def run(sqs: Queue, url: str, namespace: str, ledger: Ledger, target: str, token: str, stop: Event) -> None:
    with httpx.Client(timeout=3, follow_redirects=False, trust_env=False) as http:

        def send(key: str, payload: TargetJob) -> httpx.Response:
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
                    handle_message(sqs, url, message, namespace, ledger, send)
            except Exception:
                # Keep receipts undeleted on queue, storage, or transport failures.
                # Do not log exception messages that could contain payloads or credentials.
                print("worker iteration failed; message remains recoverable", flush=True)
                stop.wait(1)
