#!/usr/bin/env python3
"""Fail if any relative link in the repository's Markdown files is broken.

Checks every Markdown file that git tracks (plus new, unignored files) for:

- inline links and images: [text](path) and ![alt](path)
- reference definitions: [label]: path
- HTML attributes in Markdown: href="path" and src="path"

A link is broken when its target file or directory is not in the working
tree, or when its #fragment does not match a heading (GitHub slug rules) or
an explicit id/name anchor in the target Markdown file. External URLs
(http, https, mailto and similar) are skipped, not fetched. Links inside
fenced code blocks and inline code spans are ignored.

Usage: python3 tools/check_markdown_links.py
Exit status: 0 when every link resolves, 1 otherwise. Standard library only.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
MARKDOWN_SUFFIXES = {".md", ".markdown"}
EXTERNAL = re.compile(r"^(?:[a-z][a-z0-9+.-]*:|//)", re.IGNORECASE)

FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
INLINE_CODE = re.compile(r"(`+)(?:(?!\1).)+?\1")
INLINE_LINK = re.compile(
    r"!?\[(?P<text>(?:[^\[\]\\]|\\.|\[[^\[\]]*\])*)\]"  # [text], one level of nested brackets
    r"\(\s*(?P<target><[^>\n]*>|[^\s()]+(?:\([^\s()]*\)[^\s()]*)*)"  # (target
    r"(?:\s+(?:\"[^\"]*\"|'[^']*'|\([^)]*\)))?\s*\)"  # optional "title")
)
REFERENCE_DEF = re.compile(r"^ {0,3}\[([^\]]+)\]:\s*(<[^>]*>|\S+)")
HTML_ATTR = re.compile(r"""\b(?:href|src)\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.IGNORECASE)
HTML_ANCHOR = re.compile(r"""<[a-z][^>]*\b(?:id|name)\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.IGNORECASE)
ATX_HEADING = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*$")
SETEXT_UNDERLINE = re.compile(r"^ {0,3}(=+|-+)[ \t]*$")


def repository_files() -> set[str]:
    """Tracked files plus new unignored files that exist in the working tree."""
    out = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT, check=True, capture_output=True,
    ).stdout.decode("utf-8")
    return {p for p in out.split("\0") if p and (ROOT / p).is_file()}


def directories(files: set[str]) -> set[str]:
    dirs = {""}
    for path in files:
        parts = path.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            dirs.add("/".join(parts[:i]))
    return dirs


def prose_lines(text: str, strip_code: bool = True) -> list[tuple[int, str]]:
    """Return (line_number, line) pairs outside fenced code, inline code removed by default."""
    lines: list[tuple[int, str]] = []
    fence: str | None = None
    for number, line in enumerate(text.splitlines(), start=1):
        match = FENCE.match(line)
        if fence is None and match:
            fence = match.group(1)[0] * len(match.group(1))
            continue
        if fence is not None:
            if match and match.group(1)[0] == fence[0] and len(match.group(1)) >= len(fence):
                fence = None
            continue
        lines.append((number, INLINE_CODE.sub("", line) if strip_code else line))
    return lines


def heading_text(raw: str) -> str:
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", raw)  # links and images keep their text
    text = re.sub(r"<[^>]+>", "", text)  # inline HTML tags
    text = text.replace("`", "")
    text = re.sub(r"(\*\*|__|\*|_|~~)(?=\S)(.+?)(?<=\S)\1", r"\2", text)
    return text.strip()


def github_slug(text: str) -> str:
    text = heading_text(text).lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def anchors(text: str) -> set[str]:
    found: set[str] = set()
    counts: dict[str, int] = {}
    previous = ""
    for _, line in prose_lines(text, strip_code=False):
        level_text = None
        match = ATX_HEADING.match(line)
        if match:
            level_text = match.group(2)
        elif (previous.strip() and SETEXT_UNDERLINE.match(line)
              and not previous.lstrip().startswith(("-", "*", "|", ">"))):
            level_text = previous.strip()
        if level_text is not None:
            slug = github_slug(level_text)
            n = counts.get(slug, 0)
            counts[slug] = n + 1
            found.add(slug if n == 0 else f"{slug}-{n}")
        for attr in HTML_ANCHOR.finditer(line):
            found.add(attr.group(1) or attr.group(2) or "")
        previous = line
    return found


def inline_targets(fragment: str) -> list[str]:
    """Targets of inline links and images, including an image nested in a link's text."""
    found: list[str] = []
    for match in INLINE_LINK.finditer(fragment):
        found.append(match.group("target").strip("<>"))
        found.extend(inline_targets(match.group("text")))
    return found


def link_targets(text: str) -> list[tuple[int, str]]:
    targets: list[tuple[int, str]] = []
    for number, line in prose_lines(text):
        targets.extend((number, target) for target in inline_targets(line))
        definition = REFERENCE_DEF.match(line)
        if definition:
            targets.append((number, definition.group(2).strip("<>")))
        for attr in HTML_ATTR.finditer(line):
            targets.append((number, attr.group(1) if attr.group(1) is not None else attr.group(2)))
    return targets


def main() -> int:
    files = repository_files()
    dirs = directories(files)
    markdown = sorted(p for p in files if Path(p).suffix.lower() in MARKDOWN_SUFFIXES)
    anchor_cache: dict[str, set[str]] = {}
    broken: list[str] = []
    checked = 0

    def anchors_for(path: str) -> set[str]:
        if path not in anchor_cache:
            anchor_cache[path] = anchors((ROOT / path).read_text(encoding="utf-8"))
        return anchor_cache[path]

    for source in markdown:
        text = (ROOT / source).read_text(encoding="utf-8")
        base = Path(source).parent
        for number, target in link_targets(text):
            if not target or EXTERNAL.match(target):
                continue
            checked += 1
            path_part, _, fragment = target.partition("#")
            path_part = unquote(path_part.split("?", 1)[0])
            if path_part:
                joined = Path(path_part.lstrip("/")) if path_part.startswith("/") else base / path_part
                parts: list[str] = []
                escaped = False
                for part in joined.as_posix().split("/"):
                    if part in ("", "."):
                        continue
                    if part == "..":
                        if not parts:
                            escaped = True
                            break
                        parts.pop()
                    else:
                        parts.append(part)
                resolved = "/".join(parts)
                if escaped or (resolved not in files and resolved not in dirs):
                    broken.append(f"{source}:{number}: missing target {target}")
                    continue
            else:
                resolved = source
            if fragment and resolved in files and Path(resolved).suffix.lower() in MARKDOWN_SUFFIXES:
                if unquote(fragment).lower() not in {a.lower() for a in anchors_for(resolved)}:
                    broken.append(f"{source}:{number}: missing anchor #{fragment} in {resolved}")

    for line in broken:
        print(line)
    print(f"checked {checked} relative links in {len(markdown)} Markdown files: {len(broken)} broken")
    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main())
