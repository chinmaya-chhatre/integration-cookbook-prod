"""Sign webhook payloads the way each provider does.

Every function returns a ``SignedRequest``: the exact body bytes, the headers the provider
would send, and the URL it was signed for. Send those bytes unchanged. Re-serialising the JSON
changes the signature, which is the most common cause of "signature mismatch" in practice.

Pass ``timestamp`` to make a request deterministic; the default is the current time. Only the
standard library is used, so recipes can depend on this module without extra installs.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode


@dataclass(frozen=True)
class SignedRequest:
    body: bytes
    headers: dict[str, str]
    url: str = ""
    content_type: str = "application/json"
    extra: dict[str, str] = field(default_factory=dict)

    def all_headers(self) -> dict[str, str]:
        """Headers including Content-Type, ready to hand to an HTTP client."""
        return {"Content-Type": self.content_type, **self.headers}


def _now(timestamp: int | None) -> int:
    return int(time.time()) if timestamp is None else timestamp


def _hmac(key: bytes, message: bytes, digestmod=hashlib.sha256) -> bytes:
    return hmac.new(key, message, digestmod).digest()


def github(
    body: bytes, secret: str, event: str = "push", delivery: str = "0b1c3d4e-0000-4000-8000-000000000001"
) -> SignedRequest:
    """``X-Hub-Signature-256: sha256=<hex HMAC-SHA256 of the body>``."""
    digest = _hmac(secret.encode(), body).hex()
    return SignedRequest(
        body,
        {
            "X-Hub-Signature-256": f"sha256={digest}",
            "X-GitHub-Event": event,
            "X-GitHub-Delivery": delivery,
            "User-Agent": "GitHub-Hookshot/0000000",
        },
    )


def stripe(body: bytes, secret: str, timestamp: int | None = None) -> SignedRequest:
    """``Stripe-Signature: t=<ts>,v1=<hex HMAC-SHA256 of "<ts>.<body>">``."""
    ts = _now(timestamp)
    digest = _hmac(secret.encode(), str(ts).encode() + b"." + body).hex()
    return SignedRequest(body, {"Stripe-Signature": f"t={ts},v1={digest}"})


def shopify(
    body: bytes, secret: str, topic: str = "orders/create", shop: str = "example.myshopify.com"
) -> SignedRequest:
    """``X-Shopify-Hmac-Sha256: <base64 HMAC-SHA256 of the body>``."""
    digest = base64.b64encode(_hmac(secret.encode(), body)).decode()
    return SignedRequest(
        body,
        {
            "X-Shopify-Hmac-Sha256": digest,
            "X-Shopify-Topic": topic,
            "X-Shopify-Shop-Domain": shop,
        },
    )


def slack(
    body: bytes, secret: str, timestamp: int | None = None, content_type: str = "application/json"
) -> SignedRequest:
    """``X-Slack-Signature: v0=<hex HMAC-SHA256 of "v0:<ts>:<body>">`` plus the timestamp header."""
    ts = _now(timestamp)
    digest = _hmac(secret.encode(), b"v0:" + str(ts).encode() + b":" + body).hex()
    return SignedRequest(
        body,
        {"X-Slack-Signature": f"v0={digest}", "X-Slack-Request-Timestamp": str(ts)},
        content_type=content_type,
    )


def twilio(params: dict[str, str] | list[tuple[str, str]], auth_token: str, url: str) -> SignedRequest:
    """Form-encoded Twilio request: ``X-Twilio-Signature`` over the full URL plus sorted parameters.

    The signature covers the exact public URL Twilio was configured with, including the scheme
    and any query string. Behind a proxy that rewrites the URL, verification fails; that is the
    pitfall recipe R5 covers.
    """
    pairs = list(params.items()) if isinstance(params, dict) else list(params)
    return SignedRequest(
        urlencode(pairs).encode(),
        {"X-Twilio-Signature": twilio_signature(auth_token, url, pairs)},
        url=url,
        content_type="application/x-www-form-urlencoded",
    )


def twilio_signature(auth_token: str, url: str, params: list[tuple[str, str]]) -> str:
    """Base64 HMAC-SHA1 of ``url`` followed by every parameter name and value, sorted by name."""
    data = url + "".join(name + value for name, value in sorted(params))
    return base64.b64encode(_hmac(auth_token.encode(), data.encode(), hashlib.sha1)).decode()


def twilio_json(body: bytes, auth_token: str, url: str) -> SignedRequest:
    """Twilio request with a JSON body: the URL carries ``bodySHA256`` and is what gets signed."""
    separator = "&" if "?" in url else "?"
    signed_url = f"{url}{separator}bodySHA256={hashlib.sha256(body).hexdigest()}"
    return SignedRequest(
        body,
        {"X-Twilio-Signature": twilio_signature(auth_token, signed_url, [])},
        url=signed_url,
    )


def svix(
    body: bytes,
    secret: str,
    msg_id: str = "msg_2Lh9KRb0pzN4LePd3XiA4v2Kz2D",
    timestamp: int | None = None,
    prefix: str = "svix",
) -> SignedRequest:
    """Svix / Standard Webhooks: ``<prefix>-signature: v1,<base64 HMAC-SHA256 of "id.ts.body">``.

    ``secret`` is the ``whsec_...`` string; the key is its base64 part. Use ``prefix="webhook"``
    for senders that follow the Standard Webhooks header names.
    """
    ts = _now(timestamp)
    key = base64.b64decode(secret.removeprefix("whsec_"))
    signed = f"{msg_id}.{ts}.".encode() + body
    signature = base64.b64encode(_hmac(key, signed)).decode()
    return SignedRequest(
        body,
        {
            f"{prefix}-id": msg_id,
            f"{prefix}-timestamp": str(ts),
            f"{prefix}-signature": f"v1,{signature}",
        },
    )


def generic(body: bytes, secret: str) -> SignedRequest:
    """``X-Signature: <hex HMAC-SHA256 of the body>``, for senders without a standard."""
    return SignedRequest(body, {"X-Signature": _hmac(secret.encode(), body).hex()})


def parse_form(body: bytes) -> list[tuple[str, str]]:
    """The parameters of a form-encoded body, in order."""
    return parse_qsl(body.decode(), keep_blank_values=True)
