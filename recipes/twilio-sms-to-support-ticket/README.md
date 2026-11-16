# Twilio SMS to support ticket

## Problem and scope

Turn an inbound SMS into a support ticket without losing messages during a target outage or creating duplicate tickets after redelivery. This recipe designs the boundary between Twilio, HookScope, a ticket handler and a generic support API mock. It is an architecture specification; configuration is included, while handler code and executable scenarios follow separately. It does not claim compatibility with a particular support vendor.

Inbound messaging webhooks carry `application/x-www-form-urlencoded` fields, including `MessageSid`, `AccountSid`, `From`, `To` and `Body`. They are not JSON. Parse form encoding once, preserving blank values and Unicode; do not trim, normalize or re-encode values before signature verification. Reject ambiguous duplicate parameter names rather than silently choosing a value. Unknown fields must participate in verification.

## Architecture

```mermaid
flowchart TD
    T[Twilio inbound SMS] --> P[HTTPS reverse proxy]
    P --> H[HookScope signature boundary]
    H --> Q[Durable delivery record]
    Q --> W[Ticket handler]
    W --> S[Support API mock]
    W --> D[Dead letter and replay]
```

The receiving boundary validates the request before accepting work. It returns a successful TwiML response, such as `<Response/>`, only after durable acceptance. An empty response sends no automatic SMS reply. Authentication failures must not create tickets or enqueue work. A failure before durable acceptance returns an error, with provider retry behavior configured and verified separately.

Durable acceptance, retry storage and safe replay are requirements for the implementation, not guarantees of this architecture page. If the pinned HookScope version cannot satisfy a requirement, the subsequent configuration and handler work must supply it explicitly before go-live.

## Signature and proxy boundary

Twilio signs the exact externally configured webhook URL, including its scheme, host, path and query string, together with the form parameters using the account auth token. Validate `X-Twilio-Signature` with a maintained Twilio validator or a tested equivalent. A proxy that changes HTTPS to HTTP, strips a path prefix or rewrites the host can make an authentic request fail verification.

Configure the public webhook URL explicitly. Do not construct it from arbitrary client-provided forwarding headers. If trusted proxy metadata is used, restrict direct ingress and define which proxy can set those headers. Preserve the original form body and content type through the proxy. Outbound forwarding to the ticket handler requires its own authentication; the handler must not attempt to validate the original Twilio signature against its internal URL.

Keep the auth token in secret configuration. Never log it, signatures, full phone numbers or SMS bodies. Correlate deliveries with `MessageSid` and a request identifier; redact personal data in diagnostic examples.

## Delivery sequence

```mermaid
sequenceDiagram
    participant T as Twilio
    participant H as HookScope ingress
    participant W as Ticket handler
    participant S as Support mock
    T->>H: Signed form POST to public URL
    H->>H: Validate signature and persist delivery
    H-->>T: Successful empty TwiML
    H->>W: Authenticated delivery with MessageSid
    W->>S: Create ticket with stable idempotency key
    alt Ticket accepted or already exists
        S-->>W: Ticket identifier
        W-->>H: Delivery complete
    else Rate limit or transient failure
        S-->>W: 429 or 500
        W-->>H: Retryable failure
        H->>H: Bounded retry then dead letter
    end
```

## Mapping and delivery policy

| Source | Target use |
| --- | --- |
| AccountSid and MessageSid | Durable deduplication key scoped to the sending account |
| Body | Ticket description, treated as untrusted text |
| From | Restricted contact field, subject to retention policy |
| To | Support destination or routing key |
| MessageSid | External reference and correlation identifier |

Create one ticket per inbound message, not one per phone number. A new message from the same sender creates another ticket. Conversation threading and outbound replies are outside this recipe. Attachments are outside the initial SMS scope; do not fetch arbitrary media URLs.

Persist the mapping from account/message identity to ticket identifier. Concurrent deliveries require atomic claims or a unique constraint. Pass a stable target idempotency key where supported. If the target accepts a request but the response is lost, reconcile by external reference before creating again. Local deduplication alone cannot guarantee exactly-once creation across that failure window.

Honor target rate limits with bounded backoff and jitter. Retry transient transport failures, 429 and selected 5xx responses. Route permanent validation failures to operator review. Preserve the same identity during replay; never manufacture a new message identity to bypass deduplication. Retain failed work durably with a documented retention period and access controls.

## Acceptance criteria for the runnable recipe

- A valid signed form creates one ticket with Unicode and blank fields preserved.
- A modified body, wrong token or mismatched public URL is rejected before side effects.
- Trusted proxy configuration validates the public URL while internal URL reconstruction fails predictably.
- Duplicate and concurrent deliveries create one ticket; different message identities remain independent.
- Target 429/500, response loss after acceptance, restart and outage replay retain identity and avoid duplicate creation.
- Required integration tests execute with the pinned HookScope dependency and local mocks.

## Implementation status

Problem, architecture and deployment configuration are defined. The recipe remains marked `.wip`: the handler build context and Dockerfile arrive with core code. Compose configuration can be validated now, but `make up` is not yet supported. Signed integration scenarios, failure tests, troubleshooting evidence and customer go-live material remain outstanding.

## Configuration contract

Merge `docker-compose.base.yml` followed by this recipe's `docker-compose.yml`, from the repository root. Run `make config RECIPE=twilio-sms-to-support-ticket` to validate the configuration. Once the handler is implemented, `make up RECIPE=twilio-sms-to-support-ticket` will select this recipe's rules automatically.

The `inbound-sms` rule forwards raw Twilio form bytes to `http://handler:8080/twilio/sms`. It deliberately has no JSON transform or event-type filter: form bodies have no JSON `type`. Five total attempts use exponential delays starting at two seconds, capped at sixty seconds. Invalid or unsigned events do not match; a missing configured secret is unsafe because HookScope permits `no_secret` events. Always supply `HOOKSCOPE_SECRET_TWILIO` through secret configuration; `make env` supplies demonstration secrets only.

The handler has no published port, persists its future deduplication state at `/data/tickets.sqlite3`, and waits for the support mock. The mock uses the existing Jira-shaped issue API as a generic ticket stand-in, at `http://support:9000/rest/api/3/issue`. Neither service is exposed on a host port. HookScope waits for the handler's `/healthz`.

`TWILIO_PUBLIC_URL` in `recipe.env` defines the future handler boundary contract, not a HookScope setting. The pinned HookScope dependency reconstructs the incoming URL and has no explicit public-URL override. Its current ingress also does not implement this page's TwiML acceptance contract. Do not connect a live Twilio number yet. Core implementation must supply a trusted URL/signature boundary, authenticated internal delivery, duplicate-parameter rejection and durable acceptance before declaring the full recipe runnable. Never solve URL mismatches by disabling signature checks.

CI keeps HookScope pinned to `b4e012c304ea69ca29fd544ab0ab2864e5cf11f6`. The shared Compose base currently builds production main; do not treat that development stack as evidence of pinned-version behavior. Configuration tests exercise the actual pinned rules parser and verify that form payloads match without a JSON event type. Full-suite CI includes these tests while the completed-recipe matrix continues to exclude this unfinished recipe.
