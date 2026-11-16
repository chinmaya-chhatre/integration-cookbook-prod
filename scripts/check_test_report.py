"""Reject missing, empty, skipped or unsuccessful pytest JUnit reports."""

import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def check_report(path: Path) -> int:
    root = ET.parse(path).getroot()
    cases = list(root.iter("testcase"))
    if not cases:
        raise ValueError("test report contains no executed test cases")
    for kind in ("skipped", "failure", "error"):
        count = len(list(root.iter(kind)))
        if count:
            raise ValueError(f"test report contains {count} {kind} result(s)")
    return len(cases)


if __name__ == "__main__":
    try:
        count = check_report(Path(sys.argv[1]))
    except (IndexError, OSError, ET.ParseError, ValueError) as exc:
        print(f"Incomplete test execution: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(f"Verified {count} test cases with no skips, failures or errors")
