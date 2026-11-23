# Signed SMS happy-path tests

Run from the repository root:

```sh
python -m pytest tests/test_twilio_happy_path.py tests/test_twilio_runtime.py
```

The fixture tests independently check the HMAC over the exact public HTTPS URL
and form parameters before parsing and mapping. They cover ordinary text,
Marathi and emoji, reserved form characters, blank bodies and international
phone numbers.

The runtime tests launch the real ingress and retry-worker process on loopback,
send signed HTTP form requests, and wait for persisted delivery completion.
A local vendor-neutral support API double checks the bearer credential, JSON
content type and stable account/message idempotency key before accepting a
ticket. Assertions cover the complete mapped payload, not only the HTTP status.

Successful delivery cases include ordinary and blank SMS bodies, Marathi,
emoji and reserved characters. Redelivery before and after an actual process
restart creates one target ticket. A different MessageSid from the same sender
creates a second independent ticket. The restart reuses the same SQLite file.

Existing negative runtime cases continue to verify wrong routes, unexpected
queries, invalid signatures, missing secrets and durable acceptance during a
target outage. HTTP tests unset proxy variables only for their local servers.
The complete test suite in CI also installs the pinned HookScope dependency;
recipe configuration tests validate its rules parser.

These checks validate the runtime and target HTTP contract. They do not certify
a support vendor or demonstrate a working Compose deployment. The recipe stays
marked `.wip` until deployment integration and customer handover are complete.
