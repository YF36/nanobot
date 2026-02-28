"""Memory file I/O helpers with atomic semantics."""

from __future__ import annotations

import re
from pathlib import Path

from nanobot.utils.helpers import atomic_append_text, atomic_write_text

_H2_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$")


class MemoryIO:
    """Thin I/O adapter so memory pipeline can be tested independently."""

    @staticmethod
    def write_text(path: Path, content: str, *, encoding: str = "utf-8") -> None:
        atomic_write_text(path, content, encoding=encoding)

    @staticmethod
    def append_text(path: Path, content: str, *, encoding: str = "utf-8") -> None:
        atomic_append_text(path, content, encoding=encoding)


def parse_markdown_h2_sections(text: str) -> tuple[list[str], list[tuple[str, list[str]]]]:
    """Parse markdown text into preamble lines and a list of (heading, body_lines) tuples.

    Splits on ``## `` headings. Lines before the first heading go into preamble.
    """
    preamble: list[str] = []
    section_map: dict[str, list[str]] = {}
    section_order: list[str] = []
    current_heading: str | None = None
    for raw_line in text.splitlines():
        m = _H2_HEADING_RE.match(raw_line)
        if m:
            current_heading = m.group(1).strip()
            if current_heading not in section_map:
                section_map[current_heading] = []
                section_order.append(current_heading)
            continue
        if current_heading is None:
            preamble.append(raw_line)
        else:
            section_map[current_heading].append(raw_line)
    sections = [(heading, section_map[heading]) for heading in section_order]
    return preamble, sections


def render_markdown_h2_sections(
    preamble: list[str],
    sections: list[tuple[str, list[str]]],
) -> str:
    """Render preamble and (heading, body_lines) tuples back to markdown text."""
    parts: list[str] = []
    preamble_text = "\n".join(preamble).strip("\n")
    if preamble_text:
        parts.append(preamble_text)
    for heading, lines in sections:
        body = "\n".join(lines).strip("\n")
        if body:
            parts.append(f"## {heading}\n{body}")
        else:
            parts.append(f"## {heading}")
    rendered = "\n\n".join(parts).rstrip()
    return rendered + ("\n" if rendered else "")

