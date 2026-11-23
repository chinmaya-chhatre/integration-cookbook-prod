"""HTTP runtime for the signed SMS ingress and durable delivery worker."""

import os
import signal
from threading import Event, Thread
from urllib.parse import urlsplit
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from delivery import Outbox
from ingress import application
from sender import TicketSender
from worker import run


class QuietHandler(WSGIRequestHandler):
    def log_message(self, format, *args):
        """Do not emit request paths, signatures or personal data."""


class BoundedServer(WSGIServer):
    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(10)
        return connection, address


def routed_application(public_url: str, token: str, database: str):
    """Expose health separately and accept forms only at the internal SMS path."""
    receive = application(public_url, token, database)
    query = urlsplit(public_url).query

    def app(environ, start_response):
        path = environ.get("PATH_INFO", "")
        if path == "/healthz" and environ.get("REQUEST_METHOD") == "GET":
            start_response("200 OK", [("Content-Type", "text/plain"), ("Content-Length", "2")])
            return [b"ok"]
        if path != "/twilio/sms" or environ.get("QUERY_STRING", "") != query:
            start_response("404 Not Found", [("Content-Length", "0")])
            return [b""]
        return receive(environ, start_response)

    return app


def serve(app, stop: Event, host: str, port: int) -> None:
    """Bound idle waits and close the listener after shutdown is requested."""
    with make_server(host, port, app, server_class=BoundedServer, handler_class=QuietHandler) as server:
        server.timeout = 0.5
        while not stop.is_set():
            server.handle_request()


def main() -> None:
    """Validate configuration before listening, and stop both loops together."""
    database = os.environ["TICKET_STATE_DB"]
    app = routed_application(os.environ["TWILIO_PUBLIC_URL"], os.environ["TWILIO_AUTH_TOKEN"], database)
    sender = TicketSender(os.environ["SUPPORT_TICKET_URL"], os.environ["SUPPORT_TOKEN"])
    box = Outbox(database)
    stop = Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    def deliver():
        try:
            run(box, sender, stop)
        finally:
            # A failed worker must stop the HTTP process instead of accepting more work.
            stop.set()

    worker = Thread(target=deliver, name="ticket-worker")
    worker.start()
    try:
        serve(app, stop, "0.0.0.0", int(os.environ.get("PORT", "8080")))
    finally:
        stop.set()
        worker.join()


if __name__ == "__main__":
    main()
