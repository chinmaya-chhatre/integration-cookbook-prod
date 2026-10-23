"""Signed pull_request events end to end: HookScope verifies and forwards, the handler decides, Jira changes.

The fixtures in ``conftest.py`` run HookScope in-process and the handler as a real HTTP server, with the
repository's Jira mock behind it; without HookScope importable these tests are skipped, not failed.
"""

import json

from common import fixtures, signing


def send(client, **kwargs):
    request = fixtures.github_pull_request(**kwargs)
    return client.post("/hooks/github", content=request.body, headers=request.all_headers())


def forwards(client, event_id=1) -> list[dict]:
    return client.get(f"/api/events/{event_id}/forwards").json()


def test_an_opened_pull_request_creates_an_issue(hookscope, jira):
    response = send(hookscope, action="opened", number=42, title="Fix retry backoff")
    assert response.status_code == 202 and response.json()["verification"] == "valid"
    assert response.json()["forwarded_to"] == ["pull-request"]
    assert list(jira.issues) == ["PROJ-1"]
    assert jira.issues["PROJ-1"]["fields"]["summary"] == "Review PR #42: Fix retry backoff"
    assert jira.status_of("PROJ-1") == "To Do"
    assert [(f["rule"], f["status_code"]) for f in forwards(hookscope)] == [("pull-request", 200)]


def test_the_issue_links_back_to_the_pull_request(hookscope, jira):
    send(hookscope, action="opened", number=42, repo="acme/billing-service")
    text = json.dumps(jira.issues["PROJ-1"]["fields"]["description"])
    assert "https://github.com/acme/billing-service/pull/42" in text and "octocat" in text


def test_merging_moves_the_issue_to_done(hookscope, jira):
    send(hookscope, action="opened", number=42)
    send(hookscope, action="closed", number=42, merged=True)
    assert list(jira.issues) == ["PROJ-1"] and jira.status_of("PROJ-1") == "Done"
    assert [f["status_code"] for f in forwards(hookscope, 2)] == [200]


def test_each_pull_request_gets_its_own_issue(hookscope, jira):
    send(hookscope, action="opened", number=1, title="First")
    send(hookscope, action="opened", number=2, title="Second")
    assert [i["fields"]["summary"] for i in jira.issues.values()] == [
        "Review PR #1: First",
        "Review PR #2: Second",
    ]


def test_reopening_a_merged_pull_request_puts_its_issue_back_to_do(hookscope, jira):
    send(hookscope, action="opened", number=42)
    send(hookscope, action="closed", number=42, merged=True)
    send(hookscope, action="reopened", number=42)
    assert len(jira.issues) == 1 and jira.status_of("PROJ-1") == "To Do"


def test_closing_without_merging_is_a_quiet_success(hookscope, jira):
    send(hookscope, action="opened", number=42)
    response = send(hookscope, action="closed", number=42, merged=False)
    assert response.status_code == 202
    assert jira.status_of("PROJ-1") == "To Do"
    assert [f["status_code"] for f in forwards(hookscope, 2)] == [200]  # delivered, not retried


def test_actions_that_need_no_issue_change_are_acknowledged_and_ignored(hookscope, jira):
    for action in ("edited", "synchronize", "labeled", "review_requested"):
        assert send(hookscope, action=action, number=42).status_code == 202
    assert jira.issues == {} and jira.requests == []


def test_other_github_events_never_reach_the_handler(hookscope, jira):
    payload = json.dumps({"ref": "refs/heads/main", "commits": []}).encode()
    request = signing.github(payload, fixtures.SECRETS["github"], event="push")
    response = hookscope.post("/hooks/github", content=request.body, headers=request.all_headers())
    assert response.status_code == 202 and response.json()["forwarded_to"] == []
    assert jira.requests == [] and forwards(hookscope) == []


def test_the_stored_event_keeps_the_pull_request_for_debugging(hookscope):
    send(hookscope, action="opened", number=42)
    event = hookscope.get("/api/events/1").json()
    assert event["source"] == "github" and event["verification"] == "valid"
    assert json.loads(event["body"])["pull_request"]["number"] == 42
