# github-pr-to-jira

Give every GitHub pull request one Jira issue, and keep that issue's status in step with the pull
request, without creating a second issue when GitHub sends the same event twice or when events arrive
in the wrong order.

| Part | Status |
|---|---|
| 1. Problem and architecture | done |
| 2. Config | done (the handler it starts is built in part 3) |
| 3. Core code | done |
| 4. Happy-path tests | next |
| 5. Failure-mode tests | to do |
| 6. Troubleshooting | to do |
| 7. Customer pack | to do |

## 1. Problem and architecture

### The scenario

An engineering manager wants Jira to show the review work that GitHub already knows about. When a
developer opens a pull request, a Jira issue ("Review PR #42: Fix retry backoff") should appear in the
team's project with a link back to the pull request. When the pull request is merged, the issue moves
to **Done**. Nobody should have to copy anything by hand, and nobody should find three issues for one
pull request. Two things decide whether the integration is trusted or switched off:

- **One pull request, one issue.** GitHub delivers a webhook at least once, may deliver it again, and
  gives no ordering guarantee between a `closed` event and the `opened` that came before it. A handler
  that creates an issue for every `opened` it sees will eventually create duplicates.
- **A late event must not undo a later one.** If the "opened" delivery is retried after the pull request
  has already been merged, the issue must stay Done.

### What must be true

| Requirement | Why it matters |
|---|---|
| Only verified GitHub events are acted on | Anyone can POST to a public URL; `X-Hub-Signature-256` is the proof |
| A pull request has at most one issue, whatever arrives, however often | The point of the recipe; identity is the pull request's id, not the delivery |
| Opening a pull request creates the issue; merging moves it to Done | The two transitions the team cares about |
| A `closed` event for a pull request the handler has never seen still ends with a Done issue | Events can arrive out of order, or the first delivery can be lost |
| An old event never moves an issue backwards | A retried `opened` after a merge is news from the past |
| A Jira outage loses nothing | Failed deliveries are retried, then kept as dead letters that can be replayed |
| Events that need no action are not errors | Edits, new commits and labels answer 2xx so HookScope does not retry them |

### Where the filter lives

HookScope forwarding rules match on the **source** and the **event type**. For GitHub the event type is
the `X-GitHub-Event` header, so a rule can pick `pull_request` events but cannot pick "opened" or "closed
and merged", which are values of `action` inside the payload. The rule therefore sends every
`pull_request` event to a small handler, and the handler decides: which actions matter, which issue the
pull request maps to, and what to do. Routing stays in the receiver; the idempotency and state logic
live in code that can be tested.

### Architecture

```mermaid
flowchart LR
    GitHub["GitHub<br/>pull_request"] -->|"signed POST /hooks/github"| HookScope["HookScope<br/>verify signature, store, ack 202"]
    HookScope -->|"rule: source=github, type=pull_request"| Handler["Handler<br/>PR id to issue, state rules"]
    Handler -->|"create issue, transition"| Jira["Jira<br/>project PROJ"]
    Handler <-->|"pull request id and issue key"| Map[("Mapping store<br/>PR id to issue key")]
    HookScope -.->|"retries, dead letters, replay"| HookScope
    Handler -.->|"nothing to do: 200, no change"| HookScope
```

### One delivery, step by step

```mermaid
sequenceDiagram
    participant G as GitHub
    participant H as HookScope
    participant A as Handler
    participant J as Jira
    G->>H: POST /hooks/github (pull_request, opened)
    H->>H: verify signature, store the event
    H-->>G: 202 Accepted
    H->>A: forward the event (rule matches pull_request)
    A->>A: look up the pull request id
    alt no issue yet
        A->>J: create the issue
        J-->>A: PROJ-17
        A->>A: remember PR id as PROJ-17
        A-->>H: 200 created
    else already has an issue
        A-->>H: 200 already linked
    end
    G->>H: POST /hooks/github (pull_request, closed, merged)
    H->>A: forward the event
    A->>J: move PROJ-17 to Done
    J-->>A: 204
    A-->>H: 200 transitioned
    Note over H,A: if Jira answers 429 or 5xx the handler answers 502,<br/>HookScope retries with backoff, then keeps a dead letter
```

### What each pull request event does

| Event (`action`) | The handler | The issue ends up |
|---|---|---|
| `opened` | Creates the issue if the pull request has none | To Do |
| `reopened` | Creates it if missing, otherwise moves it back | To Do |
| `closed` with `merged: true` | Creates it if missing, then moves it | Done |
| `closed` without merging | Records nothing; people decide whether abandoned work is Done | unchanged |
| `edited`, `synchronize`, `labeled`, `review_requested`, others | Nothing to do, answers 200 | unchanged |
| Any event repeated | Finds the existing issue and changes nothing | unchanged |

The issue's summary comes from the pull request (`Review PR #42: Fix retry backoff`), and its text has
the link to the pull request, the repository and the author. These come from the `pull_request` object
in the payload (`number`, `title`, `html_url`, `user.login`) and `repository.full_name`; the sample
event used by the tests is `common/fixtures.py`: `github_pull_request()`.

### Decisions made up front

1. **Identity is the pull request's id**, not the delivery id: a redelivery has the same pull request id
   and so finds the same issue, and a re-opened pull request keeps its one issue.
2. **The handler keeps the pull-request-to-issue mapping itself** (a small database file on a volume),
   because HookScope rules cannot do it and Jira has no notion of "the issue for pull request 1000042".
   The first thing the handler does for a pull request is claim its id, so two deliveries racing each
   other cannot both create an issue. The one gap, a crash after Jira created the issue and before the
   handler recorded its key, is covered in the failure tests and the troubleshooting section.
3. **Move forward only.** Only `reopened` moves an issue back to To Do; a repeated `opened` never does.
4. **Answer 2xx for anything deliberately ignored**, 400 for a payload the handler cannot read, and 502
   only when Jira could not be reached or refused the request, so retries are reserved for real failures.
5. **Jira is a mock in the tests**: `common/mocks.JiraMock` stands in for it, and the recipe says so
   rather than claiming it was exercised against a real Jira site.

## 2. Config

Three files describe the setup; nothing here is code.

| File | What it does |
|---|---|
| [`rules.json`](rules.json) | HookScope's forwarding rule: events from `github` of type `pull_request` go to the handler, with up to 5 attempts and exponential backoff from 2 s to 60 s |
| [`docker-compose.yml`](docker-compose.yml) | Adds the handler (with a volume for its pull-request-to-issue mapping) and a mock Jira to HookScope, and makes HookScope wait for the handler to be healthy |
| [`recipe.env`](recipe.env) | The handler's settings: the Jira project, the issue type, the workflow transition ids and where the mapping is kept |

### The rule

```json
{
  "name": "pull-request",
  "source": "github",
  "event_types": ["pull_request"],
  "target_url": "http://handler:8080/github/pull-request",
  "retry": {"attempts": 5, "backoff_seconds": 2, "max_backoff_seconds": 60}
}
```

- `source` and `event_types` keep every other provider, and every other GitHub event (`push`, `issues`,
  `check_run`, ...), away from the handler. For GitHub the event type is the `X-GitHub-Event` header, so
  `pull_request` is matched there; the `action` inside the payload is the handler's business (part 1).
- The retry policy is what turns a Jira outage into a delay instead of a loss: HookScope waits 2, 4, 8
  and 16 seconds between attempts (longer if the target sends `Retry-After`), then keeps the delivery as
  a dead letter that can be re-sent from the dashboard.
- Only events whose signature verified are forwarded; a forged request is stored and never reaches the handler.

### Settings

| Setting | Where | Meaning |
|---|---|---|
| `HOOKSCOPE_SECRET_GITHUB` | `.env`, written by `make env` | The webhook secret HookScope verifies `X-Hub-Signature-256` with. The demo value comes from `common/fixtures.py`; in production it is the secret you typed into the GitHub webhook settings |
| `JIRA_BASE_URL` | `docker-compose.yml` | Where the handler calls Jira. It points at the mock; for Jira Cloud it is your site's URL |
| `JIRA_EMAIL`, `JIRA_API_TOKEN` | `docker-compose.yml` | The account the handler acts as. The mock ignores them and the values here are placeholders; real ones belong in your secret store, not in git |
| `JIRA_PROJECT` | `recipe.env` | The project key the issues are created in (`PROJ` here) |
| `JIRA_ISSUE_TYPE` | `recipe.env` | The issue type to create (`Task`) |
| `JIRA_TRANSITION_TODO`, `JIRA_TRANSITION_DONE` | `recipe.env` | Workflow transition ids. They differ per Jira project, so look yours up with `GET /rest/api/3/issue/<KEY>/transitions`; `11` and `31` are the ids of the repository's mock |
| `PR_DB` | `recipe.env` | The file holding the pull-request-to-issue mapping, on the `handler-data` volume so a restart keeps it |

The mapping must survive a restart: if it were lost, a redelivery of an old `opened` event would create a
second issue for a pull request that already has one. That is why it lives on a volume, not in memory.

### Run it

```bash
make env                                              # write .env with the demo secrets
make up RECIPE=github-pr-to-jira                      # HookScope, the handler and a mock Jira
python -m common.send github_pull_request action=opened number=42
curl localhost:9001/rest/api/3/issue/PROJ-1           # what "Jira" now holds
python -m common.send github_pull_request action=closed number=42 merged=true
make down RECIPE=github-pr-to-jira
```

`common.send` signs a realistic `pull_request` event the way GitHub does and posts it to HookScope, so the
whole path runs: verification, the rule, the handler, Jira. `make up` builds the handler from `handler/` (part 3).

Compose resolves every relative path against the repository root (the first `-f` file), so the paths in
this recipe's compose file start with `./recipes/github-pr-to-jira/`.

## 3. Core code

The handler is one standard-library file, [`handler/app.py`](handler/app.py), with a short
[`Dockerfile`](handler/Dockerfile). It listens on port 8080: `POST /github/pull-request` for HookScope and
`GET /healthz` for the compose health check. The decisions live in small pieces, separate from HTTP:

| Piece | Job |
|---|---|
| `Settings.from_env` | Reads and checks every setting at once; a missing or malformed one stops the container with one line naming all of them |
| `PullRequests` | The mapping on the volume: pull request id to issue key and last state. `claim` decides who creates the issue |
| `Jira` | The two calls the recipe needs, creating an issue and moving it, signed in with the configured account |
| `parse_event`, `wanted_state` | Read the webhook, and say which state the issue should end in (or that the event needs no action) |
| `Handler.handle` | One event in, `(status, body)` out |

What `handle` answers, and why:

| Situation | Answer | Reason |
|---|---|---|
| New pull request, `opened` (or `reopened`, or a merge) | `200 created` | The issue was created (and moved to Done for a merge) |
| The pull request already has an issue and nothing needs changing | `200 already_linked` | A repeat is not news |
| Merge of a pull request whose issue is still To Do; reopening a Done one | `200 moved` | The only two moves the handler makes |
| `edited`, `synchronize`, a label, a review request, closing without merging | `200 skipped` | A deliberate non-event is not a failure, so HookScope must not retry it |
| Body is not a pull request event | `400 unreadable` | The same bytes will never work, so a retry would only repeat the failure |
| Jira unreachable, refused the request, or answered 429 or 5xx | `502 jira_failed` | A retry can fix it; HookScope backs off, then keeps a dead letter |
| Another delivery for the same pull request is creating the issue right now | `503 busy` | Ask HookScope to come back in a moment |

How one issue per pull request is kept true:

- **Claim first, then create.** Before calling Jira the handler claims the pull request id in the database
  inside one transaction. Of several simultaneous deliveries exactly one is told `claimed`; the rest get
  `503 busy` and retry after the issue is recorded, so they find it.
- **Release on failure.** If Jira fails, the claim is given back, so the retry can create the issue; it
  does not wait for a timeout.
- **A claim nobody finished is taken over** after 60 seconds (`CLAIM_SECONDS`), so a crash between asking
  Jira and recording the key does not block a pull request forever. This is the one gap: if Jira had
  already created the issue before the crash, the takeover creates a second one. Every issue carries the
  label `github-pr-<id>`, so a search finds duplicates and the troubleshooting section tells you how.
- **Forward only.** The handler records the state it last set. A repeated or late `opened` finds the issue
  and changes nothing, so it can never undo a merge; only `reopened` moves a Done issue back to To Do.
- **Ordering.** A merge for a pull request the handler has never seen creates the issue first and then
  moves it, so a lost or late `opened` costs nothing.

The decisions are covered by `tests/test_pr_handler.py`, which runs the handler against the repository's Jira
mock: creation and the issue's text, repeats, a restart, eight simultaneous deliveries, moves, every
ignored action, unreadable bodies, Jira failing at each step, the claim rules and the settings. Part 4
adds the full path through HookScope.

## 4. Happy-path tests

_To do: signed `pull_request` fixtures through HookScope to the handler and Jira._

## 5. Failure-mode tests

_To do: duplicates, out-of-order events, Jira 429 and 500, crash between create and record, replay after
an outage._

## 6. Troubleshooting

_To do: symptom, cause, how to check and fix, with real log lines._

## 7. Customer pack

_To do: FAQ, discovery questions and a go-live checklist._
