"""Bounded JSON ticket sender. Redirects never receive the support credential."""

import json
import math
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class TicketSender:
    def __init__(self, url: str, token: str, timeout: float = 5):
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
            raise ValueError("target must be an HTTP(S) URL without embedded credentials")
        if parsed.fragment or not token or "\n" in token or "\r" in token:
            raise ValueError("invalid target or token")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be positive and finite")
        self.url, self.token, self.timeout = url, token, timeout
        self.opener = build_opener(NoRedirect())

    def __call__(self, payload: dict, identity: str) -> int:
        if not identity or "\n" in identity or "\r" in identity:
            raise ValueError("invalid idempotency key")
        request = Request(
            self.url,
            data=json.dumps(payload, ensure_ascii=False).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.token}",
                "Idempotency-Key": identity,
            },
            method="POST",
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                return response.status
        except HTTPError as error:
            error.close()
            return error.code
