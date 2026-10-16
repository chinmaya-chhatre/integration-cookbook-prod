"""Print the finished recipes as a JSON list, for CI to build a test matrix from.

A recipe is any folder under recipes/ whose name does not start with an underscore (``_template``
is the skeleton, not a recipe). While a recipe is being built, in its seven commits, it carries an
empty ``.wip`` file and is left out, so its job does not fail for lack of tests it has not got yet;
the commit that adds the last part removes the file and the recipe joins the matrix.

    python scripts/list_recipes.py          # finished recipes (the CI matrix)
    python scripts/list_recipes.py --all    # including work in progress
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def recipes(root: Path = ROOT, include_wip: bool = False) -> list[str]:
    folder = root / "recipes"
    if not folder.is_dir():
        return []
    return sorted(
        p.name
        for p in folder.iterdir()
        if p.is_dir() and not p.name.startswith(("_", ".")) and (include_wip or not (p / ".wip").exists())
    )


if __name__ == "__main__":
    arguments = [a for a in sys.argv[1:] if not a.startswith("--")]
    print(json.dumps(recipes(Path(arguments[0]) if arguments else ROOT, include_wip="--all" in sys.argv)))
