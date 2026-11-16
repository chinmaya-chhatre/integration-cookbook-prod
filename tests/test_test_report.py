"""The release check must reject an incomplete or unsuccessful test run."""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "check_test_report.py"
spec = importlib.util.spec_from_file_location("check_test_report", SCRIPT)
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)


@pytest.mark.parametrize("root", ["testsuite", "testsuites"])
def test_successful_report(root, tmp_path):
    path = tmp_path / "results.xml"
    path.write_text(f'<{root}><testcase name="one"/><testcase name="two"/></{root}>')
    assert report.check_report(path) == 2


@pytest.mark.parametrize("result", ["skipped", "failure", "error"])
def test_incomplete_results_are_rejected(result, tmp_path):
    path = tmp_path / "results.xml"
    path.write_text(f"<testsuites><testsuite><testcase><{result}/></testcase></testsuite></testsuites>")
    with pytest.raises(ValueError, match=result):
        report.check_report(path)


@pytest.mark.parametrize("contents", ["<testsuites/>", "not xml", None])
def test_bad_or_missing_report_exits_nonzero(contents, tmp_path):
    path = tmp_path / "results.xml"
    if contents is not None:
        path.write_text(contents)
    result = subprocess.run([sys.executable, str(SCRIPT), str(path)], capture_output=True, text=True)
    assert result.returncode == 1
    assert "Incomplete test execution:" in result.stderr
