"""Signed Twilio SMS fixtures through form parsing and ticket mapping."""

import base64
import hashlib
import hmac
import importlib.util
import sys
from pathlib import Path
from urllib.parse import parse_qsl

import pytest

from common.fixtures import SECRETS, twilio_inbound_sms

PATH = Path(__file__).parents[1] / "recipes/twilio-sms-to-support-ticket/transform.py"
SPEC = importlib.util.spec_from_file_location("twilio_happy_transform", PATH)
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)

URL = "https://support.example.com/hooks/twilio"


def expected_signature(request):
    """Independent HMAC oracle over the public URL and decoded sorted parameters."""
    fields = dict(parse_qsl(request.body.decode(), keep_blank_values=True))
    signed = URL + "".join(key + fields[key] for key in sorted(fields))
    digest = hmac.new(SECRETS["twilio"].encode(), signed.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


@pytest.mark.parametrize("body", ["My order never arrived", "नमस्कार 🌍", " + & = % / ? ", ""])
def test_signed_sms_maps_to_ticket_without_losing_text(body):
    request = twilio_inbound_sms(URL, body=body)
    assert request.all_headers()["Content-Type"] == "application/x-www-form-urlencoded"
    assert request.headers["X-Twilio-Signature"] == expected_signature(request)
    message = module.parse_message(request.body)
    assert module.ticket_payload(message) == {
        "external_reference": (
            "twilio:AC00000000000000000000000000000001:SM00000000000000000000000000000001"
        ),
        "summary": "Inbound SMS SM00000000000000000000000000000001",
        "description": body,
        "contact": "+16175550123",
        "destination": "+18005551212",
        "provider": "twilio",
    }


def test_custom_phone_numbers_survive_form_encoding():
    request = twilio_inbound_sms(URL, from_number="+442079460123", to_number="+912255501234")
    assert request.headers["X-Twilio-Signature"] == expected_signature(request)
    ticket = module.ticket_payload(module.parse_message(request.body))
    assert ticket["contact"] == "+442079460123"
    assert ticket["destination"] == "+912255501234"
