# SMS handler runtime

The handler image now packages the strict form parser, fixed-URL signature
validator, SQLite outbox, HTTP sender and retry worker. It uses Python's standard
library and runs as an unprivileged user. Build from the repository root:

```sh
docker build -f recipes/twilio-sms-to-support-ticket/handler/Dockerfile -t sms-handler .
```

Supply these environment variables before starting `python runtime.py`:

| Variable | Contract |
| --- | --- |
| TWILIO_PUBLIC_URL | Exact externally configured HTTPS webhook URL, including query |
| TWILIO_AUTH_TOKEN | Twilio account auth token from secret configuration |
| TICKET_STATE_DB | Writable persistent SQLite file path |
| SUPPORT_TICKET_URL | HTTP(S) endpoint accepting the vendor-neutral ticket payload |
| SUPPORT_TOKEN | Support endpoint bearer credential |
| PORT | Internal listener port, default 8080 |

The proxy must preserve the query and original form bytes, and route the public
webhook path to `/twilio/sms`. Arbitrary paths return 404. `GET /healthz` returns
200 while the process is serving requests. Forwarding headers never select the
signed URL. Do not publish the internal port directly to the internet.

The worker runs in the same process and drains the shared outbox. SIGTERM or
SIGINT stops new batches and closes the HTTP listener after its current request.
The sender has a five-second timeout; inbound sockets have a ten-second timeout.
Request access logs are suppressed to avoid exposing personal data. Persist
`/data` across container replacements. The target must honor the idempotency key,
including after an accepted response is lost.

The [demonstration stack](README.md#run-the-synthetic-demonstration) now supplies
a TwiML edge and a compatible persistent support mock. The edge sends the fixed
public URL headers to pinned HookScope and confirms a successful handler forward
before acknowledgement. Use the edge's published port, not HookScope's raw receipt
endpoint, for provider requests. Use the [customer handover pack](CUSTOMER-PACK.md)
to record deployment acceptance. The public example URL and credentials are synthetic; a live
deployment requires actual TLS, private backend networking and secret injection.

See [Troubleshooting](TROUBLESHOOTING.md) for the read-only aggregate health
command, retry-state interpretation and recovery after target acceptance loss.

The edge also requires `HOOKSCOPE_INTERNAL_URL`, `SMS_ADMIN_TOKEN` and
`SMS_HANDLER_URL`. The support mock uses `SUPPORT_STATE_DB` and `SUPPORT_TOKEN`.
The checked-in environment file supplies demonstration values; keep the edge,
HookScope and handler URL/token configuration aligned when replacing them.
