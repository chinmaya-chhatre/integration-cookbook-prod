"""Persistent vendor-neutral support API stand-in for local SMS demonstrations."""

import hmac
import json
import os
import signal
import sqlite3
from contextlib import closing
from http import HTTPStatus
from threading import Event

from runtime import serve


def application(database: str, token: str):
    if not token:
        raise ValueError("support token required")
    with closing(sqlite3.connect(database)) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS tickets (identity TEXT PRIMARY KEY, payload TEXT NOT NULL)")

    def app(environ, start_response):
        def respond(code, data):
            raw = json.dumps(data).encode()
            start_response(
                f"{code} {HTTPStatus(code).phrase}",
                [("Content-Type", "application/json"), ("Content-Length", str(len(raw)))],
            )
            return [raw]

        path, method = environ.get("PATH_INFO"), environ.get("REQUEST_METHOD")
        if path == "/healthz" and method == "GET":
            return respond(200, {"status": "ok"})
        supplied = environ.get("HTTP_AUTHORIZATION", "")
        if not hmac.compare_digest(supplied.encode(), ("Bearer " + token).encode()):
            return respond(403, {"error": "forbidden"})
        if path == "/_mock/tickets" and method == "GET":
            with closing(sqlite3.connect(database)) as db:
                rows = db.execute("SELECT payload FROM tickets ORDER BY identity").fetchall()
            return respond(200, [json.loads(row[0]) for row in rows])
        if path != "/tickets" or method != "POST":
            return respond(404, {"error": "not found"})
        if environ.get("CONTENT_TYPE", "").split(";")[0] != "application/json":
            return respond(415, {"error": "JSON required"})
        try:
            length = int(environ.get("CONTENT_LENGTH", "0"))
            if not 0 < length <= 65536:
                return respond(413, {"error": "payload too large"})
            raw = environ["wsgi.input"].read(length)
            payload = json.loads(raw)
            keys = {"external_reference", "summary", "description", "contact", "destination", "provider"}
            if (
                len(raw) != length
                or not isinstance(payload, dict)
                or set(payload) != keys
                or any(not isinstance(payload[key], str) for key in keys)
                or payload["provider"] != "twilio"
                or not payload["external_reference"]
                or payload["external_reference"] != environ.get("HTTP_IDEMPOTENCY_KEY")
            ):
                return respond(400, {"error": "invalid ticket"})
        except (ValueError, UnicodeError):
            return respond(400, {"error": "invalid ticket"})
        encoded = json.dumps(payload, sort_keys=True)
        with closing(sqlite3.connect(database, timeout=5)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute(
                "SELECT payload FROM tickets WHERE identity = ?", (payload["external_reference"],)
            ).fetchone()
            if previous:
                return respond(
                    200 if previous[0] == encoded else 409,
                    {"external_reference": payload["external_reference"]},
                )
            db.execute("INSERT INTO tickets VALUES (?, ?)", (payload["external_reference"], encoded))
        return respond(201, {"external_reference": payload["external_reference"]})

    return app


def main():
    app = application(os.environ["SUPPORT_STATE_DB"], os.environ["SUPPORT_TOKEN"])
    stop = Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    serve(app, stop, "0.0.0.0", 9000)


if __name__ == "__main__":
    main()
