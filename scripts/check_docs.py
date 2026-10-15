"""Check the Markdown: links must resolve and Mermaid diagrams must be well formed.

    python scripts/check_docs.py              # relative links, #anchors and diagram syntax (blocking in CI)
    python scripts/check_docs.py --external   # also request every http(s) link (flaky by nature)

Links: a relative link must point at a file or folder that exists, and a ``#fragment`` must match a
heading in the target file. Diagrams: every ```mermaid block is checked for a known diagram type,
balanced brackets and quotes, matched ``subgraph``/``end`` and ``alt``/``end`` pairs, and (in a
sequence diagram that declares participants) arrows between declared participants only. That
catches the typos that break rendering; it is a syntax check, not a full Mermaid parse.
"""

from __future__ import annotations

import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", ".pytest_cache", ".ruff_cache"}

LINK = re.compile(r"(?<!!)\[(?:[^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
IMAGE = re.compile(r"!\[(?:[^\]]*)\]\(([^)\s]+)\)")
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
FENCE = re.compile(r"^(\s*)(```+|~~~+)\s*([\w-]*)")

DIAGRAM_TYPES = (
    "flowchart", "graph", "sequenceDiagram", "classDiagram", "stateDiagram-v2", "stateDiagram",
    "erDiagram", "gantt", "pie", "journey", "gitGraph", "mindmap", "timeline", "quadrantChart",
    "requirementDiagram", "C4Context", "sankey-beta", "xychart-beta", "block-beta",
)  # fmt: skip
SEQUENCE_BLOCKS = ("alt", "opt", "loop", "par", "critical", "break", "rect")


@dataclass(frozen=True)
class Problem:
    path: Path
    line: int
    message: str

    def __str__(self) -> str:
        shown = (
            self.path.relative_to(ROOT)
            if self.path.is_absolute() and ROOT in self.path.parents
            else self.path
        )
        return f"{shown}:{self.line}: {self.message}"


def markdown_files(root: Path = ROOT) -> list[Path]:
    return sorted(p for p in root.rglob("*.md") if not SKIP_DIRS & set(p.relative_to(root).parts))


def slug(heading: str) -> str:
    """GitHub's heading anchor: lower-case, punctuation dropped, spaces to hyphens."""
    text = re.sub(r"[`*_]|<[^>]+>", "", heading).strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def split_blocks(text: str) -> tuple[list[tuple[int, str]], list[tuple[int, str, list[str]]], int | None]:
    """Return (prose lines, fenced blocks, line of an unclosed fence). Prose excludes code."""
    prose: list[tuple[int, str]] = []
    blocks: list[tuple[int, str, list[str]]] = []
    open_block: tuple[int, str, str, list[str]] | None = None
    for number, line in enumerate(text.splitlines(), 1):
        fence = FENCE.match(line)
        if open_block is None:
            if fence:
                open_block = (number, fence.group(2)[0], fence.group(3), [])
            else:
                prose.append((number, line))
        elif fence and fence.group(2)[0] == open_block[1] and not fence.group(3):
            blocks.append((open_block[0], open_block[2], open_block[3]))
            open_block = None
        else:
            open_block[3].append(line)
    return prose, blocks, open_block[0] if open_block else None


def anchors(path: Path) -> set[str]:
    prose, _, _ = split_blocks(path.read_text(encoding="utf-8"))
    seen: dict[str, int] = {}
    result = set()
    for _, line in prose:
        match = HEADING.match(line)
        if match:
            base = slug(match.group(2))
            count = seen.get(base, 0)
            result.add(base if count == 0 else f"{base}-{count}")
            seen[base] = count + 1
    return result


def check_links(path: Path, external: list[tuple[Path, int, str]] | None = None) -> list[Problem]:
    problems: list[Problem] = []
    prose, _, _ = split_blocks(path.read_text(encoding="utf-8"))
    for number, line in prose:
        line = re.sub(r"`[^`]*`", "", line)  # links inside inline code are examples, not links
        for target in [*LINK.findall(line), *IMAGE.findall(line)]:
            if target.startswith(("http://", "https://")):
                if external is not None:
                    external.append((path, number, target))
                continue
            if target.startswith(("mailto:", "tel:")):
                continue
            file_part, _, fragment = target.partition("#")
            destination = path if not file_part else (path.parent / file_part).resolve()
            if not destination.exists():
                problems.append(Problem(path, number, f"broken link: {target} (no such file or folder)"))
            elif fragment and destination.is_file() and destination.suffix == ".md":
                if fragment.lower() not in anchors(destination):
                    problems.append(Problem(path, number, f"broken anchor: {target} (no such heading)"))
    return problems


def check_external(links: list[tuple[Path, int, str]], timeout: float = 10.0) -> list[Problem]:
    problems = []
    cache: dict[str, str | None] = {}
    for path, number, url in links:
        if url not in cache:
            cache[url] = _fetch_error(url, timeout)
        if cache[url]:
            problems.append(Problem(path, number, f"{url}: {cache[url]}"))
    return problems


def _fetch_error(url: str, timeout: float) -> str | None:
    request = urllib.request.Request(url, method="GET", headers={"User-Agent": "cookbook-link-check"})
    try:
        with urllib.request.urlopen(request, timeout=timeout):
            return None
    except urllib.error.HTTPError as error:
        return f"HTTP {error.code}"
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        return f"unreachable ({error})"


def check_mermaid(source: list[str]) -> list[str]:
    """Return the problems found in one Mermaid diagram (empty when it looks well formed)."""
    lines = [line for line in source if line.strip() and not line.strip().startswith("%%")]
    if lines and lines[0].strip() == "---":  # YAML front matter
        end = next((i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
        lines = lines[end + 1 :] if end else lines
    if not lines:
        return ["empty diagram"]
    kind = lines[0].split()[0]
    if kind not in DIAGRAM_TYPES:
        examples = ", ".join(DIAGRAM_TYPES[:6])
        return [f"unknown diagram type '{kind}' (first line must start with one of {examples}, ...)"]
    problems = []
    for line in lines:
        if line.count('"') % 2:
            problems.append(f"unbalanced quote in: {line.strip()}")
    body = lines[1:]
    if kind in ("flowchart", "graph"):
        problems += _check_flowchart(body)
    elif kind == "sequenceDiagram":
        problems += _check_sequence(body)
    return problems


def _check_flowchart(body: list[str]) -> list[str]:
    problems = []
    depth = 0
    for line in body:
        text = re.sub(r'"[^"]*"', '""', line)  # brackets inside quoted labels do not count
        stripped = text.strip()
        if re.match(r"subgraph\b", stripped):
            depth += 1
        elif stripped == "end":
            depth -= 1
            if depth < 0:
                problems.append("'end' without a matching 'subgraph'")
                depth = 0
        for open_, close in ("[]", "()", "{}"):
            if text.count(open_) != text.count(close):
                problems.append(f"unbalanced {open_}{close} in: {line.strip()}")
    if depth > 0:
        problems.append("'subgraph' without a matching 'end'")
    return problems


def _check_sequence(body: list[str]) -> list[str]:
    problems = []
    declared: set[str] = set()
    depth = 0
    arrow = re.compile(r"^\s*([^\s\-+>]+?)\s*(?:-->>|->>|--x|-x|--\)|-\)|-->|->)[+-]?\s*([^\s:]+?)\s*:")
    for line in body:
        stripped = line.strip()
        declaration = re.match(r"(?:participant|actor)\s+(\S+)(?:\s+as\s+.*)?$", stripped)
        if declaration:
            declared.add(declaration.group(1))
            continue
        word = stripped.split()[0] if stripped.split() else ""
        if word in SEQUENCE_BLOCKS:
            depth += 1
        elif word == "end":
            depth -= 1
            if depth < 0:
                problems.append("'end' without a matching block")
                depth = 0
        match = arrow.match(line)
        if match and declared:
            for who in match.groups():
                if who not in declared:
                    problems.append(f"'{who}' is not a declared participant in: {stripped}")
    if depth > 0:
        problems.append("a block (alt/opt/loop/...) is not closed with 'end'")
    return problems


def check_diagrams(path: Path) -> list[Problem]:
    _, blocks, unclosed = split_blocks(path.read_text(encoding="utf-8"))
    problems = []
    if unclosed:
        problems.append(Problem(path, unclosed, "code fence is never closed"))
    for start, language, source in blocks:
        if language == "mermaid":
            problems += [Problem(path, start, message) for message in check_mermaid(source)]
    return problems


def main(argv: list[str]) -> int:
    external: list[tuple[Path, int, str]] | None = [] if "--external" in argv else None
    files = markdown_files()
    problems: list[Problem] = []
    for path in files:
        problems += check_links(path, external)
        problems += check_diagrams(path)
    if external is not None:
        problems += check_external(external)
    for problem in problems:
        print(problem)
    diagrams = sum(
        1
        for path in files
        for _, language, _ in split_blocks(path.read_text(encoding="utf-8"))[1]
        if language == "mermaid"
    )
    print(f"checked {len(files)} Markdown files and {diagrams} diagrams: {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
