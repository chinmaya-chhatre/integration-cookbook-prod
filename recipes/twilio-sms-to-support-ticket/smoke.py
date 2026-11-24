"""Check the running demonstration stack using synthetic messages only."""

import base64
import hashlib
import hmac
import json
import os
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def main():
    public = os.environ["TWILIO_PUBLIC_URL"]
    fields = {
        "AccountSid": "AC00000000000000000000000000000001",
        "MessageSid": "SM00000000000000000000000000000001",
        "From": "+16175550123",
        "To": "+18005551212",
        "Body": "नमस्कार + &",
    }
    signed = public + "".join(key + fields[key] for key in sorted(fields))
    signature = base64.b64encode(
        hmac.new(os.environ["TWILIO_AUTH_TOKEN"].encode(), signed.encode(), hashlib.sha1).digest()
    ).decode()
    url = "http://127.0.0.1:8080/hooks/twilio"

    def send(value):
        request = Request(
            url,
            data=urlencode(fields).encode(),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Twilio-Signature": value,
            },
        )
        try:
            with urlopen(request, timeout=8) as response:
                return response.status, response.read()
        except HTTPError as error:
            with error:
                return error.code, error.read()

    assert send("invalid")[0] == 403
    assert send(signature) == (200, b"<Response/>")
    assert send(signature) == (200, b"<Response/>")
    target = Request(
        "http://support:9000/_mock/tickets",
        headers={"Authorization": "Bearer " + os.environ["SUPPORT_TOKEN"]},
    )
    for _ in range(100):
        with urlopen(target, timeout=2) as response:
            tickets = json.load(response)
        if tickets:
            break
        time.sleep(0.1)
    assert len(tickets) == 1
    assert tickets[0]["description"] == fields["Body"]
    assert tickets[0]["external_reference"] == ("twilio:" + fields["AccountSid"] + ":" + fields["MessageSid"])
    print("SMS stack smoke passed: signed delivery, invalid signature and duplicate checks")


if __name__ == "__main__":
    main()
