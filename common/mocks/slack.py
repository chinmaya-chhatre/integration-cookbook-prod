"""A Slack incoming-webhook stand-in: it accepts messages and keeps them for inspection.

Slack's incoming webhooks answer ``200`` with the body ``ok``, and ``400`` with ``invalid_payload``
or ``no_text`` for a message it cannot use. This mock does the same, so a handler's error handling
runs against realistic answers. ``GET /_mock/messages`` (not part of Slack) lists what arrived,
which is handy when a compose stack is running; in tests read ``messages`` instead.
"""

from __future__ import annotations

from typing import Any

from .base import MockServer, Reply

WEBHOOK_PREFIX = "/services/"


class SlackMock(MockServer):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[dict[str, Any]] = []

    def handle(self, method: str, path: str, headers: dict[str, str], body: bytes) -> Reply:
        if method == "GET" and path == "/_mock/messages":
            return Reply(200, self.messages)
        if not path.startswith(WEBHOOK_PREFIX) or len(path) == len(WEBHOOK_PREFIX):
            return Reply(404, text="no_service")
        if method != "POST":
            return Reply(405, text="invalid_request_method")
        payload = self.parse(body)
        if not isinstance(payload, dict):
            return Reply(400, text="invalid_payload")
        if not any(payload.get(key) for key in ("text", "blocks", "attachments")):
            return Reply(400, text="no_text")
        self.messages.append(payload)
        return Reply(200, text="ok")
