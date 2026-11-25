# SMS integration customer handover

Use this pack with the [recipe guide](README.md), [test evidence and commands](TESTING.md)
and [operations runbook](TROUBLESHOOTING.md). The runnable demonstration uses synthetic
messages and a vendor-neutral support mock. Customer acceptance gates remain unchecked
until their owners provide evidence from the actual environment.

## FAQ

**What does a successful webhook response prove?**
The TwiML edge returns 200 only after authenticated HookScope forward history confirms
durable handler acceptance. The ticket may still be pending. A raw HookScope capture
receipt is not that acknowledgement. Monitor target completion and review state separately.

**Do multiple messages become one conversation?**
No. Each account/message identity creates a separate ticket. Identical redelivery of
the same identity is deduplicated; changed content with that identity is a conflict.
Conversation grouping, media downloads and outbound replies need a separate design.

**Can we connect directly to our support platform?**
Only if it implements the [HTTP delivery contract](HTTP-DELIVERY.md): the documented
JSON payload, bearer authentication and stable idempotency key. Otherwise implement and
test an adapter. The mock is not Jira or another vendor's certified API.

**Is ticket creation exactly once?**
Attempts can repeat, including after a target accepted a request but its response was
lost. The outbox reuses its account-scoped key. The real target must durably deduplicate
that key and reconcile uncertain acceptance, including downstream side effects.
A successful happy-path test alone does not establish exactly-once behavior.

**Why do signatures fail behind a proxy?**
The verifier uses the exact configured public HTTPS URL and all signed form parameters.
Changes to host, scheme, path, query or decoded values change the signing input. Preserve
form bytes and query strings. The edge supplies fixed trusted URL headers to a private
HookScope backend; the handler independently checks the original signature. Never disable
verification to resolve mismatches. See [Ingress](INGRESS.md).

**Which settings must agree?**
Align the provider's external URL with edge and handler `TWILIO_PUBLIC_URL`, and the
provider token with HookScope and handler configuration. If the public URL has a query,
match it on `SMS_HANDLER_URL` and the rule target too. `SMS_ADMIN_TOKEN` authenticates
forward-history reads; `SUPPORT_TOKEN` authenticates the target. Checked-in values are
public synthetic examples, not credentials for a live deployment.

**What happens during an outage?**
Unconfirmed handler handoff returns 503; acceptance may finish later. Verify actual
provider redelivery/fallback settings rather than assuming 503 guarantees redelivery.
After acceptance, the worker persists its own five-attempt budget for transport errors,
408/425/429 and 5xx. Permanent failures and exhaustion enter review. Restart does not
reset attempts. Health endpoints report liveness, not successful ticket delivery.

**Can resending fix review entries?**
No automatic reset occurs, even when the same signed message is redelivered. There is
no supported review reset/requeue command. Reconcile target acceptance and agree an
authorized recovery procedure before go-live. Follow the runbook; do not delete state,
invent a new identity or edit retry counters to force a new delivery.

**Where does personal data remain?**
SMS text and phone numbers exist in captures, outbox records, support tickets and possibly
backups. Aggregate diagnostics omit payloads and identities and runtime access logs are
suppressed, but those measures do not delete stored data. Define access, encryption,
retention and coordinated deletion for every store. HookScope event retention does not
purge handler state, target tickets or backups.

**Can the demo run unchanged in production?**
No. It uses a synthetic URL, public demo credentials, loopback ports and a support mock.
A live deployment needs TLS, private backend networking, secret injection, durable storage,
monitoring and capacity tests. Serialized SQLite work and bounded edge polling are not a
demonstrated scaling SLA.

**How do we stop or roll back?**
Coordinate provider traffic, the edge, forwarding and the worker with a named owner.
Stopping traffic does not cancel accepted work or reverse existing tickets. Preserve all
three volumes and target deduplication. `make down` deletes volumes and is unsuitable for
incident recovery. Reconcile accepted/delivered work before reverting or resuming an old
processor. See the runbook's recovery boundaries.

## Discovery questions

| Area | Ask | Record before implementation |
| --- | --- | --- |
| Outcome | What support action should SMS cause, and who owns completion? | Approved mapping, acceptance criteria and escalation owner |
| Scope | Which accounts, numbers and message types? Are threads, media or replies required? | Account/number boundary and separate work for unsupported cases |
| Ingress | Who owns DNS, TLS and proxy routes? Does the external URL include a query? | Exact URL, byte-preserving route and private trust boundary |
| Credentials | Who supplies and rotates provider, admin and target credentials? | Secret references, owners and rotation rehearsal, without values |
| Target | Does the API accept this schema and a durable account/message key? | Adapter contract, authentication and acceptance evidence |
| Uncertain writes | What if the target commits but its response is lost? | Deduplication retention, reconciliation lookup and side-effect policy |
| Provider failures | Which retries, fallbacks and notifications are configured? | Observed behavior for 503, timeout and permanent rejection |
| Capacity | What bursts, sustained rates and maximum ticket delay are acceptable? | Measured capacity, timeout margin and backlog thresholds |
| Durability | Which failures must storage survive? Who restores every store? | Backup/restore evidence, recovery point and recovery time |
| Operations | Who handles overdue pending work, review and exhaustion? | Alert routing, response targets and authorized reconciliation |
| Privacy | Who may read phone numbers and text, and for how long? | Access, encryption, retention and deletion across all stores/backups |
| Cutover | Which existing processor handles these messages? | Bounded migration set, writer coordination, rollback and sign-off |

## Go-live checklist

Repository tests demonstrate the synthetic implementation. They do not complete these
customer gates. Record an owner, environment, evidence reference and result for each item
in a restricted acceptance record outside source control.

### Scope and configuration

- [ ] Approve account/number boundaries, mapping, one-ticket-per-message behavior and
      handling of blank text, Unicode, duplicates, conflicting identities and unsupported media.
- [ ] Validate the real target or adapter: schema, authentication, durable idempotency,
      downstream side effects and a reconciliation lookup for uncertain acceptance.
- [ ] Align the exact public HTTPS URL and query across provider, edge, HookScope and
      handler. Verify routes preserve form bytes and do not trust client forwarding headers.
- [ ] Replace every demo credential through secret injection. Protect administration,
      trusted-proxy backend networking, handler connectivity, target and state volumes.
- [ ] Record application/configuration versions and keep the tested HookScope dependency
      aligned between Compose, CI and deployment.
- [ ] Verify actual provider retry, fallback and failure-notification settings with authorized
      test traffic. Measure response timeout against the edge's bounded polling behavior.

### Delivery acceptance

- [ ] Run the [signed stack smoke](README.md#run-the-synthetic-demonstration) in an isolated
      demo, then equivalent authorized real-target cases. Check the complete ticket mapping.
- [ ] Prove invalid signatures, wrong URLs/queries and malformed forms cannot create tickets.
      Confirm responses for unsupported media and conflicting identities.
- [ ] Redeliver an identical signed message before and after container replacement: one
      target ticket. Send a new MessageSid from the same sender: a separate ticket.
- [ ] Exercise target 429/500 and transport failure, scheduled retries and process restart.
      Verify unchanged identity and persisted attempt budget.
- [ ] Simulate target acceptance followed by a lost response. Verify real-target deduplication
      and downstream side effects across retry/restart, not only the mock's behavior.
- [ ] Exercise permanent failures and retry exhaustion. Confirm review state, alert ownership
      and reconciliation; redelivery cannot reset review automatically.
- [ ] Prove a captured event without confirmed handler persistence does not receive successful
      TwiML. Define recovery when acceptance finishes after the edge already returned 503.

### Deployment and operations

- [ ] Load-test expected bursts and sustained traffic, slow targets, serialized SQLite work,
      backlog growth and edge polling. Agree measured capacity and maximum delivery delay.
- [ ] Persist and back up captures, handler/retry state and target deduplication. Rehearse
      coordinated restore and uncertain-response recovery without volume deletion.
- [ ] Define access, encryption, retention and deletion across captures, outbox, tickets and
      backups. Keep operational reports free of message bodies, phone numbers and credentials.
- [ ] Monitor overdue pending work and review entries with named responders and escalation
      contacts. Liveness alone must not be treated as delivery readiness.
- [ ] Rehearse stop/rollback preserving accepted work and deduplication, accounting for in-flight
      forwards and tickets already created.
- [ ] Coordinate the previous processor and bounded cutover set to prevent independent ticket
      writers. Reconcile source identities against target results and obtain business sign-off.

## Handover record

Deliver approved discovery decisions and mapping, deployed version/configuration references,
adapter contract, secret owners, measured capacity, monitoring contacts, completed acceptance
evidence and restore/rollback procedures. Include this FAQ, the [testing guide](TESTING.md)
and [troubleshooting runbook](TROUBLESHOOTING.md). Keep live credentials and customer
messages/phone numbers out of repository files.
