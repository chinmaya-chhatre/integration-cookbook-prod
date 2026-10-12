# integration-cookbook

Runnable, tested integration recipes (webhooks, REST APIs, queues), plus the checklists and
documents a Solutions Engineer hands a customer. The recipes run on
[HookScope](https://github.com/chinmaya-chhatre/hookscope-prod), a webhook receiver that verifies
signatures, retries deliveries and lets you replay events.

**Status:** in development.

Each recipe will include an architecture diagram, a docker-compose file that runs it, tests with
realistic provider payloads (including failure modes), a troubleshooting table, and a customer
pack with FAQ, discovery questions and a go-live checklist.
