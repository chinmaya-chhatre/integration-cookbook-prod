"""Validate exact source bytes and the versioned queue envelope."""

import base64
import hashlib
import hmac
import json
import re
from typing import TypeAlias, TypedDict

JSONValue: TypeAlias = "None | bool | int | float | str | list[JSONValue] | dict[str, JSONValue]"


class TargetJob(TypedDict):
    external_reference: str
    body_sha256: str
    data: dict[str, JSONValue]


MAX_BODY = 64 * 1024
MAX_ENVELOPE = 128 * 1024


def unique_object(pairs: list[tuple[str, JSONValue]]) -> dict[str, JSONValue]:
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("duplicate object key")
        result[name] = value
    return result


def invalid_number(_: str) -> None:
    raise ValueError("non-finite number")


def document(raw: bytes) -> dict:
    if not 0 < len(raw) <= MAX_BODY:
        raise ValueError("body size")
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object, parse_constant=invalid_number)
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("event_id"), str)
        or re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", value["event_id"]) is None
        or value.get("kind") != "support.job"
        or not isinstance(value.get("data"), dict)
    ):
        raise ValueError("unsupported job")
    return value


def verify(raw: bytes, signature: str, secret: str) -> dict:
    expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    if (
        not secret
        or re.fullmatch(r"[0-9a-f]{64}", signature) is None
        or not hmac.compare_digest(expected, signature)
    ):
        raise PermissionError("signature mismatch")
    return document(raw)


def envelope(raw: bytes, namespace: str) -> str:
    job = document(raw)
    if re.fullmatch(r"[a-z0-9-]{1,32}", namespace) is None:
        raise ValueError("invalid namespace")
    result = json.dumps(
        {
            "schema_version": 1,
            "namespace": namespace,
            "event_id": job["event_id"],
            "body_base64": base64.b64encode(raw).decode(),
            "body_sha256": hashlib.sha256(raw).hexdigest(),
        }
    )
    if len(result.encode()) > MAX_ENVELOPE:
        raise ValueError("envelope size")
    return result


def unpack(message: str, namespace: str) -> tuple[str, str, dict]:
    if len(message.encode()) > MAX_ENVELOPE:
        raise ValueError("envelope size")
    value = json.loads(message, object_pairs_hook=unique_object, parse_constant=invalid_number)
    if (
        not isinstance(value, dict)
        or type(value.get("schema_version")) is not int
        or value["schema_version"] != 1
        or value.get("namespace") != namespace
    ):
        raise ValueError("unsupported envelope")
    raw = base64.b64decode(value["body_base64"], validate=True)
    job = document(raw)
    digest = hashlib.sha256(raw).hexdigest()
    if value.get("body_sha256") != digest or value.get("event_id") != job["event_id"]:
        raise ValueError("envelope integrity")
    return namespace + ":" + job["event_id"], digest, job["data"]


def target_job(message: str, namespace: str) -> TargetJob:
    """Map a verified envelope to a target job without changing its business identity."""
    key, digest, data = unpack(message, namespace)
    return {"external_reference": key, "body_sha256": digest, "data": data}
