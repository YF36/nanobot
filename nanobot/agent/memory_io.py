"""Memory file I/O helpers with atomic semantics."""

from __future__ import annotations

from pathlib import Path

from nanobot.utils.helpers import atomic_append_text, atomic_write_text


class MemoryIO:
    """Thin I/O adapter so memory pipeline can be tested independently."""

    @staticmethod
    def write_text(path: Path, content: str, *, encoding: str = "utf-8") -> None:
        atomic_write_text(path, content, encoding=encoding)

    @staticmethod
    def append_text(path: Path, content: str, *, encoding: str = "utf-8") -> None:
        atomic_append_text(path, content, encoding=encoding)

