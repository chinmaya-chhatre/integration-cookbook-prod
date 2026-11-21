"""WSGI ingress: validate the fixed public URL and persist before acknowledging."""

import base64
import hashlib
import hmac
from urllib.parse import parse_qsl

from delivery import Outbox
from transform import MAX_BODY_BYTES, parse_message, ticket_payload


def application(public_url: str, auth_token: str, database: str):
    """Create an ingress callable. Mount only at the configured webhook path.

    Forwarding headers never change the signed URL. A separate worker delivers
    persisted tickets; the response does not depend on support API availability.
    """
    if not public_url.startswith("https://") or not auth_token:
        raise ValueError("an explicit HTTPS public URL and auth token are required")
    outbox = Outbox(database)

    def receive(environ, start_response):
        def respond(status, body):
            start_response(status, [("Content-Type", "application/xml"), ("Content-Length", str(len(body)))])
            return [body]

        if environ.get("REQUEST_METHOD") != "POST":
            return respond("405 Method Not Allowed", b"<Response/>")
        if environ.get("CONTENT_TYPE", "").split(";")[0].lower() != ("application/x-www-form-urlencoded"):
            return respond("415 Unsupported Media Type", b"<Response/>")
        try:
            length = int(environ.get("CONTENT_LENGTH", "0"))
            if not 0 < length <= MAX_BODY_BYTES:
                return respond("413 Payload Too Large", b"<Response/>")
            raw = environ["wsgi.input"].read(length)
            if len(raw) != length:
                raise ValueError("incomplete body")
            message = parse_message(raw)
            fields = dict(parse_qsl(raw.decode(), keep_blank_values=True, errors="strict"))
            signed = public_url + "".join(key + fields[key] for key in sorted(fields))
            expected = base64.b64encode(
                hmac.new(auth_token.encode(), signed.encode(), hashlib.sha1).digest()
            ).decode()
            supplied = environ.get("HTTP_X_TWILIO_SIGNATURE", "")
            if not hmac.compare_digest(expected.encode(), supplied.encode()):
                return respond("403 Forbidden", b"<Response/>")
        except (ValueError, UnicodeError):
            return respond("400 Bad Request", b"<Response/>")
        try:
            outbox.enqueue(message.identity, ticket_payload(message))
        except ValueError:
            return respond("409 Conflict", b"<Response/>")
        except Exception:
            # Never acknowledge failed persistence or disclose payloads and credentials.
            return respond("503 Service Unavailable", b"<Response/>")
        return respond("200 OK", b"<Response/>")

    return receive
