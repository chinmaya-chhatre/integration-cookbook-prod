import json
import time
from urllib.parse import parse_qs

import pytest

from common import fixtures, signing


def test_stripe_invoice_paid_defaults_and_overrides():
    request = fixtures.stripe_invoice_paid(amount_paid=500_000, currency="eur")
    event = json.loads(request.body)
    assert event["type"] == "invoice.paid"
    assert event["data"]["object"]["amount_paid"] == 500_000
    assert event["data"]["object"]["currency"] == "eur"
    assert request.headers["Stripe-Signature"].startswith("t=")


def test_stripe_timestamp_is_in_header_and_payload():
    ts = int(time.time()) - 60
    request = fixtures.stripe_invoice_paid(timestamp=ts)
    assert request.headers["Stripe-Signature"].startswith(f"t={ts},")
    assert json.loads(request.body)["created"] == ts


def test_github_pull_request_reports_its_event_type():
    request = fixtures.github_pull_request(action="closed", merged=True, number=7)
    payload = json.loads(request.body)
    assert request.headers["X-GitHub-Event"] == "pull_request"
    assert payload["pull_request"]["merged"] is True
    assert payload["pull_request"]["state"] == "closed"
    assert payload["repository"]["full_name"] == "acme/billing-service"


def test_shopify_order_has_line_items_and_topic():
    request = fixtures.shopify_order_created(quantity=3)
    order = json.loads(request.body)
    assert request.headers["X-Shopify-Topic"] == "orders/create"
    assert order["line_items"][0]["quantity"] == 3
    assert order["shipping_address"]["country_code"] == "US"


def test_slack_slash_command_is_form_encoded_with_a_response_url():
    request = fixtures.slack_slash_command(text="hello world")
    fields = parse_qs(request.body.decode())
    assert fields["command"] == ["/ticket"]
    assert fields["text"] == ["hello world"]
    assert fields["response_url"][0].startswith("https://hooks.slack.com/")
    assert request.all_headers()["Content-Type"] == "application/x-www-form-urlencoded"


def test_slack_url_verification_carries_the_challenge():
    request = fixtures.slack_url_verification("abc")
    assert json.loads(request.body) == {"token": "unused", "challenge": "abc", "type": "url_verification"}


def test_twilio_sms_is_signed_for_the_given_url():
    url = "https://example.ngrok.app/hooks/twilio"
    request = fixtures.twilio_inbound_sms(url, body="Help")
    fields = signing.parse_form(request.body)
    assert ("Body", "Help") in fields
    assert request.headers["X-Twilio-Signature"] == signing.twilio_signature(
        fixtures.SECRETS["twilio"], url, fields
    )
    other = fixtures.twilio_inbound_sms("http://example.ngrok.app/hooks/twilio", body="Help")
    assert other.headers != request.headers


def test_svix_and_generic_events_are_signed():
    assert fixtures.svix_event().headers["svix-signature"].startswith("v1,")
    assert len(fixtures.generic_event().headers["X-Signature"]) == 64


@pytest.mark.parametrize(
    "build",
    [
        fixtures.stripe_invoice_paid,
        fixtures.github_pull_request,
        fixtures.shopify_order_created,
        fixtures.svix_event,
        fixtures.generic_event,
    ],
)
def test_json_bodies_are_stable_bytes(build):
    assert build().body == build().body  # no clock or randomness in the payload itself


def test_dumps_is_compact_and_sorted():
    assert fixtures.dumps({"b": 1, "a": [1, 2]}) == b'{"a":[1,2],"b":1}'
