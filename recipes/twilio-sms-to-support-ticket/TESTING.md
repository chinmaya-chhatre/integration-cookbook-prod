# Signed SMS happy-path tests

Run from the repository root:

```sh
make test RECIPE=twilio-sms-to-support-ticket
python -m pytest tests/test_twilio_happy_path.py
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

The recipe-local suite runs in the completed-recipe CI matrix and in the full
suite, without duplicate test modules. It validates the runtime and target HTTP
contract. Full-suite tests also cover the parser, signed ingress, outbox,
scheduler, diagnostics and edge failure handling.

The mandatory Compose job separately builds and starts the pinned four-service
stack and executes its signed smoke command: invalid signature rejection, durable
acceptance and one target ticket after duplicate delivery. See the
[demonstration commands](README.md#run-the-synthetic-demonstration).
Process tests and Compose smoke provide different evidence; neither certifies
a support vendor or completes the [customer acceptance gates](CUSTOMER-PACK.md).

## Failure recovery through the running process

The runtime suite also exercises these HTTP failures using the production
ingress, outbox worker and sender, with no injected clock or direct delivery calls:

| Failure | Evidence checked |
| --- | --- |
| Target returns 429 or 500 | Pending work and its first attempt are committed; after ingress/worker restart the same payload and key deliver successfully on attempt two |
| Target returns 400 | Work remains in review after restart and provider redelivery; no automatic target retry occurs |
| Target accepts, then worker crashes before acknowledgement | The test holds the HTTP response, kills the worker process, verifies pending SQLite state, then restarts and confirms one target ticket across two identical delivery attempts |

The target double keeps accepted tickets across the client restart and honors
the stable idempotency key. The crash test demonstrates the response-loss
window and SQLite process-crash rollback, not power-loss durability or a
vendor's idempotency implementation. A real target must provide the same
stable-key contract or a reconciliation mechanism.

Earlier unit tests retain coverage for concurrent duplicates, conflicting
identities, out-of-order independent messages, five-attempt exhaustion and
persisted due times. These runtime checks add real sockets and process
boundaries. The separate Compose check proves the synthetic stack runs;
actual provider/target deployment readiness still requires customer acceptance.
