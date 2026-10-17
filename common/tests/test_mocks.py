import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from common.mocks import CrmMock, JiraMock, SlackMock, WarehouseMock, validate_order


def call(mock, method, path, payload=None):
    """Return (status, parsed JSON or None, headers) without raising on HTTP errors."""
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(mock.url + path, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None, dict(response.headers)
    except urllib.error.HTTPError as error:
        raw = error.read()
        return error.code, json.loads(raw) if raw else None, dict(error.headers)


@pytest.fixture
def jira():
    with JiraMock() as mock:
        yield mock


@pytest.fixture
def crm():
    with CrmMock() as mock:
        yield mock


@pytest.fixture
def warehouse():
    with WarehouseMock() as mock:
        yield mock


# ----------------------------------------------------------------------------- Jira


def test_jira_creates_reads_and_transitions_an_issue(jira):
    status, body, _ = call(jira, "POST", "/rest/api/3/issue", {"fields": {"summary": "Fix retry backoff"}})
    assert (status, body["key"]) == (201, "PROJ-1")
    assert call(jira, "GET", "/rest/api/3/issue/PROJ-1")[1]["fields"]["summary"] == "Fix retry backoff"
    assert jira.status_of("PROJ-1") == "To Do"
    assert call(jira, "POST", "/rest/api/3/issue/PROJ-1/transitions", {"transition": {"id": "31"}})[0] == 204
    assert jira.status_of("PROJ-1") == "Done"


def test_jira_validates_input(jira):
    assert call(jira, "POST", "/rest/api/3/issue", {"fields": {}})[0] == 400
    assert call(jira, "POST", "/rest/api/3/issue", {"nope": 1})[0] == 400
    call(jira, "POST", "/rest/api/3/issue", {"fields": {"summary": "x"}})
    assert call(jira, "POST", "/rest/api/3/issue/PROJ-1/transitions", {"transition": {"id": "99"}})[0] == 400
    assert call(jira, "GET", "/rest/api/3/issue/PROJ-9")[0] == 404
    assert call(jira, "GET", "/elsewhere")[0] == 404


# ------------------------------------------------------------------------------ CRM


def test_crm_upsert_distinguishes_new_from_existing(crm):
    assert call(crm, "PUT", "/contacts/jane@example.com", {"name": "Jane"})[0] == 201
    status, body, _ = call(crm, "PUT", "/contacts/jane@example.com", {"plan": "pro"})
    assert status == 200
    assert body == {"name": "Jane", "plan": "pro", "id": "jane@example.com"}


def test_crm_rejects_stale_subscription_updates(crm):
    assert call(crm, "PUT", "/subscriptions/sub_1", {"version": 2, "state": "active"})[0] == 201
    status, body, _ = call(crm, "PUT", "/subscriptions/sub_1", {"version": 1, "state": "trialing"})
    assert (status, body["current_version"]) == (409, 2)
    assert call(crm, "GET", "/subscriptions/sub_1")[1]["state"] == "active"
    assert call(crm, "PUT", "/subscriptions/sub_1", {"version": 3, "state": "canceled"})[0] == 200
    assert call(crm, "PUT", "/subscriptions/sub_1", {"state": "x"})[0] == 400


# ------------------------------------------------------------------------ warehouse

ORDER = {"order_id": "1001", "lines": [{"sku": "TEE-BLK-M", "qty": 2}]}


def test_warehouse_accepts_a_valid_order_once(warehouse):
    assert call(warehouse, "POST", "/v1/orders", ORDER)[0] == 201
    assert call(warehouse, "GET", "/v1/orders/1001")[1]["status"] == "received"
    status, body, _ = call(warehouse, "POST", "/v1/orders", ORDER)
    assert (status, body["order_id"]) == (409, "1001")
    assert len(warehouse.orders) == 1


@pytest.mark.parametrize(
    ("payload", "problem"),
    [
        ({"lines": [{"sku": "A", "qty": 1}]}, "order_id is required"),
        ({"order_id": "1", "lines": []}, "lines must be a non-empty list"),
        ({"order_id": "1", "lines": [{"qty": 1}]}, "lines[0].sku is required"),
        ({"order_id": "1", "lines": [{"sku": "A", "qty": 0}]}, "lines[0].qty must be a positive integer"),
        ({"order_id": "1", "lines": [{"sku": "A", "qty": True}]}, "lines[0].qty must be a positive integer"),
        ([], "body must be a JSON object"),
    ],
)
def test_order_validation_names_the_problem(payload, problem):
    assert problem in validate_order(payload)


def test_warehouse_answers_422_with_the_problems(warehouse):
    status, body, _ = call(warehouse, "POST", "/v1/orders", {"order_id": "1", "lines": []})
    assert status == 422
    assert body["problems"] == ["lines must be a non-empty list"]


# ---------------------------------------------------------------- failure injection


def test_fail_next_answers_500_then_recovers(jira):
    jira.fail_next(2)
    payload = {"fields": {"summary": "x"}}
    assert [call(jira, "POST", "/rest/api/3/issue", payload)[0] for _ in range(3)] == [500, 500, 201]
    assert len(jira.issues) == 1  # the failed requests never reached the handler
    assert [r.faulted for r in jira.requests] == [True, True, False]
    assert len(jira.handled()) == 1


def test_custom_status_for_injected_failures(crm):
    crm.fail_next(1, status=503)
    assert call(crm, "GET", "/contacts/x")[0] == 503


def test_rate_limit_sets_retry_after(warehouse):
    warehouse.rate_limit_next(1, retry_after=7)
    status, _, headers = call(warehouse, "POST", "/v1/orders", ORDER)
    assert (status, headers["Retry-After"]) == (429, "7")
    assert call(warehouse, "POST", "/v1/orders", ORDER)[0] == 201


def test_slow_next_delays_then_handles_normally(jira):
    jira.slow_next(1, seconds=0.3)
    start = time.monotonic()
    status, _, _ = call(jira, "POST", "/rest/api/3/issue", {"fields": {"summary": "x"}})
    assert status == 201
    assert time.monotonic() - start >= 0.3
    start = time.monotonic()
    call(jira, "GET", "/rest/api/3/issue/PROJ-1")
    assert time.monotonic() - start < 0.3


def test_faults_are_consumed_in_the_order_queued(crm):
    crm.fail_next(1)
    crm.rate_limit_next(1)
    crm.fail_next(1, status=502)
    assert [call(crm, "GET", "/contacts/x")[0] for _ in range(4)] == [500, 429, 502, 404]


def test_clear_faults(crm):
    crm.fail_next(5)
    crm.clear_faults()
    assert call(crm, "GET", "/contacts/x")[0] == 404


def test_requests_are_recorded_with_headers_and_body(jira):
    call(jira, "POST", "/rest/api/3/issue", {"fields": {"summary": "hello"}})
    request = jira.requests[0]
    assert (request.method, request.path) == ("POST", "/rest/api/3/issue")
    assert request.json() == {"fields": {"summary": "hello"}}
    assert request.headers["content-type"] == "application/json"


def test_concurrent_requests_are_all_handled(warehouse):
    def post(n):
        call(warehouse, "POST", "/v1/orders", {"order_id": str(n), "lines": [{"sku": "A", "qty": 1}]})

    threads = [threading.Thread(target=post, args=(n,)) for n in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(warehouse.orders) == 20


def test_each_mock_gets_its_own_port():
    with JiraMock() as first, JiraMock() as second:
        assert first.url != second.url


# ------------------------------------------------------------------------------ Slack


def post_text(mock, path, payload):
    """POST JSON and return (status, body text); the Slack mock answers in plain text."""
    request = urllib.request.Request(mock.url + path, data=json.dumps(payload).encode(), method="POST")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()


@pytest.fixture
def slack():
    with SlackMock() as mock:
        yield mock


def test_slack_accepts_a_message_and_keeps_it(slack):
    assert post_text(slack, "/services/T000/B000/xyz", {"text": "hello"}) == (200, "ok")
    assert post_text(slack, "/services/T000/B000/xyz", {"blocks": [{"type": "section"}]}) == (200, "ok")
    assert [m.get("text") for m in slack.messages] == ["hello", None]
    assert call(slack, "GET", "/_mock/messages")[1] == slack.messages


def test_slack_rejects_what_slack_rejects(slack):
    assert post_text(slack, "/services/T/B/x", {"username": "bot"}) == (400, "no_text")
    assert post_text(slack, "/services/T/B/x", [1, 2]) == (400, "invalid_payload")
    assert post_text(slack, "/elsewhere", {"text": "x"}) == (404, "no_service")
    assert post_text(slack, "/services/", {"text": "x"}) == (404, "no_service")
    assert slack.messages == []


def test_slack_failure_injection_works_like_the_others(slack):
    slack.rate_limit_next(1, retry_after=3)
    status, _, headers = call(slack, "POST", "/services/T/B/x", {"text": "hi"})
    assert (status, headers["Retry-After"]) == (429, "3")
    assert post_text(slack, "/services/T/B/x", {"text": "hi"}) == (200, "ok")


def test_a_mock_can_be_served_from_the_command_line():
    import socket
    import subprocess
    import sys

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    process = subprocess.Popen(
        [sys.executable, "-m", "common.mocks", "slack", "--host", "127.0.0.1", "--port", str(port)],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert "slack mock listening" in process.stdout.readline()
        deadline = time.monotonic() + 5
        while True:
            try:
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/services/T/B/x", data=b'{"text": "cli"}', method="POST"
                )
                with urllib.request.urlopen(request, timeout=1) as response:
                    assert response.read() == b"ok"
                break
            except OSError:
                assert time.monotonic() < deadline, "the mock never started"
                time.sleep(0.05)
    finally:
        process.terminate()
        process.wait(timeout=5)
