"""TwiML edge: acknowledge only a confirmed durable handler acceptance."""

import io
import json
import os
import signal
import time
from http import HTTPStatus
from threading import Event
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener

from runtime import serve
from sender import NoRedirect
from transform import MAX_BODY_BYTES, parse_message


class HookScope:
    def __init__(self, public_url: str, base: str, token: str, handler_url: str):
        public, internal, handler = map(urlsplit, (public_url, base, handler_url))
        if (
            public.scheme != "https"
            or not public.hostname
            or public.username is not None
            or public.fragment
            or public.path != "/hooks/twilio"
            or internal.scheme not in {"http", "https"}
            or not internal.hostname
            or internal.username is not None
            or internal.path not in {"", "/"}
            or internal.query
            or internal.fragment
            or handler.scheme not in {"http", "https"}
            or not handler.hostname
            or handler.username is not None
            or handler.path != "/twilio/sms"
            or handler.query != public.query
            or handler.fragment
            or not token
            or "\r" in token
            or "\n" in token
        ):
            raise ValueError("invalid fixed edge, backend or handler configuration")
        self.public, self.base, self.token = public, base.rstrip("/"), token
        self.handler_url = handler_url
        self.opener = build_opener(NoRedirect())

    def read(self, request: Request):
        with self.opener.open(request, timeout=0.5) as response:
            raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("backend response too large")
        return json.loads(raw)

    def accept(self, raw: bytes, signature: str) -> int:
        path = self.public.path + ("?" + self.public.query if self.public.query else "")
        captured = False
        try:
            receipt = self.read(
                Request(
                    self.base + path,
                    data=raw,
                    method="POST",
                    headers={
                        "Content-Type": "application/x-www-form-urlencoded",
                        "X-Twilio-Signature": signature,
                        "Host": self.public.netloc,
                        "X-Forwarded-Proto": "https",
                    },
                )
            )
            event_id = receipt["id"]
            captured = True
            if (
                type(event_id) is not int
                or event_id < 1
                or receipt["verification"] != "valid"
                or "inbound-sms" not in receipt["forwarded_to"]
            ):
                return 503
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                rows = self.read(
                    Request(
                        f"{self.base}/api/events/{event_id}/forwards",
                        headers={"Authorization": f"Bearer {self.token}"},
                    )
                )
                for row in rows:
                    if row["rule"] != "inbound-sms" or row["target_url"] != self.handler_url:
                        continue
                    status = row["status_code"]
                    if isinstance(status, int) and 200 <= status < 300:
                        return 200
                    if status in {400, 403, 409, 413, 415}:
                        return status
                time.sleep(0.05)
        except HTTPError as error:
            code = error.code
            error.close()
            return 403 if not captured and code in {401, 403} else 503
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return 503


def application(public_url: str, backend: HookScope):
    public = urlsplit(public_url)

    def app(environ, start_response):
        def respond(code, body=b"<Response/>", content_type="application/xml"):
            start_response(
                f"{code} {HTTPStatus(code).phrase}",
                [("Content-Type", content_type), ("Content-Length", str(len(body)))],
            )
            return [body]

        if environ.get("PATH_INFO") == "/healthz" and environ.get("REQUEST_METHOD") == "GET":
            return respond(200, b"ok", "text/plain")
        if environ.get("PATH_INFO") != public.path or environ.get("QUERY_STRING", "") != public.query:
            return respond(404)
        if environ.get("REQUEST_METHOD") != "POST":
            return respond(405)
        if environ.get("CONTENT_TYPE", "").split(";")[0].lower() != "application/x-www-form-urlencoded":
            return respond(415)
        try:
            length = int(environ.get("CONTENT_LENGTH", "0"))
            if not 0 < length <= MAX_BODY_BYTES:
                return respond(413)
            raw = environ.get("wsgi.input", io.BytesIO()).read(length)
            if len(raw) != length:
                return respond(400)
            parse_message(raw)
        except (ValueError, UnicodeError):
            return respond(400)
        signature = environ.get("HTTP_X_TWILIO_SIGNATURE", "")
        if not signature:
            return respond(403)
        return respond(backend.accept(raw, signature))

    return app


def main():
    public = os.environ["TWILIO_PUBLIC_URL"]
    backend = HookScope(
        public,
        os.environ["HOOKSCOPE_INTERNAL_URL"],
        os.environ["SMS_ADMIN_TOKEN"],
        os.environ["SMS_HANDLER_URL"],
    )
    stop = Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    serve(application(public, backend), stop, "0.0.0.0", int(os.environ.get("PORT", "8080")))


if __name__ == "__main__":
    main()
