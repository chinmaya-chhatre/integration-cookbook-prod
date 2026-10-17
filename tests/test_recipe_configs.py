"""Every recipe's HookScope rules and compose file must hang together, including recipes in progress."""

import json
import re
from pathlib import Path
from urllib.parse import urlparse

import pytest
import yaml

ROOT = Path(__file__).parent.parent
RECIPES = sorted(p for p in (ROOT / "recipes").iterdir() if p.is_dir() and not p.name.startswith(("_", ".")))
SOURCES = {"github", "stripe", "shopify", "slack", "twilio", "svix", "generic"}
BASE = yaml.safe_load((ROOT / "docker-compose.base.yml").read_text())


def compose_of(recipe: Path) -> dict:
    return yaml.safe_load((recipe / "docker-compose.yml").read_text())


def services_of(recipe: Path) -> set[str]:
    return set(BASE["services"]) | set(compose_of(recipe)["services"])


@pytest.mark.parametrize("recipe", RECIPES, ids=lambda p: p.name)
def test_rules_file_is_valid_for_hookscope(recipe):
    rules = json.loads((recipe / "rules.json").read_text())["rules"]
    assert rules, "a recipe needs at least one rule"
    names = [rule["name"] for rule in rules]
    assert len(names) == len(set(names))
    for rule in rules:
        assert rule.get("source") in SOURCES, f"{rule['name']}: say which provider it listens to"
        assert urlparse(rule["target_url"]).scheme in ("http", "https")
        retry = rule.get("retry")
        assert retry and retry["attempts"] >= 2, (
            f"{rule['name']}: a recipe is about surviving failures; set a retry policy"
        )


@pytest.mark.parametrize("recipe", RECIPES, ids=lambda p: p.name)
def test_every_rule_targets_a_service_in_the_stack(recipe):
    services = services_of(recipe)
    for rule in json.loads((recipe / "rules.json").read_text())["rules"]:
        assert urlparse(rule["target_url"]).hostname in services, (
            f"{rule['name']} points outside the compose stack"
        )


@pytest.mark.parametrize("recipe", RECIPES, ids=lambda p: p.name)
def test_compose_adds_to_the_base_without_replacing_hookscope(recipe):
    compose = compose_of(recipe)
    hookscope = compose["services"].get("hookscope", {})
    assert set(hookscope) <= {"depends_on", "environment"}, (
        "extend hookscope's dependencies, do not redefine it"
    )
    for name, service in compose["services"].items():
        assert "healthcheck" in service or name == "hookscope", (
            f"{name} needs a healthcheck so startup order is real"
        )


@pytest.mark.parametrize("recipe", RECIPES, ids=lambda p: p.name)
def test_compose_paths_are_relative_to_the_repository_root(recipe):
    text = (recipe / "docker-compose.yml").read_text()
    paths = re.findall(r"(?:build:|env_file:|-)\s+(\./[\w./-]+)", text)
    assert paths, "expected the compose file to reference its handler or mounts"
    for path in paths:
        parts = Path(path).parts  # "./recipes/x/handler" -> ("recipes", "x", "handler")
        assert parts[0] in ("recipes", "common"), (
            f"{path}: Compose resolves paths against the first -f file's folder"
        )
        if parts[0] == "recipes":
            assert parts[1] == recipe.name


@pytest.mark.parametrize("recipe", RECIPES, ids=lambda p: p.name)
def test_environment_file_is_plain_key_value_pairs(recipe):
    env = recipe / "recipe.env"
    if not env.exists():
        pytest.skip("no recipe.env")
    for line in env.read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            assert re.fullmatch(r"[A-Z][A-Z0-9_]*=\S*", line), line


def test_make_points_hookscope_at_the_recipes_rules():
    import subprocess

    def rules_file(*args: str) -> str:
        return subprocess.run(
            ["make", "-s", "--eval=show:\n\t@echo [$$HOOKSCOPE_RULES_FILE]", "show", *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    for recipe in RECIPES:
        assert rules_file(f"RECIPE={recipe.name}") == f"[./recipes/{recipe.name}/rules.json]"
    assert rules_file() == "[]"  # with no recipe, the base compose file falls back to empty rules
