"""Print the recipes as a JSON list, for CI to build a test matrix from.

A recipe is any folder under recipes/ whose name does not start with an underscore (``_template``
is the skeleton, not a recipe). The list is empty until the first recipe lands.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def recipes(root: Path = ROOT) -> list[str]:
    folder = root / "recipes"
    if not folder.is_dir():
        return []
    return sorted(p.name for p in folder.iterdir() if p.is_dir() and not p.name.startswith(("_", ".")))


if __name__ == "__main__":
    print(json.dumps(recipes(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT)))
