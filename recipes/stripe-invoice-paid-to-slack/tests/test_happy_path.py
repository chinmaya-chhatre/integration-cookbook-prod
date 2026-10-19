"""Signed invoice.paid events end to end: HookScope verifies and forwards, the handler decides.

The fixtures in ``conftest.py`` run HookScope in-process and the handler as a real HTTP server, with the
repository's Slack mock behind it; without HookScope importable these tests are skipped, not failed.
"""

import json

import pytest

from common import fixtures


def send(client, **kwargs):
    request = fixtures.stripe_invoice_paid(**kwargs)
    return client.post("/hooks/stripe", content=request.body, headers=request.all_headers())


def forwards(client) -> list[dict]:
    return client.get("/api/events/1/forwards").json()


def test_a_large_usd_invoice_reaches_slack(hookscope, slack):
    response = send(hookscope, amount_paid=500_000, currency="usd")
    assert response.status_code == 202 and response.json()["verification"] == "valid"
    assert [m["text"].splitlines()[0] for m in slack.messages] == ["Invoice ACME-0042 paid: 5,000.00 USD"]
    assert [(f["rule"], f["status_code"]) for f in forwards(hookscope)] == [("invoice-paid", 200)]


def test_the_message_has_everything_finance_needs(hookscope, slack):
    send(hookscope, amount_paid=1_250_000, customer_email="ap@globex.example", invoice_id="in_1Globex")
    assert slack.messages[0]["text"] == (
        "Invoice ACME-0042 paid: 12,500.00 USD\n"
        "Customer: ap@globex.example\n"
        "Open invoice: https://invoice.stripe.com/i/acct_test/in_1Globex\n"
        "(evt_1QxAcmePaid, test mode)"
    )


@pytest.mark.parametrize(
    ("amount_paid", "currency", "shown"),
    [(450_000, "eur", "4,500.00 EUR"), (400_000, "gbp", "4,000.00 GBP"), (750_000, "jpy", "750,000 JPY")],
)
def test_each_currency_uses_its_own_threshold(hookscope, slack, amount_paid, currency, shown):
    send(hookscope, amount_paid=amount_paid, currency=currency)
    assert len(slack.messages) == 1 and shown in slack.messages[0]["text"]


def test_a_small_invoice_is_acknowledged_and_silent(hookscope, slack):
    response = send(hookscope, amount_paid=2_900)
    assert response.status_code == 202
    assert slack.messages == []
    # The handler answered 200 "skipped", so HookScope counts it delivered and does not retry.
    assert [f["status_code"] for f in forwards(hookscope)] == [200]


def test_distinct_invoices_each_get_a_message(hookscope, slack):
    send(hookscope, amount_paid=600_000, event_id="evt_1", invoice_id="in_1")
    send(hookscope, amount_paid=700_000, event_id="evt_2", invoice_id="in_2")
    assert [m["text"].splitlines()[0].split()[-2] for m in slack.messages] == ["6,000.00", "7,000.00"]


def test_other_stripe_events_never_reach_the_handler(hookscope, slack):
    other = fixtures.stripe_invoice_paid(amount_paid=900_000)
    body = json.loads(other.body)
    body["type"] = "invoice.created"
    request = fixtures.signing.stripe(fixtures.dumps(body), fixtures.SECRETS["stripe"])
    assert (
        hookscope.post("/hooks/stripe", content=request.body, headers=request.all_headers()).status_code
        == 202
    )
    assert slack.messages == [] and forwards(hookscope) == []
