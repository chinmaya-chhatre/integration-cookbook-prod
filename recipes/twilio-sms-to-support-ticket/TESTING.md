# Signed SMS happy-path tests

Run `pytest tests/test_twilio_happy_path.py` from the repository root.
The fixtures sign the public HTTPS URL and form parameters. An independent HMAC oracle checks the signature before parsing and mapping the original bytes into a ticket payload. Cases cover ordinary text, Marathi and emoji, reserved form characters, blank SMS bodies and international phone numbers.

These tests cover the signed fixture and pure transform boundary. HTTP ingress, durable delivery, retries and target outage behavior remain separate work. The recipe remains work in progress.
