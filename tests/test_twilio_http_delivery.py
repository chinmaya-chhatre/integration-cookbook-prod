import importlib.util
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest

ROOT = Path(__file__).parents[1] / "recipes/twilio-sms-to-support-ticket"


def load(name):
    spec = importlib.util.spec_from_file_location("ticket_" + name, ROOT / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("failure", [429, 500, 302])
def test_real_http_failure_restart_and_replay(tmp_path, monkeypatch, failure):
    for name in ("ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        monkeypatch.delenv(name, raising=False)
    requests = []
    statuses = [failure, 201]

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            requests.append(
                (json.loads(body), self.headers["Idempotency-Key"], self.headers["Authorization"])
            )
            self.send_response(statuses.pop(0))
            self.send_header("Location", "/redirected")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        sender = load("sender").TicketSender(f"http://127.0.0.1:{server.server_port}/tickets", "demo-token")
        outbox = load("delivery").Outbox
        path = str(tmp_path / "tickets.db")
        box = outbox(path)
        payload = {"external_reference": "sms-1", "description": "नमस्कार"}
        box.enqueue("sms-1", payload)
        assert box.deliver("sms-1", sender) == ("review" if failure == 302 else "pending")
        if failure != 302:
            assert outbox(path).deliver("sms-1", sender) == "delivered"
            assert requests == [(payload, "sms-1", "Bearer demo-token")] * 2
        else:
            assert len(requests) == 1
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
