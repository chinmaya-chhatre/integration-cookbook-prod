"""LocalStack queue setup and a bounded SQS client. Never uses a live AWS endpoint."""

import json
import os


def client():
    import boto3
    from botocore.config import Config

    endpoint = os.environ.get("SQS_ENDPOINT", "http://localstack:4566")
    if endpoint != "http://localstack:4566":
        raise ValueError("this demonstration requires the private LocalStack endpoint")
    return boto3.client(
        "sqs",
        endpoint_url=endpoint,
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
        config=Config(connect_timeout=2, read_timeout=5, retries={"total_max_attempts": 1}),
    )


def bootstrap(sqs) -> None:
    dlq = sqs.create_queue(QueueName="cookbook-jobs-dlq", Attributes={"MessageRetentionPeriod": "1209600"})[
        "QueueUrl"
    ]
    arn = sqs.get_queue_attributes(QueueUrl=dlq, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
    sqs.create_queue(
        QueueName="cookbook-jobs",
        Attributes={
            "VisibilityTimeout": "30",
            "ReceiveMessageWaitTimeSeconds": "2",
            "MessageRetentionPeriod": "345600",
            "RedrivePolicy": json.dumps({"deadLetterTargetArn": arn, "maxReceiveCount": 5}),
        },
    )


def queue_url(sqs) -> str:
    return sqs.get_queue_url(QueueName="cookbook-jobs")["QueueUrl"]
