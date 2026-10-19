"""Stripe ``invoice.paid`` to Slack: threshold per currency, dedupe on the event id, readable message.

HookScope forwards the verified event here. The handler answers 2xx for anything deliberately
ignored (so HookScope does not retry it) and 502 only when Slack could not be reached or refused the
message, which is the one case a retry can fix. Standard library only, so the image stays small.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from collections import OrderedDict
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

log = logging.getLogger("invoice-handler")

# Stripe amounts are integers in the smallest unit. These currencies have no minor unit, so their
# integer already is the major amount (https://docs.stripe.com/currencies#zero-decimal).
ZERO_DECIMAL = frozenset(
    {
        "BIF",
        "CLP",
        "DJF",
        "GNF",
        "JPY",
        "KMF",
        "KRW",
        "MGA",
        "PYG",
        "RWF",
        "UGX",
        "VND",
        "VUV",
        "XAF",
        "XOF",
        "XPF",
    }
)
# Three-decimal currencies, where the smallest unit is a thousandth.
THREE_DECIMAL = frozenset({"BHD", "JOD", "KWD", "OMR", "TND"})


class ConfigError(ValueError):
    """The environment is wrong; the handler refuses to start rather than guess."""


def parse_thresholds(raw: str) -> dict[str, Decimal]:
    """``"USD:5000,EUR:4500"`` to ``{"USD": Decimal("5000"), "EUR": Decimal("4500")}``."""
    thresholds: dict[str, Decimal] = {}
    for part in filter(None, (p.strip() for p in raw.split(","))):
        code, _, amount = part.partition(":")
        code = code.strip().upper()
        try:
            value = Decimal(amount.strip())
        except ArithmeticError:
            value = Decimal(-1)
        if len(code) != 3 or not code.isalpha() or value < 0:
            raise ConfigError(
                f"INVOICE_THRESHOLDS: cannot read {part!r}, expected CODE:AMOUNT, e.g. USD:5000"
            )
        thresholds[code] = value
    if not thresholds:
        raise ConfigError("INVOICE_THRESHOLDS is empty, so no invoice could ever alert")
    return thresholds


def major_units(amount: int, currency: str) -> Decimal:
    """Convert Stripe's smallest-unit integer to the currency's major unit."""
    code = currency.upper()
    if code in ZERO_DECIMAL:
        return Decimal(amount)
    return Decimal(amount) / (1000 if code in THREE_DECIMAL else 100)


def format_amount(amount: int, currency: str) -> str:
    """``125000, "usd"`` to ``"1,250.00"``; zero-decimal currencies show no decimals, KWD three."""
    code = currency.upper()
    places = 0 if code in ZERO_DECIMAL else 3 if code in THREE_DECIMAL else 2
    return f"{major_units(amount, code):,.{places}f}"


def is_large(amount: int, currency: str, thresholds: dict[str, Decimal]) -> bool:
    """At or above the threshold for this currency; a currency without a threshold never alerts."""
    limit = thresholds.get(currency.upper())
    return limit is not None and major_units(amount, currency) >= limit


class SeenEvents:
    """The last ``capacity`` Stripe event ids, oldest forgotten first."""

    def __init__(self, capacity: int) -> None:
        self.capacity = max(1, capacity)
        self._ids: OrderedDict[str, None] = OrderedDict()

    def __contains__(self, event_id: str) -> bool:
        return event_id in self._ids

    def add(self, event_id: str) -> None:
        self._ids[event_id] = None
        self._ids.move_to_end(event_id)
        while len(self._ids) > self.capacity:
            self._ids.popitem(last=False)


def format_message(event: dict[str, Any]) -> str:
    """The text finance reads: number, amount, customer, link, and which event it came from."""
    invoice = event["data"]["object"]
    currency = str(invoice["currency"]).upper()
    mode = "live mode" if event.get("livemode") else "test mode"
    lines = [
        f"Invoice {invoice.get('number') or invoice['id']} paid: "
        f"{format_amount(invoice['amount_paid'], currency)} {currency}",
        f"Customer: {invoice.get('customer_email') or invoice.get('customer') or 'unknown'}",
    ]
    if invoice.get("hosted_invoice_url"):
        lines.append(f"Open invoice: {invoice['hosted_invoice_url']}")
    lines.append(f"({event['id']}, {mode})")
    return "\n".join(lines)


def post_to_slack(url: str, text: str, timeout: float = 10.0) -> None:
    """Send the message; raises ``OSError`` (including ``URLError``) if Slack did not take it."""
    request = urllib.request.Request(
        url, data=json.dumps({"text": text}).encode(), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=timeout):
        pass


class Handler:
    """The decision for one forwarded event, separate from HTTP so it is easy to test."""

    def __init__(self, thresholds: dict[str, Decimal], slack_url: str, dedupe_memory: int) -> None:
        self.thresholds = thresholds
        self.slack_url = slack_url
        self.seen = SeenEvents(dedupe_memory)

    def skip(self, event_id: str, reason: str) -> tuple[int, dict[str, str]]:
        """A deliberate non-event: answered 200 so it is not retried, logged so it can be explained."""
        log.info("skipped %s: %s", event_id, reason)
        return 200, {"result": "skipped", "reason": reason}

    def handle(self, body: bytes) -> tuple[int, dict[str, str]]:
        try:
            event = json.loads(body)
            event_id, invoice = event["id"], event["data"]["object"]
            amount, currency = int(invoice["amount_paid"]), str(invoice["currency"])
        except (ValueError, KeyError, TypeError):
            # Retrying the same bytes cannot fix them, so this is a 400, not a 5xx.
            return 400, {"result": "unreadable"}
        if event.get("type") != "invoice.paid":
            return self.skip(event_id, "not invoice.paid")
        if event_id in self.seen:
            return self.skip(event_id, "duplicate")
        if not is_large(amount, currency, self.thresholds):
            if currency.upper() not in self.thresholds:
                return self.skip(event_id, f"no threshold for {currency.upper()}")
            return self.skip(
                event_id, f"below threshold ({format_amount(amount, currency)} {currency.upper()})"
            )
        try:
            post_to_slack(self.slack_url, format_message(event))
        except OSError as exc:
            log.warning("slack unreachable or refused %s: %s", event_id, exc)
            return 502, {"result": "slack_failed"}
        # Remembered only after Slack accepted it, so a retry after a failure still sends.
        self.seen.add(event_id)
        log.info("sent %s", event_id)
        return 200, {"result": "sent"}


def make_server(handler: Handler, port: int) -> ThreadingHTTPServer:
    class Request(BaseHTTPRequestHandler):
        def _reply(self, status: int, payload: dict[str, str]) -> None:
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802 - http.server's naming
            if self.path == "/healthz":
                self._reply(200, {"status": "ok"})
            else:
                self._reply(404, {"result": "not_found"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/stripe/invoice-paid":
                self._reply(404, {"result": "not_found"})
                return
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self._reply(*handler.handle(body))

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            log.debug("%s " + format, self.address_string(), *args)

    return ThreadingHTTPServer(("0.0.0.0", port), Request)  # noqa: S104 - inside a container


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s"
    )
    try:
        thresholds = parse_thresholds(os.environ.get("INVOICE_THRESHOLDS", ""))
        slack_url = os.environ.get("SLACK_WEBHOOK_URL", "")
        if not slack_url.startswith(("http://", "https://")):
            raise ConfigError("SLACK_WEBHOOK_URL must be an http(s) URL")
        raw_memory = os.environ.get("DEDUPE_MEMORY", "10000")
        if not raw_memory.isdigit():
            raise ConfigError(f"DEDUPE_MEMORY must be a whole number, got {raw_memory!r}")
        memory = int(raw_memory)
    except ConfigError as exc:
        raise SystemExit(f"invalid configuration: {exc}") from exc
    log.info("thresholds: %s", {code: str(v) for code, v in thresholds.items()})
    make_server(Handler(thresholds, slack_url, memory), 8080).serve_forever()


if __name__ == "__main__":
    main()
