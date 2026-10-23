"""Fixtures: the handler's module, a mock Jira, a handler wired to both, and HookScope in front of it.

HookScope runs in-process, so its source must be importable: clone chinmaya-chhatre/hookscope, run
``pip install -r requirements.txt`` there and put the clone on ``PYTHONPATH`` (the CI job for a recipe does
exactly that). The ``hookscope`` fixture skips, rather than fails, when it is missing.
"""

import importlib.util
import json
import sys
import threading
from pathlib import Path

import pytest

from common import fixtures
from common.mocks import JiraMock

RECIPE = Path(__file__).parent.parent


@pytest.fixture(scope="session")
def app():
    spec = importlib.util.spec_from_file_location("pr_handler", RECIPE / "handler" / "app.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up here
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def jira():
    with JiraMock(project="PROJ") as mock:
        yield mock


@pytest.fixture
def settings(app, jira, tmp_path):
    return app.Settings(
        base_url=jira.url,
        email="demo@example.com",
        api_token="demo-token",
        project="PROJ",
        issue_type="Task",
        transition_todo="11",
        transition_done="31",
        db_path=str(tmp_path / "prs.db"),
    )


@pytest.fixture
def handler(app, settings):
    return app.Handler(settings, app.PullRequests(settings.db_path))


@pytest.fixture
def handler_url(app, handler):
    """The handler as a real HTTP server on a free port."""
    server = app.make_server(handler, 0)
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
    app_ = create_app(
        store=EventStore(str(tmp_path / "events.db")),
        secrets={"github": fixtures.SECRETS["github"]},
        rules=parse_rules(data),
        sleep=lambda seconds: None,
        admin_password=None,
    )
    return TestClient(app_)
