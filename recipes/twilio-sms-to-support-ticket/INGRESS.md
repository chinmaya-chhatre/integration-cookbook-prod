# Durable SMS ingress

`ingress.application(public_url, auth_token, database)` creates a WSGI callable. Run it with this recipe directory on the Python module path and mount it only at the configured webhook path. It requires a fixed HTTPS public URL and rejects unsigned requests, ambiguous fields and unsupported content types. Forwarding headers never alter signature verification. Unknown form fields participate in verification.

Valid messages enter the SQLite outbox before the empty TwiML response is returned. Duplicate messages retain one record. Persistence failure returns 503, so a failed write is never acknowledged. Configure and verify provider retry behavior separately. The response does not send an SMS or create a support ticket synchronously.

The callable is packaged with an HTTP server and scheduled worker in the [runtime](RUNTIME.md). The [Compose demonstration](README.md#run-the-synthetic-demonstration) supplies a TwiML edge, pinned HookScope and a compatible support mock. Keep `.wip` until customer handover is complete, and replace all synthetic configuration before connecting a live number. The target must honor stable idempotency keys before worker replay is safe. See [Troubleshooting](TROUBLESHOOTING.md) for read-only backlog checks and recovery.
