# Troubleshooting SMS ticket delivery

The recipe remains unfinished. The inbound HTTP handler, automatic retry scheduler
and handler build context are not available. Do not connect a live Twilio number
or interpret a successful Compose configuration check as a runnable deployment.

| Symptom | Likely cause | Check | Fix |
| --- | --- | --- | --- |
| Compose cannot build the handler | Handler build context is unfinished | Run `make config RECIPE=twilio-sms-to-support-ticket` to distinguish configuration from build errors | Complete the inbound handler before starting this stack |
| An authentic SMS fails verification | Public URL differs from the URL used by the verifier | Compare scheme, host, path and query against the configured Twilio webhook URL | Preserve the external URL through a trusted boundary; never disable signature checks |
| Parsing rejects a message | Duplicate fields, malformed encoding, invalid identity or media payload | Run the transform and signed ingress tests with synthetic fixtures | Preserve original bytes; reject ambiguous parameters; route media outside this SMS recipe |
| Ticket stays pending after a target outage | Transport failure or HTTP 408, 425, 429 or 5xx | Inspect persisted state and target status without printing payloads | Restore the target, then have the worker call deliver with the original identity |
| Ticket requires review | Target returned a non-retryable status | Check target authentication, schema and redirect configuration | Correct configuration and reconcile the target before operator recovery |
| Replay creates a second target ticket | Target does not honor the stable idempotency key | Compare external references at the target using synthetic data | Implement target idempotency or reconciliation before replay |
| Identity reuse raises an error | Same identity was submitted with different content | Compare redacted metadata and the producer mapping | Preserve AccountSid/MessageSid identity; investigate the conflicting message |

## Inspect persisted state safely

Back up the SQLite volume before recovery. Query counts rather than SMS content:

```sql
SELECT state, COUNT(*) FROM tickets GROUP BY state;
```

Example diagnostic result, not production telemetry:

```text
delivered|12
pending|2
review|1
```

Pending work is retained across reopening the database. There is no automatic
retry timer in the current component. A worker must call `deliver(identity, sender)`
again. Delivered and review entries are not resent by that method. Do not delete
rows or manufacture identities to force retries. For review entries, reconcile
the external reference at the target before designing an explicit recovery path.

## Verify recovery

Run `python -m pytest tests/test_twilio_delivery.py tests/test_twilio_http_delivery.py`
from the repository root. These checks cover target outages, persisted replay,
response loss with stable keys, redirects and permanent failures. Run the full
suite before release. A passing mock test establishes the component contract,
not a guarantee that a real support vendor implements idempotency.

For response loss, the target may already have accepted the ticket. Keep the
same external reference and idempotency key after restart. Verify that replay
returns the existing ticket instead of creating another.

## Operational evidence

The current components do not emit structured logs or export metrics. Do not
assume dashboards or alerts exist. An operator can derive pending and review
counts from the query above. A future worker should report redacted outcomes,
for example `delivery state=pending target_status=429`, without recording tokens,
signatures, phone numbers or message bodies. Such a line is a proposed log
format, not output emitted by this implementation.

Escalate growing pending counts, any review count, missing persistent storage,
or an inability to reconcile target acceptance. Keep backups and access controls
on the database because it contains ticket descriptions.
