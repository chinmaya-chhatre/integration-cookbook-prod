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

This runtime completes the deployable core, but the demonstration stack remains
unfinished and marked `.wip`. Its environment file still needs real secret
injection and a public HTTPS URL, and its Jira-shaped mock does not accept the
vendor-neutral payload. HookScope's URL reconstruction and acknowledgement
behavior also need an explicit deployment design. Do not connect a live Twilio
number or treat `make up` as an end-to-end acceptance test yet.
