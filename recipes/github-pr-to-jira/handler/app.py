"""GitHub ``pull_request`` to Jira: one issue per pull request, idempotent, moved forward only.

HookScope forwards the verified event here. The handler keeps a small database of which Jira issue
belongs to which pull request id, creates the issue on the first sight of a pull request, and moves it
to Done when the pull request is merged. It answers 2xx for anything deliberately ignored (so HookScope
does not retry it), 400 for a payload it cannot read, and 502 or 503 only when a retry can help: Jira
failed, or another delivery for the same pull request is still creating the issue.
Standard library only, so the image stays small.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import sqlite3
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

log = logging.getLogger("pr-handler")

TODO, DONE = "To Do", "Done"
SUMMARY_LIMIT = 255  # Jira refuses a longer summary
CLAIM_SECONDS = 60  # how long a half-finished creation blocks others before one may take over


class ConfigError(ValueError):
    """The environment is wrong; the handler refuses to start rather than guess."""


class JiraError(OSError):
    """Jira could not be reached, or refused the request."""


@dataclass(frozen=True)
class Settings:
    base_url: str
    email: str
    api_token: str
    project: str
    issue_type: str
    transition_todo: str
    transition_done: str
    db_path: str

    @classmethod
    def from_env(cls, env: dict[str, str] | os._Environ[str] = os.environ) -> Settings:
        """Read and check every setting at once, so one start reports all the mistakes."""
        problems: list[str] = []

        def need(name: str, default: str | None = None) -> str:
            value = env.get(name, default)
            if not value:
                problems.append(f"{name} is not set")
            return value or ""

        settings = cls(
            base_url=need("JIRA_BASE_URL").rstrip("/"),
            email=need("JIRA_EMAIL"),
            api_token=need("JIRA_API_TOKEN"),
            project=need("JIRA_PROJECT"),
            issue_type=need("JIRA_ISSUE_TYPE", "Task"),
            transition_todo=need("JIRA_TRANSITION_TODO"),
            transition_done=need("JIRA_TRANSITION_DONE"),
            db_path=need("PR_DB", "/data/pr-issues.db"),
        )
        if settings.base_url and not settings.base_url.startswith(("http://", "https://")):
            problems.append(f"JIRA_BASE_URL must be an http(s) URL, got {settings.base_url!r}")
        for name in ("transition_todo", "transition_done"):
            if getattr(settings, name) and not getattr(settings, name).isdigit():
                problems.append(
                    f"JIRA_{name.upper()} must be a whole number, got {getattr(settings, name)!r}"
                )
        if problems:
            raise ConfigError("; ".join(problems))
        return settings


# ----------------------------------------------------------------------------------- the mapping


class PullRequests:
    """Which Jira issue belongs to which pull request id, and the state the handler last put it in."""

    def __init__(self, path: str, clock: Callable[[], float] = time.time) -> None:
        self.path = path
        self.clock = clock
        with self._connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS prs (pr_id INTEGER PRIMARY KEY, issue_key TEXT,"
                " state TEXT NOT NULL, claimed_at REAL NOT NULL)"
            )

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)  # we manage transactions
        db.row_factory = sqlite3.Row
        return db

    def claim(self, pr_id: int, stale_after: float = CLAIM_SECONDS) -> tuple[str, str | None, str | None]:
        """Decide who creates the issue: ``linked`` (with key and state), ``claimed`` or ``busy``.

        Exactly one caller is told ``claimed`` for a pull request that has no issue yet; the others are
        told ``busy`` until the issue is recorded. A claim nobody finished is taken over after
        ``stale_after`` seconds, so a crash between asking Jira and recording the key cannot block a
        pull request forever.
        """
        now = self.clock()
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM prs WHERE pr_id = ?", (pr_id,)).fetchone()
            if row is None:
                db.execute("INSERT INTO prs VALUES (?, NULL, ?, ?)", (pr_id, TODO, now))
                outcome: tuple[str, str | None, str | None] = ("claimed", None, None)
            elif row["issue_key"]:
                outcome = ("linked", row["issue_key"], row["state"])
            elif now - row["claimed_at"] > stale_after:
                db.execute("UPDATE prs SET claimed_at = ? WHERE pr_id = ?", (now, pr_id))
                outcome = ("claimed", None, None)
            else:
                outcome = ("busy", None, None)
            db.execute("COMMIT")
        return outcome

    def record(self, pr_id: int, issue_key: str) -> None:
        with closing(self._connect()) as db:
            db.execute("UPDATE prs SET issue_key = ? WHERE pr_id = ?", (issue_key, pr_id))

    def release(self, pr_id: int) -> None:
        """Give a claim back after Jira failed, so the retry can claim it again."""
        with closing(self._connect()) as db:
            db.execute("DELETE FROM prs WHERE pr_id = ? AND issue_key IS NULL", (pr_id,))

    def set_state(self, pr_id: int, state: str) -> None:
        with closing(self._connect()) as db:
            db.execute("UPDATE prs SET state = ? WHERE pr_id = ?", (state, pr_id))

    def get(self, pr_id: int) -> tuple[str, str] | None:
        with closing(self._connect()) as db:
            row = db.execute("SELECT issue_key, state FROM prs WHERE pr_id = ?", (pr_id,)).fetchone()
        return (row["issue_key"], row["state"]) if row and row["issue_key"] else None


# ------------------------------------------------------------------------------------------- Jira


def _explain(exc: OSError) -> str:
    """The error as one log line; for an HTTP error, the start of Jira's own explanation of it."""
    detail = ""
    if isinstance(exc, urllib.error.HTTPError):
        try:
            detail = exc.read(300).decode("utf-8", errors="replace")
        except OSError:
            pass
    return f"{exc} {' '.join(detail.split())}".strip()


class Jira:
    """The two Jira calls the recipe needs: create an issue, move an issue."""

    def __init__(self, settings: Settings, timeout: float = 10.0) -> None:
        self.settings = settings
        self.timeout = timeout
        token = base64.b64encode(f"{settings.email}:{settings.api_token}".encode()).decode()
        self.headers = {"Authorization": f"Basic {token}", "Content-Type": "application/json"}

    def _post(self, path: str, payload: dict[str, Any]) -> Any:
        request = urllib.request.Request(
            self.settings.base_url + path, data=json.dumps(payload).encode(), headers=self.headers
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except OSError as exc:  # HTTPError and URLError are OSErrors
            raise JiraError(_explain(exc)) from exc
        return json.loads(raw) if raw else None

    def create_issue(self, summary: str, description: str, labels: list[str]) -> str:
        fields = {
            "project": {"key": self.settings.project},
            "issuetype": {"name": self.settings.issue_type},
            "summary": summary,
            "description": document(description),
            "labels": labels,
        }
        created = self._post("/rest/api/3/issue", {"fields": fields})
        if not isinstance(created, dict) or not created.get("key"):
            raise JiraError(f"Jira did not return an issue key: {created!r}")
        return str(created["key"])

    def transition(self, key: str, transition_id: str) -> None:
        self._post(f"/rest/api/3/issue/{key}/transitions", {"transition": {"id": transition_id}})


def document(text: str) -> dict[str, Any]:
    """Plain text as an Atlassian Document Format body, one paragraph per line."""
    paragraphs = [
        {"type": "paragraph", "content": [{"type": "text", "text": line}]}
        for line in text.splitlines()
        if line
    ]
    return {"type": "doc", "version": 1, "content": paragraphs}


# ------------------------------------------------------------------------------------ the events


@dataclass(frozen=True)
class PullRequest:
    id: int
    number: int
    title: str
    url: str
    author: str
    repo: str
    merged: bool


def parse_event(body: bytes) -> tuple[str, PullRequest]:
    """``(action, pull request)`` from a webhook body; ``ValueError`` if it is not a pull request event."""
    try:
        event = json.loads(body)
        pr = event["pull_request"]
        return str(event["action"]), PullRequest(
            id=int(pr["id"]),
            number=int(pr["number"]),
            title=str(pr["title"]),
            url=str(pr["html_url"]),
            author=str((pr.get("user") or {}).get("login", "unknown")),
            repo=str((event.get("repository") or {}).get("full_name", "unknown")),
            merged=bool(pr.get("merged")),
        )
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ValueError(f"not a pull_request event: {exc!r}") from exc


def summary_of(pr: PullRequest) -> str:
    text = f"Review PR #{pr.number}: {pr.title}"
    return text if len(text) <= SUMMARY_LIMIT else text[: SUMMARY_LIMIT - 1] + "…"


def description_of(pr: PullRequest) -> str:
    return f"Pull request: {pr.url}\nRepository: {pr.repo}\nAuthor: {pr.author}"


def label_of(pr: PullRequest) -> str:
    """A label that finds the issue from the pull request id even if the mapping is lost."""
    return f"github-pr-{pr.id}"


def wanted_state(action: str, pr: PullRequest) -> str | None:
    """The state the issue should end in for this event, or ``None`` when the event needs no action."""
    if action in ("opened", "reopened"):
        return TODO
    if action == "closed" and pr.merged:
        return DONE
    return None


class Handler:
    """The decision for one forwarded event, separate from HTTP so it is easy to test."""

    def __init__(self, settings: Settings, pull_requests: PullRequests, jira: Jira | None = None) -> None:
        self.settings = settings
        self.pull_requests = pull_requests
        self.jira = jira or Jira(settings)

    def handle(self, body: bytes) -> tuple[int, dict[str, str]]:
        try:
            action, pr = parse_event(body)
        except ValueError as exc:
            # Retrying the same bytes cannot fix them, so this is a 400, not a 5xx.
            log.warning("unreadable event, answered 400: %s", exc)
            return 400, {"result": "unreadable"}
        want = wanted_state(action, pr)
        if want is None:
            return self.skip(
                pr, f"nothing to do for {action}" + (" (not merged)" if action == "closed" else "")
            )
        outcome, key, current = self.pull_requests.claim(pr.id)
        if outcome == "busy":
            log.info("PR %s: another delivery is creating the issue; ask for a retry", pr.id)
            return 503, {"result": "busy"}
        created = outcome == "claimed"
        try:
            if created:
                key, current = self.create_issue(pr), TODO
            moved = self.move(pr, str(key), want, current, back=action == "reopened")
        except JiraError as exc:
            log.warning("PR %s: Jira failed: %s", pr.id, exc)
            if created and self.pull_requests.get(pr.id) is None:
                self.pull_requests.release(pr.id)
            return 502, {"result": "jira_failed"}
        result = "created" if created else "moved" if moved else "already_linked"
        log.info("PR %s #%s: %s %s", pr.id, pr.number, result, key)
        return 200, {"result": result, "issue": key}

    def skip(self, pr: PullRequest, reason: str) -> tuple[int, dict[str, str]]:
        """A deliberate non-event: answered 200 so it is not retried, logged so it can be explained."""
        log.info("PR %s #%s skipped: %s", pr.id, pr.number, reason)
        return 200, {"result": "skipped", "reason": reason}

    def create_issue(self, pr: PullRequest) -> str:
        key = self.jira.create_issue(summary_of(pr), description_of(pr), [label_of(pr)])
        self.pull_requests.record(pr.id, key)
        return key

    def move(self, pr: PullRequest, key: str, want: str, current: str | None, back: bool = False) -> bool:
        """Move the issue forward to ``want``; a Done issue goes back to To Do only when ``back`` is set."""
        if want == current:
            return False
        if want == TODO and current == DONE and back:
            transition = self.settings.transition_todo  # a reopened pull request
        elif want == DONE:
            transition = self.settings.transition_done
        else:
            return False
        self.jira.transition(key, transition)
        self.pull_requests.set_state(pr.id, want)
        return True


# --------------------------------------------------------------------------------------- the server


def make_server(handler: Handler, port: int) -> ThreadingHTTPServer:
    class Request(BaseHTTPRequestHandler):
        def _reply(self, status: int, payload: dict[str, str]) -> None:
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802 - http.server's naming
            if self.path == "/healthz":
                self._reply(200, {"status": "ok"})
            else:
                self._reply(404, {"result": "not_found"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/github/pull-request":
                self._reply(404, {"result": "not_found"})
                return
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self._reply(*handler.handle(body))

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            log.debug("%s " + format, self.address_string(), *args)

    return ThreadingHTTPServer(("0.0.0.0", port), Request)  # noqa: S104 - inside a container


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s"
    )
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        raise SystemExit(f"invalid configuration: {exc}") from exc
    log.info("project %s on %s, mapping in %s", settings.project, settings.base_url, settings.db_path)
    make_server(Handler(settings, PullRequests(settings.db_path)), 8080).serve_forever()


if __name__ == "__main__":
    main()
