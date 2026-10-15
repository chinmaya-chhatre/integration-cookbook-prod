"""The CI workflow and the helper that feeds its recipe matrix."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).parent.parent
WORKFLOW = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text())

_spec = importlib.util.spec_from_file_location("list_recipes", ROOT / "scripts" / "list_recipes.py")
list_recipes = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(list_recipes)


def test_no_recipes_yet():
    assert list_recipes.recipes(ROOT) == []  # recipes/ only holds _template today


def test_recipes_are_the_non_underscore_folders(tmp_path):
    for name in ("stripe-invoice-paid-to-slack", "_template", ".hidden", "github-pr-to-jira"):
        (tmp_path / "recipes" / name).mkdir(parents=True)
    (tmp_path / "recipes" / "README.md").write_text("not a recipe")
    assert list_recipes.recipes(tmp_path) == ["github-pr-to-jira", "stripe-invoice-paid-to-slack"]


def test_missing_recipes_folder_is_an_empty_list(tmp_path):
    assert list_recipes.recipes(tmp_path) == []


def test_script_prints_json():
    out = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "list_recipes.py")],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(out.stdout) == []


def test_workflow_runs_on_push_and_pull_request():
    triggers = WORKFLOW.get("on", WORKFLOW.get(True))  # YAML turns a bare `on` into True
    assert set(triggers) == {"push", "pull_request"}


def test_workflow_runs_the_same_commands_a_contributor_does():
    steps = [step.get("run", "") for job in WORKFLOW["jobs"].values() for step in job["steps"]]
    for command in ("make lint", "make test", "make config PYTHON=python", "scripts/list_recipes.py"):
        assert any(command in step for step in steps), command


def test_recipe_matrix_is_built_from_discovery_and_skipped_when_empty():
    job = WORKFLOW["jobs"]["recipe"]
    assert job["needs"] == "discover"
    assert job["if"] == "needs.discover.outputs.recipes != '[]'"
    assert "fromJSON(needs.discover.outputs.recipes)" in job["strategy"]["matrix"]["recipe"]
    assert job["strategy"]["fail-fast"] is False
    assert any("make test RECIPE=${{ matrix.recipe }}" in s.get("run", "") for s in job["steps"])


def test_tests_run_on_both_python_versions():
    assert WORKFLOW["jobs"]["test"]["strategy"]["matrix"]["python-version"] == ["3.11", "3.12"]
