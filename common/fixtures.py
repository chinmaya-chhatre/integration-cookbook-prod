"""Realistic, signed example events for each provider, for use in recipe tests.

Each builder returns a ``SignedRequest`` ready to post to HookScope (or to a handler) with
``requests.all_headers()`` and ``request.body``. Keyword arguments override the interesting
fields so a test can say "an invoice for 5,000 USD" without hand-writing the payload.

The payloads follow the shape of the providers' documented examples, trimmed to the fields the
recipes read. They are illustrative test data, not recordings of real traffic.
"""

from __future__ import annotations

import json
from typing import Any

from . import signing
from .signing import SignedRequest

# Secrets used across the recipes' tests. They are public on purpose; never reuse them.
SECRETS = {
    "github": "cookbook-github-secret",
    "stripe": "whsec_cookbook_stripe_secret",
    "shopify": "cookbook-shopify-secret",
    "slack": "cookbook-slack-signing-secret",
    "twilio": "cookbook-twilio-auth-token",
    "svix": "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw",
    "generic": "cookbook-generic-secret",
}


def dumps(payload: dict[str, Any]) -> bytes:
    """Compact, stable JSON bytes: sign and send exactly these."""
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()


def stripe_invoice_paid(
    amount_paid: int = 125_000,
    currency: str = "usd",
    customer_email: str = "billing@acme.example",
    invoice_id: str = "in_1QxAcmeInvoice",
    event_id: str = "evt_1QxAcmePaid",
    timestamp: int | None = None,
) -> SignedRequest:
    """``invoice.paid``; ``amount_paid`` is in the smallest currency unit (cents)."""
    payload = {
        "id": event_id,
        "object": "event",
        "api_version": "2024-06-20",
        "type": "invoice.paid",
        "created": timestamp or 1_760_000_000,
        "livemode": False,
        "data": {
            "object": {
                "id": invoice_id,
                "object": "invoice",
                "number": "ACME-0042",
                "customer": "cus_Acme123",
                "customer_email": customer_email,
                "amount_due": amount_paid,
                "amount_paid": amount_paid,
                "currency": currency,
                "status": "paid",
                "hosted_invoice_url": f"https://invoice.stripe.com/i/acct_test/{invoice_id}",
            }
        },
    }
    return signing.stripe(dumps(payload), SECRETS["stripe"], timestamp=timestamp)


def github_pull_request(
    action: str = "opened",
    number: int = 42,
    title: str = "Fix retry backoff",
    repo: str = "acme/billing-service",
    merged: bool = False,
) -> SignedRequest:
    """A ``pull_request`` event."""
    payload = {
        "action": action,
        "number": number,
        "pull_request": {
            "id": 1_000_000 + number,
            "number": number,
            "title": title,
            "state": "closed" if action == "closed" else "open",
            "merged": merged,
            "html_url": f"https://github.com/{repo}/pull/{number}",
            "user": {"login": "octocat"},
            "head": {"ref": "fix/retry-backoff"},
            "base": {"ref": "main"},
        },
        "repository": {"full_name": repo},
        "sender": {"login": "octocat"},
    }
    return signing.github(dumps(payload), SECRETS["github"], event="pull_request")


def shopify_order_created(
    order_id: int = 820982911946154508,
    total: str = "199.00",
    currency: str = "USD",
    quantity: int = 2,
) -> SignedRequest:
    """An ``orders/create`` webhook with one line item and a shipping address."""
    payload = {
        "id": order_id,
        "name": "#1001",
        "email": "jane@example.com",
        "created_at": "2026-10-13T09:15:00-04:00",
        "currency": currency,
        "total_price": total,
        "financial_status": "paid",
        "line_items": [
            {
                "id": 866550311766439020,
                "sku": "TEE-BLK-M",
                "title": "Black tee",
                "quantity": quantity,
                "price": "99.50",
            }
        ],
        "shipping_address": {
            "name": "Jane Doe",
            "address1": "1 Main St",
            "city": "Boston",
            "province_code": "MA",
            "zip": "02110",
            "country_code": "US",
        },
    }
    return signing.shopify(dumps(payload), SECRETS["shopify"], topic="orders/create")


def slack_slash_command(
    command: str = "/ticket",
    text: str = "printer on fire",
    timestamp: int | None = None,
    url: str = "https://hooks.slack.com/commands/T0/1/xyz",
) -> SignedRequest:
    """A slash command: form-encoded, with a ``response_url`` for delayed replies."""
    from urllib.parse import urlencode

    form = urlencode(
        [
            ("team_id", "T0123"),
            ("channel_id", "C0123"),
            ("user_id", "U0123"),
            ("user_name", "jane"),
            ("command", command),
            ("text", text),
            ("response_url", url),
            ("trigger_id", "1234.5678.abcdef"),
        ]
    ).encode()
    return signing.slack(
        form, SECRETS["slack"], timestamp=timestamp, content_type="application/x-www-form-urlencoded"
    )


def slack_url_verification(
    challenge: str = "3eZbrw1aBm2rZgRNFdxV2595E9CY3gmdALWMmHkvFXO7tYXAYM8P",
) -> SignedRequest:
    """Slack's endpoint handshake, which must be answered with the challenge."""
    payload = {"token": "unused", "challenge": challenge, "type": "url_verification"}
    return signing.slack(dumps(payload), SECRETS["slack"])


def twilio_inbound_sms(
    url: str,
    body: str = "My order never arrived",
    from_number: str = "+16175550123",
    to_number: str = "+18005551212",
) -> SignedRequest:
    """An inbound SMS, form-encoded and signed for exactly ``url``."""
    params = {
        "MessageSid": "SM00000000000000000000000000000001",
        "AccountSid": "AC00000000000000000000000000000001",
        "From": from_number,
        "To": to_number,
        "Body": body,
        "NumMedia": "0",
    }
    return signing.twilio(params, SECRETS["twilio"], url)


def svix_event(
    event_type: str = "invoice.created", msg_id: str = "msg_2Lh9KRb0pzN4LePd3XiA4v2Kz2D"
) -> SignedRequest:
    """A Svix-signed event; works for any sender that uses Svix or Standard Webhooks."""
    payload = {
        "type": event_type,
        "timestamp": "2026-10-13T13:00:00Z",
        "data": {"id": "inv_123", "total": 4200},
    }
    return signing.svix(dumps(payload), SECRETS["svix"], msg_id=msg_id)


def generic_event(payload: dict[str, Any] | None = None) -> SignedRequest:
    """A plain JSON event signed with ``X-Signature``."""
    return signing.generic(dumps(payload or {"event": "ping", "id": "evt_generic_1"}), SECRETS["generic"])
