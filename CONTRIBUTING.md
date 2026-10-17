# Contributing

This repository is a set of integration recipes, each one a small, complete, tested example of a
problem customers bring to a Solutions Engineer, plus the documents that go with them. This page
says how to set up, how to add a recipe and what "finished" means.

## Set up

You need Python 3.11 or newer. Docker is only needed to run a recipe's stack (`make up`).

```bash
pip install -r requirements-dev.txt
make help      # every target
make check     # what CI runs: lint, docs checks, tests
```

| Command | What it does |
|---|---|
| `make test` | All tests; `make test RECIPE=<name>` runs one recipe's |
| `make lint` | `ruff check` and `ruff format --check` |
| `make format` | Apply the formatting |
| `make docs` | Check Markdown links and Mermaid diagrams |
| `make up RECIPE=<name>` | Start HookScope and the recipe's own services; `make down` stops them |

## Add a recipe

1. Copy [`recipes/_template/`](recipes/_template/README.md) to `recipes/<name>/`. The name says what
   goes in and what comes out, like `stripe-invoice-paid-to-slack`.
2. Add an empty `recipes/<name>/.wip` file. Fill in the README section by section, as you build each part. The skeleton has one section per
   part of the definition of done below.
3. Build on what is already here instead of rewriting it:
   - [`common/fixtures.py`](common/fixtures.py) and [`common/signing.py`](common/signing.py) give you
     correctly signed requests for each provider, so tests post realistic payloads.
   - [`common/mocks/`](common/mocks) are Jira-, CRM- and warehouse-style servers that can be told to
     answer 500, 429 or slowly. Use them as the target of your recipe.
   - [`docker-compose.base.yml`](docker-compose.base.yml) runs HookScope. Your recipe's own
     `docker-compose.yml` adds its services and talks to `http://hookscope:8000`. If the recipe has a
     `rules.json`, `make up RECIPE=<name>` loads it into HookScope. Compose paths are relative to the repository
     root, not to the recipe folder.
   - `python -m common.send <fixture>` signs one of the fixtures and posts it to a running HookScope;
     `python -m common.mocks slack` (or `jira`, `crm`, `warehouse`) runs a mock as a compose service.
4. Run `make test RECIPE=<name>` and `make check` before you push.

## A recipe is done when it has all seven parts

Complete the following parts before marking a recipe ready for use.

1. **Problem and architecture**: the scenario in the customer's words, with a Mermaid architecture
   diagram and a sequence diagram.
2. **Config**: the HookScope rules and a `docker-compose.yml` that runs the whole recipe with
   `make up RECIPE=<name>`.
3. **Core code**: the handler or transform, in small typed functions.
4. **Happy-path tests**: realistic, signed payloads from `common/`, not hand-written headers.
5. **Failure-mode tests**: at least retries, duplicates, out-of-order events where they apply, the
   target answering 429 and 500, and replay after an outage. This is what makes a recipe useful to a
   customer; a recipe does not ship without it.
6. **Troubleshooting**: symptom, likely cause, how to check it, how to fix it, with real log lines
   and metric names, not placeholders.
7. **Customer pack**: a FAQ, discovery questions and a go-live checklist for this integration.

CI runs one job per finished recipe and fails a recipe that has no tests. While you are still building
yours, keep an empty `.wip` file in its folder so CI leaves it out; the commit that adds the last part
deletes the file, and the recipe joins the matrix.

## Testing and credentials

The Jira, CRM and warehouse services in this repository are mocks. The recipes have not
been validated against vendor production systems and carry no vendor certification.
Provider documentation and published test vectors are linked in the relevant recipes.

Values in `common/fixtures.py` are demo credentials. Production credentials belong in a
secret store or environment variables. Cloud examples use configuration validation and
LocalStack; validation does not establish that a deployment will succeed in a real account.

Include relevant regression tests with changes and explain any corrections to existing tests.

## Documents and diagrams

Markdown is checked in CI: every relative link and `#anchor` has to resolve, and every `mermaid`
block needs a known diagram type, balanced brackets and quotes and matched `subgraph`/`end` pairs.
Run `make docs` to see the same result locally. The check catches typos that would break rendering;
it is not a full Mermaid parser, so look at the diagram on GitHub too.
