"""The Makefile and compose file are code too: keep them honest."""

import subprocess
from pathlib import Path

import pytest
import yaml

from common import fixtures
from common.env import render_env

ROOT = Path(__file__).parent.parent
COMPOSE = yaml.safe_load((ROOT / "docker-compose.base.yml").read_text())


def make(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["make", *args], cwd=ROOT, capture_output=True, text=True)


def test_env_file_has_one_secret_per_provider():
    lines = render_env().splitlines()
    assert len(lines) == len(fixtures.SECRETS) == 7
    assert "HOOKSCOPE_SECRET_GITHUB=cookbook-github-secret" in lines
    assert all(line.startswith("HOOKSCOPE_SECRET_") and "=" in line for line in lines)


def test_compose_runs_hookscope_with_health_check_and_data_volume():
    service = COMPOSE["services"]["hookscope"]
    assert service["ports"] == ["${HOOKSCOPE_PORT:-8000}:8000"]
    assert service["environment"]["HOOKSCOPE_RULES"] == "/config/rules.json"
    assert "healthcheck" in service
    assert "hookscope-data:/data" in service["volumes"]
    assert "hookscope-data" in COMPOSE["volumes"]


def test_compose_keeps_secrets_out_of_the_file():
    assert "whsec_" not in (ROOT / "docker-compose.base.yml").read_text()
    assert service_env_has_no_secrets(COMPOSE["services"]["hookscope"])


def service_env_has_no_secrets(service: dict) -> bool:
    return not any(key.startswith("HOOKSCOPE_SECRET_") for key in service.get("environment", {}))


def test_default_rules_file_is_valid_empty_rules():
    import json

    assert json.loads((ROOT / "common" / "rules.empty.json").read_text()) == {"rules": []}


def test_make_help_lists_the_targets():
    result = make("help")
    assert result.returncode == 0
    for target in ("test", "lint", "up", "down", "env"):
        assert f"make {target}" in result.stdout


def test_make_test_rejects_an_unknown_recipe():
    result = make("test", "RECIPE=does-not-exist")
    assert result.returncode == 2
    assert "No such recipe: does-not-exist" in result.stderr


@pytest.mark.parametrize("target", ["up", "down", "logs"])
def test_compose_targets_reject_an_unknown_recipe(target):
    assert make(target, "RECIPE=does-not-exist").returncode == 2


def test_make_dry_run_includes_the_recipes_compose_file_only_when_it_exists(tmp_path):
    recipe = ROOT / "recipes" / "_demo_recipe"
    recipe.mkdir(parents=True)
    try:
        without = make("-n", "down", "RECIPE=_demo_recipe").stdout
        assert "recipes/_demo_recipe/docker-compose.yml" not in without
        (recipe / "docker-compose.yml").write_text("services: {}\n")
        with_file = make("-n", "down", "RECIPE=_demo_recipe").stdout
        assert "-f docker-compose.base.yml -f recipes/_demo_recipe/docker-compose.yml" in with_file
    finally:
        for path in recipe.glob("*"):
            path.unlink()
        recipe.rmdir()


def test_test_files_have_distinct_names_across_the_repository():
    """pytest imports test files by base name, so two ``test_handler.py`` files break collection."""
    names: dict[str, Path] = {}
    for path in sorted(ROOT.glob("**/test_*.py")):
        if ".git" in path.parts or "node_modules" in path.parts:
            continue
        assert path.name not in names, f"{path} and {names[path.name]} share a name: rename one"
        names[path.name] = path
