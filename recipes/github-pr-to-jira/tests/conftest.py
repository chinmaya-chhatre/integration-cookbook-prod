"""Fixtures: the handler's module, a mock Jira, and a handler wired to both with a temporary database."""

import importlib.util
import sys
from pathlib import Path

import pytest

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
