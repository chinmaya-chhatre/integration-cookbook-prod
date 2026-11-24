# Troubleshooting SMS ticket delivery

## Establish the boundary first

The [runtime](RUNTIME.md) serves signed ingress and runs the persistent retry
worker. It validates the configured public HTTPS URL, commits accepted messages
to SQLite, then returns empty TwiML. A 200 response means durable acceptance,
not completed ticket creation. The support target must honor the stable
idempotency key across response loss and process restarts.

The [Compose demonstration](README.md#run-the-synthetic-demonstration) now runs
the edge, pinned HookScope, handler and persistent support mock. Run its signed
smoke check, not just `make config`. The edge confirms handler acceptance before
returning TwiML; a HookScope capture receipt alone does not prove persistence.
Keep `.wip` until customer handover is complete. The checked-in credentials and
HTTPS URL are synthetic and must not be used with a live number.

## Diagnose by symptom

| Symptom | Likely cause | Check | Fix |
| --- | --- | --- | --- |
| Runtime exits before listening | Missing setting, invalid public URL, empty token or unwritable database directory | Check the variable names in Runtime and the container exit status; inspect secret configuration without printing its values | Supply all required settings and writable persistent storage; never bypass validation |
| Health endpoint is unreachable | Process stopped, listener inaccessible or worker failed | Request the internal `GET /healthz`; check process state and container health | Restore the process and storage, then verify health before accepting traffic |
| Signed POST returns 404 | Wrong internal path or query string | Compare the proxy route with `/twilio/sms` and the public URL query | Preserve query bytes and route the public path to the internal SMS path |
| Authentic POST returns 403 | Token, exact public URL or signed field value differs | Compare scheme, host, path and query with provider configuration using a synthetic signed fixture | Correct the fixed URL/token and preserve form bytes; forwarding headers never override the verifier |
| POST returns 400 | Invalid form encoding, duplicate fields, missing identity or media message | Run signed ingress tests with synthetic data | Preserve original bytes and all signed fields; handle media outside this SMS recipe |
| POST returns 409 | Existing account/message identity has different content | Reconcile the provider event and target external reference through an authorized interface | Investigate identity reuse; do not invent an identity to force acceptance |
| POST returns 503 | Durable enqueue failed | Check database mount, free disk, read/write permissions and lock contention | Restore persistence before allowing provider retries; do not acknowledge an uncommitted write |
| TwiML succeeds but work remains pending | Target outage, transport error, HTTP 408/425/429/5xx, scheduled delay or stopped worker | Sample aggregate health twice, check process health and redacted target status | Restore target/worker and allow scheduled retries using the same identity |
| Work moves to review | Permanent target failure or five transient attempts exhausted | Check target authentication/schema and the persisted state counts | Reconcile target acceptance before operator recovery; review entries are not retried automatically |
| Replay creates duplicate target tickets | Target ignores stable idempotency keys or lost its deduplication state | Compare target external references using synthetic data | Restore target idempotency/reconciliation before replay; restarting the local worker cannot fix it |
| HTTP redirect leads to review | Endpoint changed or a proxy redirects requests | Verify the configured final target URL without forwarding credentials | Configure the trusted final endpoint; the sender deliberately refuses redirects |
| Edge returns 503 although HookScope captured an event | No matching confirmed handler acceptance, wrong admin credential or backend outage | Check the rule target, handler health and authenticated forward history | Restore the handoff and keep the original identity on provider retry; capture alone is not acceptance |

Health is liveness, not a target-connectivity or backlog-readiness check. The
listener can be healthy while target delivery is failing. The runtime suppresses
HTTP access logs to avoid exposing paths, signatures and personal data.

## Read aggregate health safely

From the repository root, on a host with access to the actual persisted database:

```sh
python recipes/twilio-sms-to-support-ticket/diagnostics.py --database /data/tickets.sqlite3
```

For a separately started handler container, run the same command against its
mounted volume using `python /app/diagnostics.py --database /data/tickets.sqlite3`.
Do not assume the host's `/data` is the container's volume. The command uses
SQLite read-only mode, opens one consistent read transaction and does not
initialize a missing file. It never selects payloads, message identities, phone
numbers, tokens or signatures. A missing/unreadable database or incompatible
schema returns exit code 2 with a generic error, not a healthy zero backlog.

Example output from synthetic fixtures, not production telemetry:

```json
{"max_pending_attempts": 4, "pending_due": 2, "pending_scheduled": 1, "sampled_at_epoch": 100, "states": {"delivered": 1, "pending": 3, "review": 1}}
```

| Field | Meaning |
| --- | --- |
| states | Counts of pending, delivered and review records, including zero counts |
| pending_due | Pending records whose persisted due time has passed, plus new records with no retry checkpoint |
| pending_scheduled | Pending records waiting for a future due time |
| max_pending_attempts | Largest committed attempt count among pending records; zero before a first checkpoint |
| sampled_at_epoch | Unix seconds used to classify due times |

Due counts use wall-clock time, so host clock changes affect them. Counts do not
identify which target HTTP status caused a failure. Two samples can show backlog
growth or a worker that is not draining due work; they cannot prove that the
target has created a ticket. The command is an on-demand diagnostic, not an
exported metric, alerting service or retention mechanism.

A read may observe pending work while a sender is awaiting a target response.
SQLite serializes worker writes, including network attempts. Do not diagnose a
stalled worker from a single snapshot or start extra workers to bypass the lock.
Back up storage using SQLite's backup API or a consistent stopped-volume backup.
A live database may have journal files; copying only the main file is unsafe.

## Recover without changing identity

1. Stop uncontrolled/manual replays and preserve the database and target
   deduplication records. Do not run `make down` during recovery: it removes
   Compose volumes with `down -v`.
2. Restore the trusted final target URL, bearer credential and network access.
   Do not log secret values or SMS contents while investigating.
3. For pending work, restore the runtime against the same database. Its worker
   persists due times and attempts: failed attempts wait 2, 4, 8 and 16 seconds,
   and a fifth transient failure moves the record to review. Restart and
   provider redelivery do not reset this budget.
4. If the target may have accepted before a lost response or worker crash,
   reconcile its external reference. The next pending attempt reuses the same
   key. Target idempotency must survive this failure window.
5. Review entries need an explicit operator decision and target reconciliation.
   There is no supported command to reset review entries automatically. Do not
   delete rows, change state with SQL, or manufacture new message identities.
   `Outbox.deliver` bypasses scheduled timing for pending entries and is not a
   review-reset API; mixing it into unattended processing bypasses retry budgets.
6. Check aggregate counts again and confirm target records through an authorized
   interface. Verify that duplicate provider deliveries create no extra ticket
   and a genuinely new MessageSid creates another ticket.

The worker does not honor `Retry-After` or add jitter; its bounded exponential
policy is documented in [Scheduled delivery](WORKER.md). Account for target rate
limits before production use. Escalate growing due backlog, any review count,
missing persistent storage or an inability to reconcile target acceptance.
No structured delivery logs or exported metrics exist in this component; do not
promise dashboard alerts or quote proposed log lines as observed output.

## Verify the recovery procedure

Run these tests from the repository root:

```sh
python -m pytest tests/test_twilio_diagnostics.py tests/test_twilio_worker.py tests/test_twilio_runtime.py
```

They cover aggregate redaction, read-only access and failure reporting, persisted
retry timing and budget, HTTP 429/500 recovery across restart, permanent failures
remaining in review, and a process killed after target acceptance but before
local delivery commit. The target mock verifies key reuse without a second
ticket. Run the full required suite before release. These are component
contracts, not real-vendor certification. The required Compose smoke separately checks the
running edge-to-HookScope-to-handler-to-support path.
