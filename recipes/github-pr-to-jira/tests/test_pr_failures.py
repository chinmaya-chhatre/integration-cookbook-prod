"""What goes wrong, and what the recipe does about it: repeats, wrong order, Jira failing, forged requests.

Same setup as the happy-path tests (see ``conftest.py``). Jira's failures are scripted with the mock's
``fail_next`` and ``rate_limit_next``; HookScope's retries use the policy in this recipe's ``rules.json``
(5 attempts), with the backoff wait skipped so the tests run in a moment.
"""

import json
import threading
import urllib.error
import urllib.request

import pytest

from common import fixtures, signing

ATTEMPTS = 5  # rules.json: retry.attempts


def send(client, **kwargs):
    request = fixtures.github_pull_request(**kwargs)
    return client.post("/hooks/github", content=request.body, headers=request.all_headers())


def statuses(client, event_id=1) -> list[int | None]:
    return [f["status_code"] for f in client.get(f"/api/events/{event_id}/forwards").json()]


def resend(client, event_id=1):
    return client.post(f"/api/events/{event_id}/forwards/pull-request/resend")


# ------------------------------------------------------------------------------ repeats


def test_a_redelivered_event_creates_no_second_issue(hookscope, jira):
    first = send(hookscope, action="opened", number=42)
    again = send(hookscope, action="opened", number=42)  # GitHub sent the same event again
    assert first.status_code == again.status_code == 202
    assert list(jira.issues) == ["PROJ-1"]
    assert statuses(hookscope, 1) == [200] and statuses(hookscope, 2) == [200]
    assert len(jira.handled()) == 1  # Jira was asked once, ever


def test_eight_simultaneous_deliveries_create_exactly_one_issue(handler_url, jira):
    codes: list[int] = []
    body = fixtures.github_pull_request(action="opened", number=42).body

    def deliver() -> None:
        request = urllib.request.Request(handler_url + "/github/pull-request", data=body)
        try:
            codes.append(urllib.request.urlopen(request).status)
        except urllib.error.HTTPError as error:
            codes.append(error.code)

    threads = [threading.Thread(target=deliver) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(jira.issues) == 1
    assert set(codes) <= {200, 503} and codes.count(200) >= 1  # the losers are told to come back


# ------------------------------------------------------------------------------ out of order


def test_a_merge_that_arrives_first_still_ends_with_one_done_issue(hookscope, jira):
    send(hookscope, action="closed", number=42, merged=True)  # the "opened" was lost or is late
    assert list(jira.issues) == ["PROJ-1"] and jira.status_of("PROJ-1") == "Done"
    send(hookscope, action="opened", number=42)  # and now the late opened arrives
    assert list(jira.issues) == ["PROJ-1"] and jira.status_of("PROJ-1") == "Done"
    assert statuses(hookscope, 2) == [200]


def test_a_late_opened_never_undoes_a_merge(hookscope, jira):
    send(hookscope, action="opened", number=42)
    send(hookscope, action="closed", number=42, merged=True)
    send(hookscope, action="opened", number=42)  # a retry from the past
    assert jira.status_of("PROJ-1") == "Done" and len(jira.issues) == 1


# ------------------------------------------------------------------------------ Jira failing


@pytest.mark.parametrize("scripted", ["server_error", "rate_limited"])
def test_a_jira_hiccup_is_retried_and_creates_one_issue(hookscope, jira, scripted):
    if scripted == "server_error":
        jira.fail_next(1, status=500)
    else:
        jira.rate_limit_next(1, retry_after=1)
    assert send(hookscope, action="opened", number=42).status_code == 202
    # The handler turned Jira's failure into a 502, HookScope retried, and the second try got through.
    assert statuses(hookscope) == [502, 200]
    assert list(jira.issues) == ["PROJ-1"]
    assert hookscope.get("/api/dead-letters").json() == []


def test_a_failed_move_is_retried_and_the_issue_is_not_duplicated(hookscope, jira):
    send(hookscope, action="opened", number=42)
    jira.fail_next(1, status=503)  # the transition on merge fails once
    send(hookscope, action="closed", number=42, merged=True)
    assert statuses(hookscope, 2) == [502, 200]
    assert list(jira.issues) == ["PROJ-1"] and jira.status_of("PROJ-1") == "Done"


def test_a_long_outage_ends_in_a_dead_letter_and_loses_nothing(hookscope, jira):
    jira.fail_next(ATTEMPTS, status=503)
    send(hookscope, action="opened", number=42)
    assert statuses(hookscope) == [502] * ATTEMPTS
    assert jira.issues == {}
    [dead] = hookscope.get("/api/dead-letters").json()
    assert (dead["event_id"], dead["rule"], dead["resendable"]) == (1, "pull-request", True)


def test_resending_after_the_outage_creates_the_issue_once(hookscope, jira):
    jira.fail_next(ATTEMPTS, status=503)
    send(hookscope, action="opened", number=42)
    jira.clear_faults()  # Jira is back
    resent = resend(hookscope)
    assert resent.status_code == 200 and resent.json()["status_code"] == 200
    assert list(jira.issues) == ["PROJ-1"]
    assert hookscope.get("/api/dead-letters").json() == []
    resend(hookscope)  # a second click must not create a second issue
    assert list(jira.issues) == ["PROJ-1"]


def test_a_jira_that_keeps_refusing_ends_in_a_dead_letter(hookscope, jira):
    jira.fail_next(ATTEMPTS, status=400)  # e.g. the project has no issue type "Task"
    send(hookscope, action="opened", number=42)
    assert statuses(hookscope) == [502] * ATTEMPTS and jira.issues == {}
    assert len(hookscope.get("/api/dead-letters").json()) == 1


# ------------------------------------------------------------------------------ the claim


def test_a_creation_that_never_finished_is_recovered_after_the_claim_goes_stale(
    hookscope, jira, handler, app
):
    # Another delivery claimed the pull request and its process died before recording an issue.
    assert handler.pull_requests.claim(1_000_042)[0] == "claimed"
    send(hookscope, action="opened", number=42)
    assert statuses(hookscope) == [503] * ATTEMPTS and jira.issues == {}  # told to come back, five times
    assert len(hookscope.get("/api/dead-letters").json()) == 1
    handler.pull_requests.clock = lambda: 10**10  # a minute and more later
    assert resend(hookscope).json()["status_code"] == 200
    assert list(jira.issues) == ["PROJ-1"]
    assert hookscope.get("/api/dead-letters").json() == []


# ------------------------------------------------------------------------------ what is not retried


def test_a_body_the_handler_cannot_read_is_not_retried(hookscope, jira):
    request = signing.github(b"{}", fixtures.SECRETS["github"], event="pull_request")
    response = hookscope.post("/hooks/github", content=request.body, headers=request.all_headers())
    assert response.status_code == 202
    assert statuses(hookscope) == [400]  # one attempt: the same bytes would fail again
    assert jira.requests == []
    assert len(hookscope.get("/api/dead-letters").json()) == 1  # visible, not retried


def test_ignored_actions_are_not_retried_either(hookscope, jira):
    send(hookscope, action="synchronize", number=42)
    assert statuses(hookscope) == [200] and jira.requests == []


# ------------------------------------------------------------------------------ forged


def test_a_forged_request_is_stored_and_never_forwarded(hookscope, jira):
    request = fixtures.github_pull_request(action="opened", number=42)
    headers = {**request.all_headers(), "X-Hub-Signature-256": "sha256=" + "0" * 64}
    response = hookscope.post("/hooks/github", content=request.body, headers=headers)
    assert response.status_code == 401 and response.json()["verification"] == "invalid"
    assert response.json()["diagnosis"]["cause"] == "unexplained"
    assert jira.requests == [] and statuses(hookscope) == []
    assert hookscope.get("/api/events/1").json()["verification"] == "invalid"


def test_a_tampered_body_fails_the_signature(hookscope, jira):
    request = fixtures.github_pull_request(action="opened", number=42)
    tampered = json.loads(request.body)
    tampered["pull_request"]["title"] = "Delete everything"
    response = hookscope.post(
        "/hooks/github", content=json.dumps(tampered).encode(), headers=request.all_headers()
    )
    assert response.status_code == 401
    assert jira.requests == []
