"""What goes wrong, and what the recipe does about it: redeliveries, Slack failing, forged requests.

Same setup as the happy-path tests (see ``conftest.py``). Slack's failures are scripted with the mock's
``fail_next`` and ``rate_limit_next``; HookScope's retries use the policy in this recipe's ``rules.json``
(5 attempts), with the backoff wait skipped so the tests run in a moment.
"""

import json

import pytest

from common import fixtures

ATTEMPTS = 5  # rules.json: retry.attempts


def send(client, **kwargs):
    request = fixtures.stripe_invoice_paid(**kwargs)
    return client.post("/hooks/stripe", content=request.body, headers=request.all_headers())


def statuses(client, event_id=1) -> list[int | None]:
    return [f["status_code"] for f in client.get(f"/api/events/{event_id}/forwards").json()]


def headlines(slack) -> list[str]:
    return [m["text"].splitlines()[0] for m in slack.messages]


# ------------------------------------------------------------------------------ duplicates


def test_a_redelivered_event_is_announced_once(hookscope, slack):
    first = send(hookscope, amount_paid=500_000)
    again = send(hookscope, amount_paid=500_000)  # Stripe sends the same evt_ id again
    assert first.status_code == again.status_code == 202
    assert headlines(slack) == ["Invoice ACME-0042 paid: 5,000.00 USD"]
    # Both deliveries were stored and forwarded; the handler answered the second with a quiet 200.
    assert statuses(hookscope, 1) == [200] and statuses(hookscope, 2) == [200]


def test_a_second_event_about_the_same_invoice_is_news(hookscope, slack):
    send(hookscope, amount_paid=500_000, event_id="evt_first", invoice_id="in_same")
    send(hookscope, amount_paid=500_000, event_id="evt_correction", invoice_id="in_same")
    assert len(slack.messages) == 2


# --------------------------------------------------------------------------- slack failing


@pytest.mark.parametrize("scripted", ["server_error", "rate_limited"])
def test_a_slack_hiccup_is_retried_and_announced_once(hookscope, slack, scripted):
    if scripted == "server_error":
        slack.fail_next(1, status=500)
    else:
        slack.rate_limit_next(1, retry_after=1)
    assert send(hookscope, amount_paid=500_000).status_code == 202
    # The handler turned Slack's failure into a 502, HookScope retried, and the second try got through.
    assert statuses(hookscope) == [502, 200]
    assert headlines(slack) == ["Invoice ACME-0042 paid: 5,000.00 USD"]
    assert hookscope.get("/api/dead-letters").json() == []


def test_a_failed_attempt_does_not_mark_the_event_as_seen(hookscope, slack):
    slack.fail_next(1, status=500)
    send(hookscope, amount_paid=500_000)
    # The retry sent it. Had the first (failed) attempt been remembered, the retry would have been
    # skipped as a duplicate and the message lost.
    assert len(slack.messages) == 1


def test_a_long_outage_ends_in_a_dead_letter_and_loses_nothing(hookscope, slack):
    slack.fail_next(ATTEMPTS, status=503)
    send(hookscope, amount_paid=500_000)
    assert statuses(hookscope) == [502] * ATTEMPTS
    assert slack.messages == []
    [dead] = hookscope.get("/api/dead-letters").json()
    assert (dead["event_id"], dead["rule"], dead["resendable"]) == (1, "invoice-paid", True)


def test_resending_after_the_outage_delivers_the_message_once(hookscope, slack):
    slack.fail_next(ATTEMPTS, status=503)
    send(hookscope, amount_paid=500_000)
    slack.clear_faults()  # Slack is back
    resent = hookscope.post("/api/events/1/forwards/invoice-paid/resend")
    assert resent.status_code == 200 and resent.json()["status_code"] == 200
    assert headlines(slack) == ["Invoice ACME-0042 paid: 5,000.00 USD"]
    assert hookscope.get("/api/dead-letters").json() == []
    # A second click on "resend" must not announce it again.
    hookscope.post("/api/events/1/forwards/invoice-paid/resend")
    assert len(slack.messages) == 1


# ---------------------------------------------------------------------- what is not an error


@pytest.mark.parametrize(
    ("amount_paid", "currency"),
    [
        (499_999, "usd"),  # one cent under
        (749_999, "jpy"),  # one yen under: JPY has no cents, so this is NOT 7,499.99
        (10**9, "chf"),  # no threshold configured for the currency
    ],
)
def test_a_skip_is_a_quiet_success_never_a_retry(hookscope, slack, amount_paid, currency):
    send(hookscope, amount_paid=amount_paid, currency=currency)
    assert slack.messages == [] and slack.requests == []  # Slack was not even called
    assert statuses(hookscope) == [200]  # one attempt: HookScope saw success and did not retry
    assert hookscope.get("/api/dead-letters").json() == []


def test_the_jpy_boundary_is_the_whole_amount(hookscope, slack):
    send(hookscope, amount_paid=750_000, currency="jpy", event_id="evt_edge")
    assert headlines(slack) == ["Invoice ACME-0042 paid: 750,000 JPY"]


# ------------------------------------------------------------------------------- forged


def test_a_forged_request_is_stored_and_never_forwarded(hookscope, slack):
    request = fixtures.stripe_invoice_paid(amount_paid=900_000)
    headers = {**request.all_headers(), "Stripe-Signature": "t=1760000000,v1=" + "0" * 64}
    response = hookscope.post("/hooks/stripe", content=request.body, headers=headers)
    assert response.status_code == 401 and response.json()["verification"] == "invalid"
    assert slack.messages == [] and statuses(hookscope) == []
    assert hookscope.get("/api/events/1").json()["verification"] == "invalid"  # kept for debugging


def test_a_tampered_body_fails_the_signature(hookscope, slack):
    request = fixtures.stripe_invoice_paid(amount_paid=500_000)
    tampered = json.loads(request.body)
    tampered["data"]["object"]["amount_paid"] = 50_000_000  # attacker inflates the amount
    response = hookscope.post(
        "/hooks/stripe", content=json.dumps(tampered).encode(), headers=request.all_headers()
    )
    assert response.status_code == 401
    assert slack.messages == []
