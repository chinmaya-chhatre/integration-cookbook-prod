"""Twilio configuration must route forms without relying on a JSON event type."""

import json
from pathlib import Path

import pytest
import yaml
from hookscope.rules import parse_rules

ROOT = Path(__file__).resolve().parents[1]
RECIPE = ROOT / "recipes" / "twilio-sms-to-support-ticket"


def rule():
    return parse_rules(json.loads((RECIPE / "rules.json").read_text()))[0]


def test_twilio_form_routing_and_retry_policy():
    configured = rule()
    event = {
        "source": "twilio",
        "verification": "valid",
        "headers": {"Content-Type": "application/x-www-form-urlencoded"},
        "body": "MessageSid=SM123&Body=Hello+world&Empty=",
    }
    assert configured.matches(event)
    assert configured.format == "raw"
    assert configured.transform is None
    assert configured.target_url == "http://handler:8080/twilio/sms"
    assert configured.retry.attempts == 5
    assert [configured.retry.delay(n) for n in range(1, 7)] == [2, 4, 8, 16, 32, 60]


@pytest.mark.parametrize("verification", ["invalid", "unsigned"])
def test_twilio_rejected_signatures_are_not_routed(verification):
    assert not rule().matches({"source": "twilio", "verification": verification})


def test_other_providers_are_not_routed():
    assert not rule().matches({"source": "stripe", "verification": "valid"})


def test_twilio_stack_is_private_and_retains_ticket_state():
    config = yaml.safe_load((RECIPE / "docker-compose.yml").read_text())
    services = config["services"]
    assert not services["handler"].get("ports")
    assert not services["support"].get("ports")
    assert services["handler"]["volumes"] == ["ticket-state:/data"]
    assert services["hookscope"]["depends_on"]["handler"]["condition"] == "service_healthy"
    assert services["handler"]["depends_on"]["support"]["condition"] == "service_healthy"
    assert services["support"]["command"] == ["python", "support_mock.py"]
    assert services["support"]["volumes"] == ["support-state:/data"]
    assert services["ingress"]["command"] == ["python", "gateway.py"]
    assert services["ingress"]["ports"] == ["127.0.0.1:${SMS_PORT:-8081}:8080"]
    assert services["ingress"]["depends_on"]["hookscope"]["condition"] == "service_healthy"
    assert services["hookscope"]["environment"]["HOOKSCOPE_ADMIN_PASSWORD"] == "cookbook-sms-admin"
    assert "ticket-state" in config["volumes"]
    assert not (RECIPE / ".wip").exists()
    assert (RECIPE / "tests" / "test_twilio_runtime.py").is_file()


def test_twilio_is_in_the_completed_recipe_matrix():
    from scripts.list_recipes import recipes

    assert RECIPE.name in recipes(ROOT)
