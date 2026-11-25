# Durable delivery boundary

`Outbox` persists ticket payloads by account-scoped message identity. Call `enqueue` only after signature verification and form parsing; pass `ticket_payload(message)` and `message.identity`. Duplicate content is accepted without another row; conflicting reuse of an identity fails.

A worker calls `deliver(identity, sender)`. The sender receives the payload and a stable idempotency key, performs a bounded HTTP request and returns its status. Transport failures, 408/425/429 and 5xx retain pending work for a later scheduled attempt. Other non-success statuses require review. Delivered identities are not resent. Keep the SQLite volume and back it up; the write transaction serializes attempts, so this demonstration is intended for low throughput.

The target must honor the idempotency key or reconcile external references. Local state alone cannot prevent a duplicate after target acceptance followed by response loss. Tests exercise that contract with an injected target. The [HTTP adapter](HTTP-DELIVERY.md), [durable ingress](INGRESS.md), [scheduled worker](WORKER.md) and [runnable stack](README.md#run-the-synthetic-demonstration) are available. The stack uses a persistent vendor-neutral support mock, not a certified vendor API. Complete the [customer acceptance gates](CUSTOMER-PACK.md) before connecting real systems.

## Concurrent delivery

Enqueue obtains a SQLite write transaction before checking an identity. Concurrent
copies of the same payload produce one pending row; the other callers return
false. A conflicting payload raises ValueError and cannot overwrite the winner.
Delivery attempts also serialize under a write transaction: once an attempt is
recorded as delivered, another worker does not send it again.

Concurrency tests use independent connections and simultaneous callers, then
reopen the outbox to verify the winning payload. SQLite lock contention can still
time out and must be treated as a failed acceptance, never a successful response.
These transactions do not replace target idempotency after a crash or response
loss, and must be used with the worker's persisted retry scheduling for unattended delivery.
