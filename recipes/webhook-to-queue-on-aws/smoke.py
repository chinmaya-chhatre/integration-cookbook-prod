"""Exercise the running stack with synthetic data and no external AWS access."""

import hashlib
import hmac
import json
import os
import sys
import time

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


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "completed":
        completed()
    else:
        main()
