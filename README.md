# integration-cookbook

Runnable, tested integration recipes for the problems customers actually bring to a Solutions
Engineer: "our webhook never arrives", "events show up twice", "the target was down for an
hour, what did we lose?". Each recipe is a small, complete integration with tests for the ways
it breaks, and the checklists and documents you would hand over with it.

The recipes run on [HookScope](https://github.com/chinmaya-chhatre/hookscope-prod), a webhook
receiver that verifies signatures, retries deliveries and lets you replay events.

> **Status: in development.** Completed and planned recipes are listed below.
>
> What exists so far: [`common/`](common), signed-request builders and ready-made example events
> for GitHub, Stripe, Shopify, Slack, Twilio, Svix and generic HMAC senders, checked against the
> providers' published test vectors, and `common/mocks/`, Jira-, CRM- and warehouse-style mock
> servers that can be told to answer 500, 429 or slowly (run `python -m pytest`).

## Who it is for

- **Solutions and integration engineers** designing or debugging a customer integration, who
  want a working starting point and an honest list of what goes wrong.
- **Customer developers** who would rather read a tested example than a vendor's one-page guide.

## Recipes (planned)

| # | Recipe | The problem it solves |
|---|---|---|
| R1 | **stripe-invoice-paid-to-slack** | Tell finance about paid invoices above a threshold, in a readable message |
| R2 | **github-pr-to-jira** | Create and transition Jira issues from pull request events, once per PR |
| R3 | **shopify-order-to-warehouse** | Reshape orders into a warehouse schema; survive an outage and replay afterwards |
| R4 | **stripe-subscription-lifecycle-to-crm** | Keep a CRM correct when subscription events arrive out of order or twice |
| R5 | **twilio-sms-to-support-ticket** | Turn inbound SMS into tickets, including the proxy and URL signature pitfall |
| R6 | **webhook-to-queue-on-aws** | Acknowledge fast, process later: HookScope, SQS and a worker, with Terraform |
| R7 | **slack-slash-command-bot** | Respect Slack's three-second rule with delayed responses |
| R8 | **customer-rest-api-client** | OAuth2 client credentials, pagination, 429 backoff and idempotency keys |
| R9 | **bulk-csv-import** | Load large files with a partial-failure report and resumable runs |
| R10 | **trace-an-integration** | Follow one event end to end with correlation ids, a dashboard and a runbook |

## What every recipe contains

Each lives in `recipes/<name>/` and is finished only when it has all seven parts:

1. **Problem and architecture**: the scenario, with architecture and sequence diagrams
2. **Config**: the HookScope rules and a `docker-compose.yml` that runs the whole thing
3. **Core code**: the handler or transform, in small typed functions
4. **Happy-path tests**: realistic, signed provider payloads
5. **Failure-mode tests**: retries, duplicates, out-of-order events, a target returning 429 or 500, replay after an outage
6. **Troubleshooting**: symptom, likely cause, how to check, how to fix, with real log and metric examples
7. **Customer pack**: FAQ, discovery questions and a go-live checklist for that integration

## Solutions Engineer documents (planned)

Integration design checklist, discovery question bank, proof-of-concept plan and success
criteria, go-live checklist, a troubleshooting decision tree, answers to common security
questionnaire questions, a sizing and rate-limit worksheet, and a guide to choosing between
push, pull and batch.

## Validation scope

Recipes use mock services and signed fixtures to exercise normal and failure paths.
They have not been validated against vendor production systems and carry no vendor
certification. Cloud examples use configuration validation and LocalStack rather than
live-account deployment tests.

## License

[MIT](LICENSE)
