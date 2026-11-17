"""Strict Twilio form parsing and vendor-neutral ticket mapping.

Call only after the ingress has verified the original URL, signature and bytes.
This module has no network or storage side effects.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qsl

MAX_BODY_BYTES = 64 * 1024


@dataclass(frozen=True)
class Message:
    account_sid: str
    message_sid: str
    sender: str
    recipient: str
    body: str

    @property
    def identity(self) -> str:
        """Stable across retries; separate accounts cannot collide."""
        return f"twilio:{self.account_sid}:{self.message_sid}"


def parse_message(body: bytes) -> Message:
    """Decode exactly once, preserve values and reject ambiguous input."""
    if not body or len(body) > MAX_BODY_BYTES:
        raise ValueError("form body must contain 1 to 65536 bytes")
    text = body.decode("utf-8", errors="strict")
    if re.search(r"%(?![0-9A-Fa-f]{2})", text):
        raise ValueError("invalid percent escape")
    pairs = parse_qsl(
        text,
        keep_blank_values=True,
        strict_parsing=True,
        encoding="utf-8",
        errors="strict",
        max_num_fields=100,
    )
    fields: dict[str, str] = {}
    for key, value in pairs:
        if not key or key in fields:
            raise ValueError("empty or duplicate parameter name")
        fields[key] = value
    for key, prefix in (("AccountSid", "AC"), ("MessageSid", "SM")):
        if not re.fullmatch(prefix + r"[0-9a-fA-F]{32}", fields.get(key, "")):
            raise ValueError(f"invalid {key}")
    for key in ("From", "To", "Body"):
        if key not in fields:
            raise ValueError(f"missing {key}")
    if not fields["From"] or not fields["To"]:
        raise ValueError("From and To must be nonempty")
    if fields.get("NumMedia", "0") != "0":
        raise ValueError("media messages are outside this SMS recipe")
    return Message(
        fields["AccountSid"],
        fields["MessageSid"],
        fields["From"],
        fields["To"],
        fields["Body"],
    )


def ticket_payload(message: Message) -> dict[str, object]:
    """Map untrusted SMS text without interpreting markup or fetching URLs."""
    return {
        "external_reference": message.identity,
        "summary": f"Inbound SMS {message.message_sid}",
        "description": message.body,
        "contact": message.sender,
        "destination": message.recipient,
        "provider": "twilio",
    }
