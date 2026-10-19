"""The handler's decisions, against the repository's Slack mock."""

import importlib.util
import json
from decimal import Decimal
from pathlib import Path

import pytest

from common import fixtures
from common.mocks import SlackMock

_path = Path(__file__).parent.parent / "handler" / "app.py"
_spec = importlib.util.spec_from_file_location("invoice_handler", _path)
app = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(app)

THRESHOLDS = app.parse_thresholds("USD:5000,EUR:4500,JPY:750000")


def body(**kwargs) -> bytes:
    return fixtures.stripe_invoice_paid(**kwargs).body


@pytest.fixture
def slack():
    with SlackMock() as mock:
        yield mock


@pytest.fixture
def handler(slack):
    return app.Handler(THRESHOLDS, slack.url + "/services/T0/B0/x", dedupe_memory=100)


def test_thresholds_parse_with_spaces_and_case():
    assert app.parse_thresholds(" usd : 5000 , JPY:750000 ") == {
        "USD": Decimal(5000),
        "JPY": Decimal(750000),
    }


@pytest.mark.parametrize("raw", ["", "USD", "USD:lots", "US:5", "USD:-1", "USD5000"])
def test_bad_thresholds_refuse_to_start(raw):
    with pytest.raises(app.ConfigError):
        app.parse_thresholds(raw)


@pytest.mark.parametrize(
    ("amount", "currency", "expected"),
    [(500_000, "usd", Decimal(5000)), (5000, "JPY", Decimal(5000)), (1_234, "kwd", Decimal("1.234"))],
)
def test_amounts_are_converted_to_major_units(amount, currency, expected):
    assert app.major_units(amount, currency) == expected


def test_threshold_is_inclusive_and_per_currency():
    assert app.is_large(500_000, "usd", THRESHOLDS)
    assert not app.is_large(499_999, "usd", THRESHOLDS)
    assert not app.is_large(750_000 - 1, "jpy", THRESHOLDS)
    assert app.is_large(750_000, "jpy", THRESHOLDS)
    assert not app.is_large(10**9, "chf", THRESHOLDS)  # no threshold configured


def test_message_is_readable():
    event = json.loads(body(amount_paid=125_000))
    assert app.format_message(event) == (
        "Invoice ACME-0042 paid: 1,250.00 USD\n"
        "Customer: billing@acme.example\n"
        "Open invoice: https://invoice.stripe.com/i/acct_test/in_1QxAcmeInvoice\n"
        "(evt_1QxAcmePaid, test mode)"
    )


def test_zero_decimal_amounts_show_no_decimals():
    event = json.loads(body(amount_paid=750_000, currency="jpy"))
    assert "750,000 JPY" in app.format_message(event)


def test_a_large_invoice_is_sent_once(handler, slack):
    assert handler.handle(body(amount_paid=500_000)) == (200, {"result": "sent"})
    assert handler.handle(body(amount_paid=500_000)) == (200, {"result": "skipped", "reason": "duplicate"})
    assert len(slack.messages) == 1 and "5,000.00 USD" in slack.messages[0]["text"]


def test_a_small_invoice_sends_nothing(handler, slack):
    status, answer = handler.handle(body(amount_paid=2_900))
    assert (status, answer["reason"]) == (200, "below threshold (29.00 USD)")
    assert slack.messages == []


def test_garbage_is_a_400_not_a_retry(handler):
    assert handler.handle(b"not json")[0] == 400
    assert handler.handle(b'{"id": "evt_1"}')[0] == 400


def test_the_oldest_event_ids_are_forgotten():
    seen = app.SeenEvents(2)
    for event_id in ("a", "b", "c"):
        seen.add(event_id)
    assert "a" not in seen and "b" in seen and "c" in seen


def test_every_skip_is_logged_with_its_reason(handler, caplog):
    with caplog.at_level("INFO", logger="invoice-handler"):
        handler.handle(body(amount_paid=2_900, event_id="evt_small"))
        handler.handle(body(amount_paid=749_999, currency="jpy", event_id="evt_yen"))
        handler.handle(body(amount_paid=10**9, currency="chf", event_id="evt_chf"))
        handler.handle(body(amount_paid=500_000, event_id="evt_big"))
        handler.handle(body(amount_paid=500_000, event_id="evt_big"))
    assert [r.getMessage() for r in caplog.records] == [
        "skipped evt_small: below threshold (29.00 USD)",
        "skipped evt_yen: below threshold (749,999 JPY)",
        "skipped evt_chf: no threshold for CHF",
        "sent evt_big",
        "skipped evt_big: duplicate",
    ]


def test_a_slack_failure_is_logged_with_the_event_id(handler, slack, caplog):
    slack.fail_next(1, status=500)
    with caplog.at_level("WARNING", logger="invoice-handler"):
        assert handler.handle(body(amount_paid=500_000))[0] == 502
    assert caplog.records[0].getMessage().startswith("slack unreachable or refused evt_1QxAcmePaid:")


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"INVOICE_THRESHOLDS": "USD:abc"}, "cannot read 'USD:abc'"),
        ({"INVOICE_THRESHOLDS": ""}, "INVOICE_THRESHOLDS is empty"),
        ({"SLACK_WEBHOOK_URL": "hooks.slack.com/x"}, "SLACK_WEBHOOK_URL must be an http(s) URL"),
        ({"DEDUPE_MEMORY": "lots"}, "DEDUPE_MEMORY must be a whole number, got 'lots'"),
    ],
)
def test_the_container_refuses_to_start_on_bad_settings(monkeypatch, env, message):
    good = {"INVOICE_THRESHOLDS": "USD:5000", "SLACK_WEBHOOK_URL": "http://slack.test/x"}
    for name, value in {**good, **env}.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(SystemExit) as stop:
        app.main()
    assert message in str(stop.value) and str(stop.value).startswith("invalid configuration:")
