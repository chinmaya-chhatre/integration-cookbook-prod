# stripe-invoice-paid-to-slack

Tell the finance team in Slack when a customer pays an invoice above a threshold, in a message a
human can act on, without drowning the channel in small invoices and without announcing the same
payment twice.

| Part | Status |
|---|---|
| 1. Problem and architecture | done |
| 2. Config | done (the handler it starts is built in part 3) |
| 3. Core code | to do |
| 4. Happy-path tests | to do |
| 5. Failure-mode tests | to do |
| 6. Troubleshooting | to do |
| 7. Customer pack | to do |

## 1. Problem and architecture

### The scenario

A finance team at a SaaS company wants one thing from Stripe: a Slack message in `#finance-alerts`
when a **large** invoice is paid, such as a $5,000 annual plan, so they can reconcile it and thank the
customer. Small invoices (monthly $29 plans) happen hundreds of times a day and must stay out of the
channel. Two details decide whether the alert is trusted or muted within a week:

- **The message has to be readable.** Stripe's payload is a deep JSON document; finance wants the
  invoice number, the customer, the amount in the right currency and a link to open the invoice.
- **It must fire once per payment.** Stripe documents that an endpoint can receive the same event more
  than once, and Slack gets no say in that, so the integration has to deduplicate.

### What must be true

| Requirement | Why it matters |
|---|---|
| Only verified Stripe events are acted on | Anyone can POST to a public URL; the `Stripe-Signature` header is the proof |
| An invoice below the threshold sends nothing | The point of the filter |
| The threshold is in the customer's units, per currency | Stripe amounts are integers in the smallest unit (cents), and some currencies have no minor unit, so `5000` means $50.00 for USD and ¥5,000 for JPY |
| The same event never produces two messages | Stripe may redeliver; HookScope may retry |
| A Slack outage loses nothing | Failed deliveries are retried, then kept as dead letters that can be replayed |
| A skipped invoice is not an error | The handler answers 2xx, so HookScope does not retry something that was deliberately ignored |

### Where the filter lives

HookScope forwarding rules match on the **source** and the **event type** (`invoice.paid`), not on
values inside the payload, so a rule cannot say "only above $5,000". HookScope's built-in
`format: slack` would post every paid invoice. The minimum-amount filter, the currency handling and
the deduplication therefore live in a small handler that sits between HookScope and Slack, and that
handler is the code this recipe builds (part 3). This split is deliberate and worth knowing in a
customer conversation: put routing in the receiver, and business rules in code you can test.

### Architecture

```mermaid
flowchart LR
    Stripe["Stripe<br/>invoice.paid"] -->|"signed POST /hooks/stripe"| HookScope["HookScope<br/>verify signature, store, ack 202"]
    HookScope -->|"rule: source=stripe, type=invoice.paid"| Handler["Handler<br/>threshold, currency, dedupe, format"]
    Handler -->|"incoming webhook"| Slack["Slack<br/>#finance-alerts"]
    HookScope -.->|"retries, dead letters, replay"| HookScope
    Handler -.->|"below threshold or duplicate: 200, no message"| HookScope
```

### One delivery, step by step

```mermaid
sequenceDiagram
    participant S as Stripe
    participant H as HookScope
    participant A as Handler
    participant K as Slack
    S->>H: POST /hooks/stripe (invoice.paid, Stripe-Signature)
    H->>H: verify signature and timestamp, store the event
    H-->>S: 202 Accepted
    H->>A: forward the event (rule matches invoice.paid)
    alt amount is below the threshold, or event id already seen
        A-->>H: 200 skipped
    else a new event above the threshold
        A->>A: format the message, remember the event id
        A->>K: POST message to the incoming webhook
        K-->>A: 200 ok
        A-->>H: 200 sent
    end
    Note over H,A: if the handler or Slack answers 429 or 5xx,<br/>HookScope retries with backoff, then keeps a dead letter
```

### The message finance sees

```text
Invoice ACME-0042 paid: 1,250.00 USD
Customer: billing@acme.example
Open invoice: https://invoice.stripe.com/i/acct_test/in_1QxAcmeInvoice
(event evt_1QxAcmePaid, test mode)
```

The invoice number, customer email, amount and link come from the `invoice` object inside the event
(`number`, `customer_email`, `amount_paid`, `currency`, `hosted_invoice_url`). The sample payload used
by the tests is built by `common/fixtures.py`: `stripe_invoice_paid()`.

### Decisions made up front

1. **Deduplicate on the Stripe event id** (`evt_...`), not on the invoice id: a second event about the
   same invoice (a later correction) is news, a redelivery of the first is not.
2. **Compare in major units per currency**, using a small table of zero-decimal currencies, so the
   threshold reads "5,000 USD", not "500000".
3. **Answer 2xx for skips**, 5xx only when Slack could not be reached, so retries are reserved for
   real failures.
4. **Slack is a mock in the tests**: the repository's `common/mocks` stands in for it, and the recipe
   says so rather than claiming it was exercised against a real workspace.

## 2. Config

Three files describe the setup; nothing here is code.

| File | What it does |
|---|---|
| [`rules.json`](rules.json) | HookScope's forwarding rule: events from `stripe` of type `invoice.paid` go to the handler, with up to 5 attempts and exponential backoff from 2 s to 60 s |
| [`docker-compose.yml`](docker-compose.yml) | Adds the handler and a mock Slack to HookScope (from the repository's base compose file) and makes HookScope wait for the handler to be healthy |
| [`recipe.env`](recipe.env) | The handler's settings: the per-currency thresholds and how many event ids to remember |

### The rule

```json
{
  "name": "invoice-paid",
  "source": "stripe",
  "event_types": ["invoice.paid"],
  "target_url": "http://handler:8080/stripe/invoice-paid",
  "retry": {"attempts": 5, "backoff_seconds": 2, "max_backoff_seconds": 60}
}
```

- `source` and `event_types` keep every other provider and every other Stripe event away from the handler.
- The retry policy is what turns a Slack or handler outage into a delay instead of a loss: HookScope waits
  2, 4, 8 and 16 seconds between attempts (longer if the target sends `Retry-After`), then keeps the
  delivery as a dead letter that can be resent from the dashboard.
- Only events whose signature verified are forwarded; a forged request is stored and never reaches the handler.

### Settings

| Setting | Where | Meaning |
|---|---|---|
| `HOOKSCOPE_SECRET_STRIPE` | `.env`, written by `make env` | The signing secret HookScope verifies with. The demo value comes from `common/fixtures.py`; in production it is the endpoint's `whsec_...` from the Stripe dashboard |
| `INVOICE_THRESHOLDS` | `recipe.env` | Minimum amount per currency in major units, e.g. `USD:5000,EUR:4500,JPY:750000`. A currency that is not listed never alerts |
| `SLACK_WEBHOOK_URL` | `docker-compose.yml` | Where the handler posts. It points at the mock Slack; put a real incoming-webhook URL here to try it for real |
| `DEDUPE_MEMORY` | `recipe.env` | How many Stripe event ids to remember when ignoring redeliveries |

### Run it

```bash
make env                                              # write .env with the demo secrets
make up RECIPE=stripe-invoice-paid-to-slack           # HookScope, the handler and a mock Slack
python -m common.send stripe_invoice_paid amount_paid=500000 currency=usd
curl localhost:9000/_mock/messages                    # what "Slack" received
make down RECIPE=stripe-invoice-paid-to-slack
```

`common.send` signs a realistic `invoice.paid` event the way Stripe does and posts it to HookScope, so the
whole path runs: verification, the rule, the handler, Slack. Until part 3 adds the handler, `make up` stops
at building it; the rule, the compose file and the mock Slack are checked by the tests in the meantime.

Compose resolves every relative path against the repository root (the first `-f` file), so the paths in
this recipe's compose file start with `./recipes/stripe-invoice-paid-to-slack/`.

## 3. Core code

_To do: the handler, in small typed functions._

## 4. Happy-path tests

_To do: signed `invoice.paid` fixtures through HookScope to the handler._

## 5. Failure-mode tests

_To do: duplicates, Slack 429 and 500, a below-threshold invoice, zero-decimal currencies, replay after
a Slack outage._

## 6. Troubleshooting

_To do: symptom, cause, how to check and fix, with real log lines._

## 7. Customer pack

_To do: FAQ, discovery questions and a go-live checklist._
