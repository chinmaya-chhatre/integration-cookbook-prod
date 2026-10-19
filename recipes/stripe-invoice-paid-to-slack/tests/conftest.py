"""Shared fixtures: the handler on a real port, Slack as the repository's mock, HookScope in-process.

HookScope runs in-process, so its source must be importable: clone chinmaya-chhatre/hookscope,
run ``pip install -r requirements.txt`` there and put the clone on ``PYTHONPATH`` (the CI job for a recipe
does exactly that). The ``hookscope`` fixture skips, rather than fails, when it is missing.
"""

import importlib.util
import json
import threading
from pathlib import Path

import pytest

from common import fixtures
from common.mocks import SlackMock

RECIPE = Path(__file__).parent.parent


@pytest.fixture(scope="session")
def handler_module():
    spec = importlib.util.spec_from_file_location("invoice_handler", RECIPE / "handler" / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def slack():
    with SlackMock() as mock:
        yield mock


@pytest.fixture
def handler_url(handler_module, slack):
    thresholds = handler_module.parse_thresholds("USD:5000,EUR:4500,GBP:4000,JPY:750000")
    handler = handler_module.Handler(thresholds, slack.url + "/services/T0/B0/x", dedupe_memory=100)
    server = handler_module.make_server(handler, 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


@pytest.fixture
def hookscope(tmp_path, handler_url):
    """HookScope with the recipe's own rule, pointed at the handler running on this machine.

    Backoff sleeping is switched off so a retried delivery finishes at once; the policy itself
    (``attempts`` and the status codes that count as retryable) is the one in ``rules.json``.
    """
    pytest.importorskip("hookscope", reason="install HookScope to run the end-to-end tests")
    from fastapi.testclient import TestClient
    from hookscope.main import create_app
    from hookscope.rules import parse_rules
    from hookscope.store import EventStore

    data = json.loads((RECIPE / "rules.json").read_text())
    for rule in data["rules"]:
        rule["target_url"] = rule["target_url"].replace("http://handler:8080", handler_url)
    app = create_app(
        store=EventStore(str(tmp_path / "events.db")),
        secrets={"stripe": fixtures.SECRETS["stripe"]},
        rules=parse_rules(data),
        sleep=lambda seconds: None,
        admin_password=None,
    )
    return TestClient(app)
