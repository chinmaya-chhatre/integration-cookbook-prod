# Usage: make help
RECIPE ?=
PYTHON ?= python
COMPOSE ?= docker compose

RECIPE_DIR := recipes/$(RECIPE)
TEST_PATHS := $(if $(RECIPE),$(RECIPE_DIR),common recipes tests)
COMPOSE_FILES := -f docker-compose.base.yml $(if $(wildcard $(RECIPE_DIR)/docker-compose.yml),-f $(RECIPE_DIR)/docker-compose.yml)
# A recipe's rules.json is what HookScope loads (compose paths are relative to this folder).
ifneq ($(and $(RECIPE),$(wildcard $(RECIPE_DIR)/rules.json)),)
export HOOKSCOPE_RULES_FILE := ./$(RECIPE_DIR)/rules.json
endif

.PHONY: help test lint format docs check env up down logs config check-recipe

help: ## Show this help
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  make %-8s %s\n", $$1, $$2}'
	@echo
	@echo "Pass RECIPE=<name> (a folder under recipes/) to limit test, up, down and logs to one recipe."

check-recipe:
	@if [ -n "$(RECIPE)" ] && [ ! -d "$(RECIPE_DIR)" ]; then \
		echo "No such recipe: $(RECIPE). Available: $$(ls recipes | grep -v '^_' | tr '\n' ' ')" >&2; exit 2; fi

test: check-recipe ## Run the tests (everything, or one RECIPE)
	$(PYTHON) -m pytest -q $(TEST_PATHS)

lint: ## Lint and check formatting
	$(PYTHON) -m ruff check .
	$(PYTHON) -m ruff format --check .

format: ## Format the code
	$(PYTHON) -m ruff format .

docs: ## Check Markdown links and Mermaid diagrams
	$(PYTHON) scripts/check_docs.py

check: lint docs test ## Everything CI runs

env: ## Write .env with the demo webhook secrets
	$(PYTHON) -m common.env > .env

up: check-recipe env ## Start HookScope (and the recipe's services, if it has any)
	$(COMPOSE) $(COMPOSE_FILES) up --build --wait -d

down: check-recipe ## Stop everything and remove its data volume
	$(COMPOSE) $(COMPOSE_FILES) down -v

logs: check-recipe ## Follow the logs
	$(COMPOSE) $(COMPOSE_FILES) logs -f

config: check-recipe env ## Validate the compose files (the base, and each recipe on top of it)
	$(COMPOSE) -f docker-compose.base.yml config -q
	@for file in recipes/*/docker-compose.yml; do \
		[ -e "$$file" ] || continue; \
		echo "checking $$file"; \
		$(COMPOSE) -f docker-compose.base.yml -f "$$file" config -q || exit 1; \
	done
