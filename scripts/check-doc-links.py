#!/usr/bin/env python3
"""Check that every relative Markdown link resolves to a file and a heading.

Scans ``*.md``, ``*.html``, ``*.yml``/``*.yaml``, ``*.sh``, and ``*.py`` files
for Markdown inline links (``[text]`` immediately followed by a parenthesised
``path#anchor`` target), resolves ``path`` relative to the linking file, and
requires the target file to exist. When the target is a Markdown file and the
link names an anchor, the anchor must match a heading in that file
under GitHub's rules (lower-case, punctuation stripped, spaces to hyphens,
``-1``/``-2`` suffixes for repeated headings). A heading whose anchor repeats
inside one file is reported too, because the duplicate silently shifts every
later link to a ``-N`` suffix.

Exit status is non-zero when any link or heading check fails.
"""

from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
EXTENSIONS = {".md", ".html", ".yml", ".yaml", ".sh", ".py"}
EXCLUDED_PARTS = {
    ".git", "node_modules", "build", ".builds", ".build-work", "test-results",
    "bin", "obj", ".cache", ".je", ".state", "artifacts",
}
LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")
FENCE_RE = re.compile(r"^\s*(```|~~~)")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
INLINE_CODE_RE = re.compile(r"`([^`]*)`")
INLINE_LINK_RE = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
EMPHASIS_RE = re.compile(r"[*_]{1,3}([^*_]+)[*_]{1,3}")
ANCHOR_STRIP_RE = re.compile(r"[^\w\- ]", re.UNICODE)


def repo_files() -> list[pathlib.Path]:
    found: list[pathlib.Path] = []
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in EXTENSIONS:
            continue
        if EXCLUDED_PARTS & set(path.relative_to(ROOT).parts):
            continue
        found.append(path)
    return found


def strip_fences(text: str) -> list[str]:
    """Return the file's lines with fenced code blocks blanked out."""
    lines: list[str] = []
    fence: str | None = None
    for line in text.splitlines():
        match = FENCE_RE.match(line)
        if match:
            marker = match.group(1)
            if fence is None:
                fence = marker
            elif marker == fence:
                fence = None
            lines.append("")
            continue
        lines.append("" if fence else line)
    return lines


def github_anchor(heading: str) -> str:
    text = INLINE_LINK_RE.sub(r"\1", heading)
    text = INLINE_CODE_RE.sub(r"\1", text)
    text = EMPHASIS_RE.sub(r"\1", text)
    text = text.strip().lower()
    text = ANCHOR_STRIP_RE.sub("", text)
    return text.replace(" ", "-")


def heading_anchors(path: pathlib.Path) -> tuple[set[str], list[str]]:
    """Return (anchors, duplicate-heading errors) for a Markdown file."""
    anchors: set[str] = set()
    counts: dict[str, int] = {}
    errors: list[str] = []
    for number, line in enumerate(strip_fences(path.read_text(encoding="utf-8")), 1):
        match = HEADING_RE.match(line)
        if not match:
            continue
        base = github_anchor(match.group(2))
        seen = counts.get(base, 0)
        counts[base] = seen + 1
        if seen:
            errors.append(
                f"{path.relative_to(ROOT)}:{number}: duplicate heading anchor "
                f"#{base} (GitHub will publish it as #{base}-{seen})"
            )
            anchors.add(f"{base}-{seen}")
        else:
            anchors.add(base)
    return anchors, errors


def main() -> int:
    files = repo_files()
    anchor_cache: dict[pathlib.Path, set[str]] = {}
    errors: list[str] = []
    checked = 0

    for path in files:
        if path.suffix.lower() == ".md":
            anchors, duplicate_errors = heading_anchors(path)
            anchor_cache[path] = anchors
            errors.extend(duplicate_errors)

    for path in files:
        text = path.read_text(encoding="utf-8", errors="replace")
        lines = strip_fences(text) if path.suffix.lower() == ".md" else text.splitlines()
        for number, line in enumerate(lines, 1):
            for target in LINK_RE.findall(line):
                if SCHEME_RE.match(target) or target.startswith("//"):
                    continue
                if target.startswith("<") and target.endswith(">"):
                    target = target[1:-1]
                file_part, _, anchor = target.partition("#")
                location = f"{path.relative_to(ROOT)}:{number}"
                if file_part:
                    resolved = (path.parent / file_part).resolve()
                else:
                    resolved = path.resolve()
                checked += 1
                if not resolved.exists():
                    errors.append(f"{location}: missing link target {target}")
                    continue
                if not anchor:
                    continue
                if resolved.suffix.lower() != ".md" or resolved.is_dir():
                    continue
                if resolved not in anchor_cache:
                    anchor_cache[resolved], _ = heading_anchors(resolved)
                if anchor.lower() not in anchor_cache[resolved]:
                    errors.append(
                        f"{location}: missing anchor #{anchor} in "
                        f"{resolved.relative_to(ROOT) if resolved.is_relative_to(ROOT) else resolved}"
                    )

    for error in errors:
        print(error, file=sys.stderr)
    print(
        f"check-doc-links: {checked} relative links across {len(files)} files, "
        f"{len(errors)} problem(s)"
    )
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
