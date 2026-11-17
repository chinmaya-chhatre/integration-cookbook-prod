import importlib.util
import sys
from pathlib import Path
from urllib.parse import urlencode

import pytest

PATH = Path(__file__).parents[1] / "recipes/twilio-sms-to-support-ticket/transform.py"
SPEC = importlib.util.spec_from_file_location("twilio_ticket_transform", PATH)
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)


def form(**overrides):
    values = {
        "AccountSid": "AC" + "a" * 32,
        "MessageSid": "SM" + "b" * 32,
        "From": "+12025550123",
        "To": "+12025550456",
        "Body": " नमस्कार + & % ",
    }
    values.update(overrides)
    return urlencode(values).encode()


def test_mapping_preserves_unicode_and_special_characters():
    message = module.parse_message(form())
    payload = module.ticket_payload(message)
    assert payload["description"] == " नमस्कार + & % "
    assert payload["contact"] == "+12025550123"
    assert payload["destination"] == "+12025550456"
    assert payload["external_reference"] == "twilio:AC" + "a" * 32 + ":SM" + "b" * 32


def test_blank_body_and_unknown_fields_are_allowed():
    assert module.parse_message(form(Body="", FutureField="unchanged")).body == ""


def test_identity_is_stable_and_account_scoped():
    first = module.parse_message(form())
    assert first.identity == module.parse_message(form()).identity
    assert first.identity != module.parse_message(form(AccountSid="AC" + "c" * 32)).identity


@pytest.mark.parametrize(
    "extra",
    [
        b"&Body=another",
        b"&%42ody=another",
        b"&Future=x&Future=y",
        b"&Bad=%ZZ",
        b"&Bad=%FF",
        b"&=value",
        b"&bare",
    ],
)
def test_ambiguous_or_malformed_form_is_rejected(extra):
    with pytest.raises(ValueError):
        module.parse_message(form() + extra)


@pytest.mark.parametrize(
    "overrides",
    [
        {"AccountSid": "invalid"},
        {"MessageSid": ""},
        {"From": ""},
        {"To": ""},
        {"NumMedia": "1"},
    ],
)
def test_invalid_identity_contact_or_media_is_rejected(overrides):
    with pytest.raises(ValueError):
        module.parse_message(form(**overrides))


@pytest.mark.parametrize("body", [b"", b"x" * 65537, b"Body=hi", b"Bad=\xff"])
def test_invalid_body_is_rejected(body):
    with pytest.raises(ValueError):
        module.parse_message(body)
