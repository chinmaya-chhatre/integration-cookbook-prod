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
    assert services["support"]["command"] == [
        "python",
        "-m",
        "common.mocks",
        "jira",
        "--port",
        "9000",
    ]
    assert "ticket-state" in config["volumes"]
    assert (RECIPE / ".wip").exists()
