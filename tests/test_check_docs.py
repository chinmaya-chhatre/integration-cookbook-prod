import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
_spec = importlib.util.spec_from_file_location("check_docs", ROOT / "scripts" / "check_docs.py")
check_docs = importlib.util.module_from_spec(_spec)
sys.modules["check_docs"] = check_docs  # dataclasses look their module up here
_spec.loader.exec_module(check_docs)


def write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def messages(problems) -> list[str]:
    return [p.message for p in problems]


# ----------------------------------------------------------------------------- links


def test_the_repos_own_docs_pass():
    for path in check_docs.markdown_files():
        assert not check_docs.check_links(path), path
        assert not check_docs.check_diagrams(path), path


def test_relative_links_to_files_and_folders_resolve(tmp_path):
    write(tmp_path, "docs/guide.md", "# Guide\n")
    (tmp_path / "pkg").mkdir()
    page = write(
        tmp_path, "README.md", "[guide](docs/guide.md) and [pkg](pkg) and [self](#title)\n\n# Title\n"
    )
    assert check_docs.check_links(page) == []


def test_a_missing_file_is_reported_with_its_line(tmp_path):
    page = write(tmp_path, "README.md", "intro\n\nsee [gone](docs/gone.md)\n")
    [problem] = check_docs.check_links(page)
    assert problem.line == 3
    assert "docs/gone.md" in problem.message


def test_anchors_must_match_a_heading_in_the_target(tmp_path):
    write(tmp_path, "guide.md", "# Guide\n\n## Set up (step 1)\n\n## Set up (step 1)\n")
    page = write(
        tmp_path,
        "README.md",
        "[ok](guide.md#set-up-step-1) [dup](guide.md#set-up-step-1-1) [bad](guide.md#nope)\n",
    )
    assert messages(check_docs.check_links(page)) == ["broken anchor: guide.md#nope (no such heading)"]


def test_links_in_code_are_examples_not_links(tmp_path):
    page = write(
        tmp_path, "README.md", "Use `[x](missing.md)` like so:\n\n```md\n[y](also-missing.md)\n```\n"
    )
    assert check_docs.check_links(page) == []


def test_images_and_mailto_and_external_links(tmp_path):
    page = write(
        tmp_path, "README.md", "![logo](missing.png) [mail](mailto:a@b.co) [web](https://example.com/x)\n"
    )
    external: list = []
    assert messages(check_docs.check_links(page, external)) == [
        "broken link: missing.png (no such file or folder)"
    ]
    assert [url for _, _, url in external] == ["https://example.com/x"]


def test_slug_follows_githubs_rules():
    assert check_docs.slug("What every recipe contains") == "what-every-recipe-contains"
    assert check_docs.slug("`make test` & friends!") == "make-test--friends"
    assert check_docs.slug("2. Config") == "2-config"


def test_external_links_report_failures(monkeypatch):
    results = {"https://ok.test": None, "https://gone.test": "HTTP 404"}
    monkeypatch.setattr(check_docs, "_fetch_error", lambda url, timeout: results[url])
    links = [(ROOT / "README.md", 1, "https://ok.test"), (ROOT / "README.md", 2, "https://gone.test")]
    [problem] = check_docs.check_external(links)
    assert problem.line == 2 and "HTTP 404" in problem.message


# -------------------------------------------------------------------------- diagrams


def diagram(text: str) -> list[str]:
    return check_docs.check_mermaid(text.strip("\n").splitlines())


def test_a_good_flowchart_and_sequence_pass():
    assert (
        diagram('flowchart LR\n    A["x<br/>y"] --> B\n    subgraph s\n      B --> C(round)\n    end') == []
    )
    sequence = (
        "sequenceDiagram\n    participant P as <provider>\n    participant H\n    P->>H: hi\n    H-->>P: ok"
    )
    assert diagram(sequence) == []


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("", "empty diagram"),
        ("flowchrt LR\n A --> B", "unknown diagram type 'flowchrt'"),
        ("flowchart LR\n A[open --> B", "unbalanced []"),
        ('flowchart LR\n A["open --> B', "unbalanced quote"),
        ("flowchart LR\n subgraph s\n A --> B", "'subgraph' without a matching 'end'"),
        ("flowchart LR\n A --> B\n end", "'end' without a matching 'subgraph'"),
        ("sequenceDiagram\n participant A\n participant B\n A->>C: hi", "'C' is not a declared participant"),
        (
            "sequenceDiagram\n participant A\n participant B\n loop forever\n A->>B: hi",
            "is not closed with 'end'",
        ),
    ],
)
def test_broken_diagrams_are_caught(source, expected):
    assert any(expected in problem for problem in diagram(source)), diagram(source)


def test_brackets_inside_quoted_labels_do_not_count():
    assert diagram('flowchart TD\n A["f(x] ["] --> B') == []


def test_comments_and_front_matter_are_ignored():
    assert diagram("---\ntitle: demo\n---\n%% a comment\nflowchart TD\n A --> B") == []


def test_unclosed_fences_and_bad_diagrams_in_files(tmp_path):
    page = write(tmp_path, "a.md", "```mermaid\nflowchart LR\n A[x --> B\n```\n\n```python\nnever closed\n")
    found = messages(check_docs.check_diagrams(page))
    assert any("unbalanced []" in m for m in found)
    assert "code fence is never closed" in found


def test_the_template_diagrams_are_checked():
    template = ROOT / "recipes" / "_template" / "README.md"
    _, blocks, _ = check_docs.split_blocks(template.read_text())
    assert [language for _, language, _ in blocks].count("mermaid") == 2
