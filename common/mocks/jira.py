"""A Jira-style issue tracker: create issues, read them, move them through a workflow.

Shaped after the Jira Cloud REST API (``/rest/api/3/issue``) but only the parts the recipes
use. It is a stand-in for tests, not an emulation of Jira.
"""

from __future__ import annotations

import re
from typing import Any

from .base import MockServer, Reply

TRANSITIONS = {"11": "To Do", "21": "In Progress", "31": "Done"}


class JiraMock(MockServer):
    def __init__(self, project: str = "PROJ") -> None:
        super().__init__()
        self.project = project
        self.issues: dict[str, dict[str, Any]] = {}

    def handle(self, method: str, path: str, headers: dict[str, str], body: bytes) -> Reply:
        if method == "POST" and path == "/rest/api/3/issue":
            return self._create(self.parse(body))
        issue = re.fullmatch(r"/rest/api/3/issue/([A-Z]+-\d+)", path)
        if method == "GET" and issue:
            found = self.issues.get(issue.group(1))
            return Reply(200, found) if found else Reply(404, {"errorMessages": ["Issue does not exist"]})
        move = re.fullmatch(r"/rest/api/3/issue/([A-Z]+-\d+)/transitions", path)
        if method == "POST" and move:
            return self._transition(move.group(1), self.parse(body))
        return Reply(404, {"errorMessages": [f"no route for {method} {path}"]})

    def _create(self, payload: Any) -> Reply:
        fields = payload.get("fields") if isinstance(payload, dict) else None
        if not isinstance(fields, dict) or not fields.get("summary"):
            return Reply(400, {"errors": {"summary": "You must specify a summary of the issue."}})
        key = f"{self.project}-{len(self.issues) + 1}"
        self.issues[key] = {
            "id": str(10000 + len(self.issues)),
            "key": key,
            "fields": {**fields, "status": {"name": "To Do"}},
        }
        return Reply(201, {"id": self.issues[key]["id"], "key": key})

    def _transition(self, key: str, payload: Any) -> Reply:
        issue = self.issues.get(key)
        if issue is None:
            return Reply(404, {"errorMessages": ["Issue does not exist"]})
        transition_id = str((payload or {}).get("transition", {}).get("id", ""))
        if transition_id not in TRANSITIONS:
            return Reply(400, {"errorMessages": [f"Invalid transition id '{transition_id}'"]})
        issue["fields"]["status"] = {"name": TRANSITIONS[transition_id]}
        return Reply(204)

    def status_of(self, key: str) -> str:
        return self.issues[key]["fields"]["status"]["name"]
