import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from common import fixtures, send


class Recorder(BaseHTTPRequestHandler):
    seen: list = []
    status = 202

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        Recorder.seen.append((self.path, dict(self.headers), body))
        payload = json.dumps({"id": 1, "verification": "valid"}).encode()
        self.send_response(Recorder.status)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    Recorder.seen, Recorder.status = [], 202
    httpd = HTTPServer(("127.0.0.1", 0), Recorder)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def test_coerce_converts_numbers_and_booleans():
    assert [send.coerce(v) for v in ("500000", "true", "False", "usd", "1.5")] == [
        500000,
        True,
        False,
        "usd",
        "1.5",
    ]


def test_build_passes_keyword_arguments_to_the_fixture():
    request = send.build("stripe_invoice_paid", ["amount_paid=500000", "currency=eur"])
    payload = json.loads(request.body)["data"]["object"]
    assert (payload["amount_paid"], payload["currency"]) == (500000, "eur")


def test_unknown_fixtures_and_bad_arguments_explain_themselves():
    with pytest.raises(SystemExit, match="unknown fixture 'nope'.*stripe_invoice_paid"):
        send.build("nope", [])
    with pytest.raises(SystemExit, match="expected name=value"):
        send.build("stripe_invoice_paid", ["amount_paid"])
    with pytest.raises(SystemExit, match="unknown fixture"):
        send.build("dumps", [])


def test_the_event_is_posted_to_the_providers_hook_with_its_signature(server, capsys):
    assert send.main(["stripe_invoice_paid", "amount_paid=700", "--url", server]) == 0
    [(path, headers, body)] = Recorder.seen
    assert path == "/hooks/stripe"
    assert headers["Stripe-Signature"].startswith("t=") and headers["Content-Type"] == "application/json"
    assert json.loads(body)["data"]["object"]["amount_paid"] == 700
    assert "-> 202" in capsys.readouterr().out


def test_every_provider_maps_to_its_own_path(server):
    for fixture, source in (
        ("github_pull_request", "github"),
        ("shopify_order_created", "shopify"),
        ("svix_event", "svix"),
    ):
        assert send.main([fixture, "--url", server]) == 0
        assert Recorder.seen[-1][0] == f"/hooks/{source}"


def test_a_rejection_is_shown_and_fails_the_command(server, capsys):
    Recorder.status = 401
    assert send.main(["generic_event", "--url", server]) == 1
    assert "-> 401" in capsys.readouterr().out


def test_signing_matches_the_fixture(server):
    send.main(["generic_event", "--url", server])
    _, headers, body = Recorder.seen[0]
    assert headers["X-Signature"] == fixtures.generic_event().headers["X-Signature"]
    assert body == fixtures.generic_event().body
