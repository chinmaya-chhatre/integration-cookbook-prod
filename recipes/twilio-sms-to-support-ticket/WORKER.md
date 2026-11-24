# Scheduled SMS delivery

Run `python worker.py` from this recipe directory with `TICKET_STATE_DB`,
`SUPPORT_TICKET_URL` and `SUPPORT_TOKEN` supplied in the environment. The
worker uses the existing HTTP sender, refuses redirects and keeps credentials
out of logs. Configure HTTPS for a real target.

`Outbox.work(sender, now, limit=100)` claims due pending tickets and stores its
attempt count and next due time in the same SQLite transaction as delivery
state. Times are Unix seconds, so restarting a process preserves the schedule.
Retries wait 2, 4, 8 and 16 seconds after the first four failed attempts.
A fifth transient failure moves the ticket to review. Permanent failures move
to review immediately. Each call processes a bounded batch; the command uses
one ticket per batch and checks for shutdown between bounded HTTP requests.

A crash rolls back the transaction. If the target accepted before the crash,
the next attempt reuses the same idempotency key. Target idempotency or external
reference reconciliation remains mandatory. SQLite serializes network attempts
under a write lock, so this demonstration is intended for low throughput.
Clock corrections can affect due times. Monitor review records and disk health.

The older `deliver` method is an explicit operator-driven attempt and bypasses
scheduled timing. Use `work` for unattended processing. Do not mix manual
attempts into a scheduled retry budget without reconciling target state.

Tests cover persisted due times, restart recovery, retry exhaustion, transport
loss, permanent failures and batch bounds. The worker is executable core code;
the [runtime](RUNTIME.md) packages it with the inbound HTTP server. The [Compose demonstration](README.md#run-the-synthetic-demonstration)
now includes the edge, pinned HookScope and a compatible persistent support mock.
The recipe retains `.wip` until customer handover is complete. Use the read-only checks in [Troubleshooting](TROUBLESHOOTING.md)
to distinguish due backlog from scheduled retries before recovery.
