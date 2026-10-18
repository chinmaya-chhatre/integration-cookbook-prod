"""Signed invoice.paid events end to end: HookScope verifies and forwards, the handler decides.

HookScope runs in-process, so its source must be importable: clone chinmaya-chhatre/hookscope,
run ``pip install -r requirements.txt`` there and put the clone on ``PYTHONPATH`` (the CI job for a recipe
does exactly that). Without it these tests are skipped, not failed. The handler runs as a real HTTP
server on a free port and Slack is the repository's mock, so the only thing replaced is the network name
in ``rules.json``.
"""

import importlib.util
import json
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from common import fixtures
from common.mocks import SlackMock

pytest.importorskip("hookscope", reason="install HookScope to run the end-to-end tests")
from hookscope.main import create_app  # noqa: E402
from hookscope.rules import parse_rules  # noqa: E402
from hookscope.store import EventStore  # noqa: E402

RECIPE = Path(__file__).parent.parent
_spec = importlib.util.spec_from_file_location("invoice_handler", RECIPE / "handler" / "app.py")
app = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(app)

THRESHOLDS = app.parse_thresholds("USD:5000,EUR:4500,GBP:4000,JPY:750000")


@pytest.fixture
def slack():
    with SlackMock() as mock:
        yield mock


@pytest.fixture
def handler_url(slack):
    handler = app.Handler(THRESHOLDS, slack.url + "/services/T0/B0/x", dedupe_memory=100)
    server = app.make_server(handler, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.fixture
def hookscope(tmp_path, handler_url):
    """HookScope with the recipe's own rule, pointed at the handler running on this machine."""
    data = json.loads((RECIPE / "rules.json").read_text())
    for rule in data["rules"]:
        rule["target_url"] = rule["target_url"].replace("http://handler:8080", handler_url)
    app_ = create_app(
        store=EventStore(str(tmp_path / "events.db")),
        secrets={"stripe": fixtures.SECRETS["stripe"]},
        rules=parse_rules(data),
        admin_password=None,
    )
    return TestClient(app_)


def send(client: TestClient, **kwargs):
    request = fixtures.stripe_invoice_paid(**kwargs)
    return client.post("/hooks/stripe", content=request.body, headers=request.all_headers())


def forwards(client: TestClient) -> list[dict]:
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
