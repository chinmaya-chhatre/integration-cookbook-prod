"""Shared signed-lifecycle integration stack for happy-path and failure tests."""

import importlib.util
import json
import sys
from pathlib import Path
from threading import Thread

import pytest

from common.fixtures import SECRETS
from common.mocks.crm import CrmMock

RECIPE = Path(__file__).parent
SPEC = importlib.util.spec_from_file_location("subscription_happy_handler", RECIPE / "handler" / "app.py")
handler_module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = handler_module
SPEC.loader.exec_module(handler_module)


@pytest.fixture
def lifecycle(tmp_path):
    pytest.importorskip("hookscope", reason="install HookScope to run the end-to-end tests")
    from fastapi.testclient import TestClient
    from hookscope.main import create_app
    from hookscope.rules import parse_rules
    from hookscope.store import EventStore

    rules = json.loads((RECIPE / "rules.json").read_text())
    with CrmMock() as crm:
        settings = handler_module.Settings(crm.url, "demo-token", str(tmp_path / "state.db"), 2)
        with handler_module.make_server(handler_module.Handler(settings), 0) as server:
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                rules["rules"][0]["target_url"] = f"http://127.0.0.1:{server.server_port}/stripe/subscription"
                app = create_app(
                    store=EventStore(str(tmp_path / "events.db")),
                    secrets={"stripe": SECRETS["stripe"]},
                    rules=parse_rules(rules),
                    sleep=lambda seconds: None,
                    admin_password=None,
                )
                with TestClient(app) as client:
                    yield client, crm
            finally:
                server.shutdown()
                thread.join()
