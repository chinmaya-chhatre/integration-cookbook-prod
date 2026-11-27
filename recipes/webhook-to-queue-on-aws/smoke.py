"""Exercise the running stack with synthetic data and no external AWS access."""

import hashlib
import hmac
import json
import os
import sys
import time
from pathlib import Path

import httpx
from queue_recipe.queue import client, queue_url


def main():
    raw = json.dumps(
        {
            "event_id": "demo-queue-1",
            "kind": "support.job",
            "data": {"message": "नमस्कार + &", "empty": ""},
        },
        ensure_ascii=False,
    ).encode()
    signature = hmac.new(os.environ["QUEUE_SOURCE_SECRET"].encode(), raw, hashlib.sha256).hexdigest()
    with httpx.Client(timeout=8, trust_env=False) as http:

        def send(sig):
            return http.post("http://edge:8080/hooks/generic", content=raw, headers={"X-Signature": sig})

        if len(sys.argv) > 1 and sys.argv[1] == "queue-down":
            assert send(signature).status_code == 503
            print("queue failure prevents producer acknowledgement")
            return
        assert send("invalid").status_code == 403
        sqs = client()
        url = queue_url(sqs)
        # CI stops the worker first, proving 202 means queued independently of target work.
        assert send(signature).status_code == 202
        messages = sqs.receive_message(QueueUrl=url, MaxNumberOfMessages=1, WaitTimeSeconds=2)["Messages"]
        envelope = json.loads(messages[0]["Body"])
        assert envelope["event_id"] == "demo-queue-1"
        assert envelope["body_sha256"] == hashlib.sha256(raw).hexdigest()
        sqs.change_message_visibility(
            QueueUrl=url, ReceiptHandle=messages[0]["ReceiptHandle"], VisibilityTimeout=0
        )
        assert send(signature).status_code == 202
        print("signed queue acceptance, invalid signature rejection and duplicate enqueue passed")


def completed():
    with httpx.Client(timeout=3, trust_env=False) as http:
        for _ in range(100):
            response = http.get(
                "http://target:8080/jobs",
                headers={
                    "Authorization": "Bearer " + os.environ["QUEUE_TARGET_TOKEN"],
                },
            )
            response.raise_for_status()
            jobs = response.json()
            if len(jobs) == 1:
                break
            time.sleep(0.1)
        assert len(jobs) == 1
        assert jobs[0]["external_reference"] == "cookbook:demo-queue-1"
        assert jobs[0]["data"] == {"message": "नमस्कार + &", "empty": ""}
        # Wait for both queue copies to be deleted rather than merely checking a fast target.
        sqs = client()
        url = queue_url(sqs)
        for _ in range(100):
            attributes = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["All"])["Attributes"]
            if (
                attributes["ApproximateNumberOfMessages"] == "0"
                and attributes["ApproximateNumberOfMessagesNotVisible"] == "0"
            ):
                break
            time.sleep(0.1)
        assert attributes["ApproximateNumberOfMessages"] == "0"
        assert attributes["ApproximateNumberOfMessagesNotVisible"] == "0"
        assert (
            len(
                http.get(
                    "http://target:8080/jobs",
                    headers={
                        "Authorization": "Bearer " + os.environ["QUEUE_TARGET_TOKEN"],
                    },
                ).json()
            )
            == 1
        )
        print("worker delivered once and deleted all current receipts")


def happy_paths():
    """Check realistic signed fixtures through the running LocalStack stack."""
    expected = {}
    with httpx.Client(timeout=8, trust_env=False) as http:
        for fixture in sorted(Path(__file__).with_name("fixtures").glob("*.json")):
            raw = fixture.read_bytes()
            job = json.loads(raw)
            digest = hashlib.sha256(raw).hexdigest()
            signature = hmac.new(os.environ["QUEUE_SOURCE_SECRET"].encode(), raw, hashlib.sha256).hexdigest()
            response = http.post(
                "http://edge:8080/hooks/generic", content=raw, headers={"X-Signature": signature}
            )
            assert response.status_code == 202
            assert response.json()["queued"] is True
            capture_id = response.json()["capture_id"]
            assert type(capture_id) is int and capture_id > 0
            expected["cookbook:" + job["event_id"]] = {
                "external_reference": "cookbook:" + job["event_id"],
                "body_sha256": digest,
                "data": job["data"],
            }
        assert len(expected) == 3
        for _ in range(100):
            response = http.get(
                "http://target:8080/jobs",
                headers={"Authorization": "Bearer " + os.environ["QUEUE_TARGET_TOKEN"]},
            )
            response.raise_for_status()
            actual = {item["external_reference"]: item for item in response.json()}
            if all(actual.get(key) == value for key, value in expected.items()):
                break
            time.sleep(0.1)
        assert all(actual.get(key) == value for key, value in expected.items())
        sqs = client()
        url = queue_url(sqs)
        for _ in range(100):
            attrs = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["All"])["Attributes"]
            if attrs["ApproximateNumberOfMessages"] == attrs["ApproximateNumberOfMessagesNotVisible"] == "0":
                break
            time.sleep(0.1)
        assert attrs["ApproximateNumberOfMessages"] == attrs["ApproximateNumberOfMessagesNotVisible"] == "0"
        print("three signed support fixtures retained exact data and digest, completed and emptied the queue")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "happy-paths":
        happy_paths()
    elif len(sys.argv) > 1 and sys.argv[1] == "completed":
        completed()
    else:
        main()
