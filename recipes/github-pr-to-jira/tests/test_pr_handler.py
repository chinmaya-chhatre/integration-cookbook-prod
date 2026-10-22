"""The handler's decisions, against the repository's Jira mock."""

import base64
import json
import threading

import pytest

from common import fixtures


def event(**kwargs) -> bytes:
    return fixtures.github_pull_request(**kwargs).body


def tweak(body: bytes, **changes) -> bytes:
    """The same event with some top-level keys of the pull request replaced."""
    document = json.loads(body)
    document["pull_request"].update(changes)
    return json.dumps(document).encode()


# --------------------------------------------------------------------------------- creating


def test_an_opened_pull_request_gets_one_issue(handler, jira):
    status, answer = handler.handle(event(action="opened", number=42, title="Fix retry backoff"))
    assert (status, answer) == (200, {"result": "created", "issue": "PROJ-1"})
    fields = jira.issues["PROJ-1"]["fields"]
    assert fields["summary"] == "Review PR #42: Fix retry backoff"
    assert fields["project"] == {"key": "PROJ"} and fields["issuetype"] == {"name": "Task"}
    assert fields["labels"] == ["github-pr-1000042"]
    assert jira.status_of("PROJ-1") == "To Do"


def test_the_issue_text_links_back_to_the_pull_request(handler, jira):
    handler.handle(event(action="opened", number=42, repo="acme/billing-service"))
    paragraphs = jira.issues["PROJ-1"]["fields"]["description"]["content"]
    text = [p["content"][0]["text"] for p in paragraphs]
    assert text == [
        "Pull request: https://github.com/acme/billing-service/pull/42",
        "Repository: acme/billing-service",
        "Author: octocat",
    ]


def test_the_handler_signs_in_with_the_configured_account(handler, jira):
    handler.handle(event(action="opened"))
    assert jira.requests[0].headers["authorization"].startswith("Basic ")
    token = jira.requests[0].headers["authorization"].removeprefix("Basic ")
    assert base64.b64decode(token) == b"demo@example.com:demo-token"


def test_a_long_title_is_cut_to_what_jira_accepts(app, handler, jira):
    handler.handle(event(action="opened", title="x" * 400))
    summary = jira.issues["PROJ-1"]["fields"]["summary"]
    assert len(summary) == app.SUMMARY_LIMIT and summary.endswith("…")


# --------------------------------------------------------------------------------- idempotency


def test_a_repeated_opened_event_changes_nothing(handler, jira):
    handler.handle(event(action="opened", number=42))
    status, answer = handler.handle(event(action="opened", number=42))
    assert (status, answer["result"], answer["issue"]) == (200, "already_linked", "PROJ-1")
    assert len(jira.issues) == 1 and len(jira.handled()) == 1  # Jira was not even asked again


def test_different_pull_requests_get_different_issues(handler, jira):
    handler.handle(event(action="opened", number=1))
    handler.handle(event(action="opened", number=2))
    assert sorted(jira.issues) == ["PROJ-1", "PROJ-2"]


def test_the_mapping_survives_a_restart(app, settings, jira):
    first = app.Handler(settings, app.PullRequests(settings.db_path))
    first.handle(event(action="opened", number=42))
    second = app.Handler(settings, app.PullRequests(settings.db_path))  # a new process, same database
    assert second.handle(event(action="opened", number=42))[1]["result"] == "already_linked"
    assert len(jira.issues) == 1


def test_simultaneous_deliveries_create_exactly_one_issue(app, settings, jira):
    handlers = [app.Handler(settings, app.PullRequests(settings.db_path)) for _ in range(8)]
    answers: list[tuple[int, dict]] = []

    def deliver(h):
        answers.append(h.handle(event(action="opened", number=42)))

    threads = [threading.Thread(target=deliver, args=(h,)) for h in handlers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(jira.issues) == 1
    assert {status for status, _ in answers} <= {200, 503}  # the losers are told to come back
    assert [a["result"] for s, a in answers if s == 200].count("created") == 1


# --------------------------------------------------------------------------------- moving


def test_a_merged_pull_request_moves_its_issue_to_done(handler, jira):
    handler.handle(event(action="opened", number=42))
    status, answer = handler.handle(event(action="closed", number=42, merged=True))
    assert (status, answer["result"]) == (200, "moved")
    assert jira.status_of("PROJ-1") == "Done"


def test_a_merge_for_an_unseen_pull_request_still_ends_done(handler, jira):
    status, answer = handler.handle(event(action="closed", number=42, merged=True))
    assert (status, answer["result"]) == (200, "created")
    assert list(jira.issues) == ["PROJ-1"] and jira.status_of("PROJ-1") == "Done"


def test_a_late_opened_event_never_undoes_a_merge(handler, jira):
    handler.handle(event(action="opened", number=42))
    handler.handle(event(action="closed", number=42, merged=True))
    status, answer = handler.handle(event(action="opened", number=42))  # a retry from the past
    assert (status, answer["result"]) == (200, "already_linked")
    assert jira.status_of("PROJ-1") == "Done"


def test_a_repeated_merge_does_not_move_it_again(handler, jira):
    handler.handle(event(action="closed", number=42, merged=True))
    transitions = len(jira.handled())
    assert handler.handle(event(action="closed", number=42, merged=True))[1]["result"] == "already_linked"
    assert len(jira.handled()) == transitions


def test_reopening_moves_a_done_issue_back(handler, jira):
    handler.handle(event(action="closed", number=42, merged=True))
    assert handler.handle(event(action="reopened", number=42))[1]["result"] == "moved"
    assert jira.status_of("PROJ-1") == "To Do" and len(jira.issues) == 1


def test_reopening_a_pull_request_with_no_issue_creates_one(handler, jira):
    assert handler.handle(event(action="reopened", number=42))[1]["result"] == "created"


# --------------------------------------------------------------------------------- ignoring


@pytest.mark.parametrize("action", ["edited", "synchronize", "labeled", "review_requested", "assigned"])
def test_other_actions_are_a_quiet_success(handler, jira, action):
    status, answer = handler.handle(event(action=action))
    assert (status, answer["result"]) == (200, "skipped") and jira.issues == {}


def test_closing_without_merging_changes_nothing(handler, jira):
    handler.handle(event(action="opened", number=42))
    status, answer = handler.handle(event(action="closed", number=42, merged=False))
    assert (status, answer["result"]) == (200, "skipped") and "not merged" in answer["reason"]
    assert jira.status_of("PROJ-1") == "To Do"


@pytest.mark.parametrize(
    "body", [b"not json", b"{}", b'{"action": "opened"}', b'{"action":"opened","pull_request":5}', b"[]"]
)
def test_garbage_is_a_400_not_a_retry(handler, jira, body):
    assert handler.handle(body) == (400, {"result": "unreadable"}) and jira.requests == []


def test_a_missing_author_or_repository_still_works(handler, jira):
    document = json.loads(event(action="opened"))
    del document["repository"]
    document["pull_request"]["user"] = None
    assert handler.handle(json.dumps(document).encode())[0] == 200
    assert "Author: unknown" in json.dumps(jira.issues["PROJ-1"]["fields"]["description"])


# --------------------------------------------------------------------------------- Jira failing


def test_a_jira_failure_is_a_502_and_the_retry_creates_the_issue(handler, jira):
    jira.fail_next(1, status=500)
    assert handler.handle(event(action="opened", number=42)) == (502, {"result": "jira_failed"})
    assert jira.issues == {}
    assert (
        handler.handle(event(action="opened", number=42))[1]["result"] == "created"
    )  # the claim was released
    assert len(jira.issues) == 1


def test_a_jira_rate_limit_is_a_502_too(handler, jira):
    jira.rate_limit_next(1, retry_after=1)
    assert handler.handle(event(action="opened"))[0] == 502


def test_a_jira_that_refuses_the_issue_is_a_502(handler, jira):
    jira.fail_next(1, status=400)  # e.g. an issue type the project does not have
    assert handler.handle(event(action="opened"))[0] == 502


def test_a_failed_move_keeps_the_issue_and_the_retry_finishes_the_job(handler, jira):
    handler.handle(event(action="opened", number=42))
    jira.fail_next(1, status=503)
    assert handler.handle(event(action="closed", number=42, merged=True))[0] == 502
    assert jira.status_of("PROJ-1") == "To Do"
    assert handler.handle(event(action="closed", number=42, merged=True))[1]["result"] == "moved"
    assert jira.status_of("PROJ-1") == "Done"


def test_a_failed_move_after_creating_does_not_create_a_second_issue(handler, jira):
    jira.fail_next(0)  # nothing scripted; the create succeeds, the transition fails below
    original = jira.handle

    def flaky(method, path, headers, body):
        if path.endswith("/transitions") and not getattr(flaky, "failed", False):
            flaky.failed = True
            from common.mocks import Reply

            return Reply(500, {"errorMessages": ["boom"]})
        return original(method, path, headers, body)

    jira.handle = flaky
    assert handler.handle(event(action="closed", number=42, merged=True))[0] == 502
    assert handler.handle(event(action="closed", number=42, merged=True))[1]["result"] == "moved"
    assert len(jira.issues) == 1 and jira.status_of("PROJ-1") == "Done"


def test_an_unreachable_jira_is_a_502(app, settings, tmp_path):
    down = app.Settings(**{**settings.__dict__, "base_url": "http://127.0.0.1:9"})
    handler = app.Handler(down, app.PullRequests(down.db_path))
    assert handler.handle(event(action="opened"))[0] == 502


# --------------------------------------------------------------------------------- the claim


def test_a_claim_held_by_a_live_delivery_asks_for_a_retry(app, settings, jira):
    store = app.PullRequests(settings.db_path)
    assert store.claim(1_000_042)[0] == "claimed"  # another delivery is mid-creation
    handler = app.Handler(settings, store)
    assert handler.handle(event(action="opened", number=42)) == (503, {"result": "busy"})
    assert jira.requests == []


def test_a_claim_nobody_finished_is_taken_over_after_a_while(app, settings, jira):
    clock = [1_000.0]
    store = app.PullRequests(settings.db_path, clock=lambda: clock[0])
    assert store.claim(1_000_042)[0] == "claimed"  # then the process died
    handler = app.Handler(settings, store)
    assert handler.handle(event(action="opened", number=42))[0] == 503
    clock[0] += app.CLAIM_SECONDS + 1
    assert handler.handle(event(action="opened", number=42))[1]["result"] == "created"
    assert len(jira.issues) == 1


def test_claim_outcomes(app, tmp_path):
    store = app.PullRequests(str(tmp_path / "c.db"))
    assert store.claim(1) == ("claimed", None, None)
    assert store.claim(1)[0] == "busy"
    store.record(1, "PROJ-9")
    assert store.claim(1) == ("linked", "PROJ-9", "To Do")
    store.set_state(1, "Done")
    assert store.get(1) == ("PROJ-9", "Done")
    store.release(1)  # a recorded issue is never released
    assert store.get(1) == ("PROJ-9", "Done")
    assert store.get(2) is None


# --------------------------------------------------------------------------------- settings


GOOD = {
    "JIRA_BASE_URL": "https://example.atlassian.net/",
    "JIRA_EMAIL": "bot@example.com",
    "JIRA_API_TOKEN": "t",
    "JIRA_PROJECT": "ENG",
    "JIRA_TRANSITION_TODO": "11",
    "JIRA_TRANSITION_DONE": "31",
}


def test_settings_are_read_from_the_environment(app):
    settings = app.Settings.from_env(GOOD)
    assert settings.base_url == "https://example.atlassian.net"  # no trailing slash
    assert (settings.issue_type, settings.db_path) == ("Task", "/data/pr-issues.db")


def test_every_mistake_is_reported_at_once(app):
    with pytest.raises(app.ConfigError) as error:
        app.Settings.from_env({"JIRA_BASE_URL": "ftp://x", "JIRA_TRANSITION_DONE": "done"})
    message = str(error.value)
    for expected in (
        "JIRA_EMAIL is not set",
        "JIRA_API_TOKEN is not set",
        "JIRA_PROJECT is not set",
        "JIRA_TRANSITION_TODO is not set",
        "http(s) URL",
        "JIRA_TRANSITION_DONE must be a whole number",
    ):
        assert expected in message


def test_the_container_refuses_to_start_on_bad_settings(app, monkeypatch):
    for name in GOOD:
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(SystemExit) as stop:
        app.main()
    assert str(stop.value).startswith("invalid configuration:") and "JIRA_BASE_URL is not set" in str(
        stop.value
    )


# --------------------------------------------------------------------------------- over HTTP


def test_the_server_answers_health_and_events(app, handler, jira):
    import urllib.request

    server = app.make_server(handler, 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        assert json.load(urllib.request.urlopen(base + "/healthz")) == {"status": "ok"}
        request = urllib.request.Request(
            base + "/github/pull-request", data=event(action="opened", number=42)
        )
        assert json.load(urllib.request.urlopen(request))["issue"] == "PROJ-1"
    finally:
        server.shutdown()
        server.server_close()
