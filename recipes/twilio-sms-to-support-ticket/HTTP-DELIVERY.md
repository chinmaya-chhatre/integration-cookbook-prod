# HTTP ticket delivery

`TicketSender(url, token, timeout=5)` adapts the persistent outbox sender contract to JSON HTTP POST. It sends the stable `Idempotency-Key` and bearer credential, returns HTTP failure statuses to the outbox, and lets transport errors retain pending work. Redirects are refused so credentials cannot follow a changed destination. Use HTTPS outside local development.

Local HTTP tests exercise target 429/500, reopening the database and replaying with the same key and body. A redirect produces review state without following it. The target must implement idempotency or external-reference reconciliation; this adapter cannot provide that guarantee itself.

The adapter runs inside the [runtime](RUNTIME.md) with automatic [retry scheduling](WORKER.md). The [Compose demonstration](README.md#run-the-synthetic-demonstration) includes a compatible persistent support mock; `.wip` remains until customer handover is complete. See [Troubleshooting](TROUBLESHOOTING.md) before recovery or replay.
