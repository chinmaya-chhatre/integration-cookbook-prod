"""Signatures checked against vectors published by the providers' own documentation."""

import base64
import hashlib
import hmac
from urllib.parse import urlencode

from common import generic, github, shopify, slack, stripe, svix, twilio, twilio_json

# GitHub docs, "Validating webhook deliveries".
GITHUB_SECRET = "It's a Secret to Everybody"
GITHUB_PAYLOAD = b"Hello, World!"
GITHUB_SIGNATURE = "sha256=757107ea0eb2509fc211221cce984b8a37570b6d7586c22c46f4379c8b043e17"

# Slack docs, "Verifying requests from Slack".
SLACK_SECRET = "8f742231b10e8888abcd99yyyzzz85a5"
SLACK_TS = 1531420618
SLACK_BODY = (
    b"token=xyzz0WbapA4vBCDEFasx0q6G&team_id=T1DC2JH3J&team_domain=testteamnow&channel_id=G8PSS9T3V"
    b"&channel_name=foobar&user_id=U2CERLKJA&user_name=roadrunner&command=%2Fwebhook-collect&text="
    b"&response_url=https%3A%2F%2Fhooks.slack.com%2Fcommands%2FT1DC2JH3J%2F397700885554%2F96rGlfmibIGlgcZRskXaIFfN"
    b"&trigger_id=398738663015.47445629121.803a0bc887a14d10d2c447fce8b6703c"
)
SLACK_SIGNATURE = "v0=a2114d57b48eac39b9ad189dd8316235a7b4a8d21a10bd27519666489c69b503"

# Twilio docs, "Validating Requests" (auth token 12345).
TWILIO_TOKEN = "12345"
TWILIO_URL = "https://mycompany.com/myapp.php?foo=1&bar=2"
TWILIO_PARAMS = {
    "CallSid": "CA1234567890ABCDE",
    "Caller": "+12349013030",
    "Digits": "1234",
    "From": "+12349013030",
    "To": "+18005551212",
}
TWILIO_JSON_BODY = b'{"property": "value", "boolean": true}'
TWILIO_JSON_URL = "https://mycompany.com/myapp.php?foo=1&bar=2"

# Svix docs, "Verifying webhooks manually".
SVIX_SECRET = "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw"
SVIX_BODY = b'{"test": 2432232314}'
SVIX_ID = "msg_p5jXN8AQM9LWM0D4loKWxJek"
SVIX_TS = 1614265330
SVIX_SIGNATURE = "v1,g0hM9SsE+OTPJTGt/tmIKtSyZlE3uFJELVlNIOLJ1OE="


def test_github_matches_the_docs_vector():
    request = github(GITHUB_PAYLOAD, GITHUB_SECRET)
    assert request.headers["X-Hub-Signature-256"] == GITHUB_SIGNATURE
    assert request.body == GITHUB_PAYLOAD
    assert request.headers["X-GitHub-Event"] == "push"


def test_slack_matches_the_docs_vector():
    request = slack(
        SLACK_BODY, SLACK_SECRET, timestamp=SLACK_TS, content_type="application/x-www-form-urlencoded"
    )
    assert request.headers["X-Slack-Signature"] == SLACK_SIGNATURE
    assert request.headers["X-Slack-Request-Timestamp"] == str(SLACK_TS)
    assert request.all_headers()["Content-Type"] == "application/x-www-form-urlencoded"


def test_twilio_form_matches_the_docs_vector():
    request = twilio(TWILIO_PARAMS, TWILIO_TOKEN, TWILIO_URL)
    assert request.headers["X-Twilio-Signature"] == "0/KCTR6DLpKmkAf8muzZqo1nDgQ="
    assert request.body == urlencode(TWILIO_PARAMS).encode()
    assert request.url == TWILIO_URL


def test_twilio_signature_ignores_parameter_order_but_not_the_url():
    reordered = dict(reversed(list(TWILIO_PARAMS.items())))
    assert (
        twilio(reordered, TWILIO_TOKEN, TWILIO_URL).headers
        == twilio(TWILIO_PARAMS, TWILIO_TOKEN, TWILIO_URL).headers
    )
    other = twilio(TWILIO_PARAMS, TWILIO_TOKEN, "http://mycompany.com/myapp.php?foo=1&bar=2")
    assert other.headers != twilio(TWILIO_PARAMS, TWILIO_TOKEN, TWILIO_URL).headers


def test_twilio_json_signs_the_url_with_the_body_hash():
    request = twilio_json(TWILIO_JSON_BODY, TWILIO_TOKEN, TWILIO_JSON_URL)
    assert request.url == TWILIO_JSON_URL + "&bodySHA256=" + hashlib.sha256(TWILIO_JSON_BODY).hexdigest()
    assert request.url.endswith("0a1ff7634d9ab3b95db5c9a2dfe9416e41502b283a80c7cf19632632f96e6620")
    assert request.headers["X-Twilio-Signature"] == "a9nBmqA0ju/hNViExpshrM61xv4="


def test_svix_matches_the_docs_vector():
    request = svix(SVIX_BODY, SVIX_SECRET, msg_id=SVIX_ID, timestamp=SVIX_TS)
    assert request.headers == {
        "svix-id": SVIX_ID,
        "svix-timestamp": str(SVIX_TS),
        "svix-signature": SVIX_SIGNATURE,
    }


def test_svix_standard_webhooks_prefix():
    request = svix(SVIX_BODY, SVIX_SECRET, msg_id=SVIX_ID, timestamp=SVIX_TS, prefix="webhook")
    assert request.headers["webhook-signature"] == SVIX_SIGNATURE
    assert not any(name.startswith("svix-") for name in request.headers)


def test_stripe_follows_the_documented_scheme():
    body, secret, ts = b'{"id": "evt_1"}', "whsec_test_secret", 1700000000
    header = stripe(body, secret, timestamp=ts).headers["Stripe-Signature"]
    expected = hmac.new(secret.encode(), b"1700000000." + body, hashlib.sha256).hexdigest()
    assert header == f"t=1700000000,v1={expected}"


def test_shopify_is_base64_of_the_raw_digest():
    body, secret = b'{"id": 1}', "hush"
    header = shopify(body, secret).headers["X-Shopify-Hmac-Sha256"]
    assert base64.b64decode(header) == hmac.new(secret.encode(), body, hashlib.sha256).digest()


def test_generic_is_hex_of_the_body():
    body, secret = b"payload", "s3cret"
    assert (
        generic(body, secret).headers["X-Signature"]
        == hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    )


def test_changing_one_byte_changes_every_signature():
    secret = "k"
    assert github(b"a", secret).headers != github(b"b", secret).headers
    assert generic(b"a", secret).headers != generic(b"b", secret).headers
    assert (
        shopify(b"a", secret).headers["X-Shopify-Hmac-Sha256"]
        != shopify(b"b", secret).headers["X-Shopify-Hmac-Sha256"]
    )


def test_default_timestamp_is_current():
    import time

    before = int(time.time())
    ts = int(slack(b"x", "k").headers["X-Slack-Request-Timestamp"])
    assert before <= ts <= int(time.time())
