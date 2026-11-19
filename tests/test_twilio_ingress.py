"""Exercise the pinned HookScope Twilio boundary with signed form fixtures."""

import pytest
from fastapi.testclient import TestClient
from hookscope.main import create_app
from hookscope.store import EventStore

from common.fixtures import SECRETS, twilio_inbound_sms


@pytest.mark.parametrize("body", ["Help", "नमस्कार + & %", ""])
def test_signed_form_is_accepted_and_stored(tmp_path, body):
    app = create_app(
        store=EventStore(str(tmp_path / "events.db")),
        secrets={"twilio": SECRETS["twilio"]},
        admin_password=None,
    )
    with TestClient(app, base_url="https://public.example.com") as client:
        request = twilio_inbound_sms("https://public.example.com/hooks/twilio", body=body)
        response = client.post("/hooks/twilio", content=request.body, headers=request.all_headers())
        assert response.status_code == 202
        assert response.json()["verification"] == "valid"


@pytest.mark.parametrize("fault", ["url", "body", "signature"])
def test_invalid_delivery_never_verifies(tmp_path, fault):
    app = create_app(
        store=EventStore(str(tmp_path / "events.db")),
        secrets={"twilio": SECRETS["twilio"]},
        admin_password=None,
    )
    request = twilio_inbound_sms(
        "http://public.example.com/hooks/twilio"
        if fault == "url"
        else "https://public.example.com/hooks/twilio"
    )
    body = request.body + b"&Extra=changed" if fault == "body" else request.body
    headers = request.all_headers()
    if fault == "signature":
        headers["X-Twilio-Signature"] = "invalid"
    with TestClient(app, base_url="https://public.example.com") as client:
        response = client.post("/hooks/twilio", content=body, headers=headers)
        assert response.status_code == 401
