"""File system tools: read, write, edit."""

import difflib
import errno
import os
from pathlib import Path
from typing import Any

from nanobot.agent.tools.base import Tool
from nanobot.logging import get_logger

audit_log = get_logger("nanobot.audit")


def _check_symlink_chain(path: Path, allowed_dir: Path) -> None:
    """Walk each component of *path* and verify every symlink resolves inside *allowed_dir*.

    Raises ``PermissionError`` if any intermediate symlink target escapes.
    """
    allowed = allowed_dir.resolve()
    current = Path(path.anchor) if path.anchor else Path(".")
    for part in path.parts[1:] if path.anchor else path.parts:
        current = current / part
        if current.is_symlink():
            target = current.resolve()
            try:
                target.relative_to(allowed)
            except ValueError:
                raise PermissionError(
                    f"Symlink {current} points to {target} which is outside allowed directory {allowed_dir}"
                )


def _safe_write(file_path: Path, content: str) -> None:
    """Write *content* to *file_path*, refusing to follow symlinks.

    Uses ``O_NOFOLLOW`` where available so the check-and-write is atomic.
    Falls back to an explicit ``is_symlink()`` guard on platforms without
    ``O_NOFOLLOW``.
    """
    o_nofollow = getattr(os, "O_NOFOLLOW", 0)

    if o_nofollow:
        try:
            fd = os.open(
                str(file_path),
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC | o_nofollow,
                0o644,
            )
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise PermissionError(
                    f"Refusing to write through symlink: {file_path}"
                ) from exc
            raise
        try:
            os.write(fd, content.encode("utf-8"))
        finally:
            os.close(fd)
    else:
        # Fallback: non-atomic but still catches obvious symlinks
        if file_path.is_symlink():
            raise PermissionError(f"Refusing to write through symlink: {file_path}")
        file_path.write_text(content, encoding="utf-8")


def _resolve_path(path: str, workspace: Path | None = None, allowed_dir: Path | None = None) -> Path:
    """Resolve path against workspace (if relative) and enforce directory restriction."""
    p = Path(path).expanduser()
    if not p.is_absolute() and workspace:
        p = workspace / p
    resolved = p.resolve()
    if allowed_dir:
        allowed_resolved = allowed_dir.resolve()
        try:
            resolved.relative_to(allowed_resolved)
        except ValueError:
            raise PermissionError(f"Path {path} is outside allowed directory {allowed_dir}")
        # Check every intermediate symlink in the original (pre-resolve) path
        _check_symlink_chain(p, allowed_dir)
    return resolved


class ReadFileTool(Tool):
    """Tool to read file contents."""

    def __init__(self, workspace: Path | None = None, allowed_dir: Path | None = None, audit_operations: bool = True):
        self._workspace = workspace
        self._allowed_dir = allowed_dir
        self._audit = audit_operations

    @property
    def name(self) -> str:
        return "read_file"

    @property
    def description(self) -> str:
        return "Read the contents of a file at the given path."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "The file path to read"
                }
            },
            "required": ["path"]
        }

    async def execute(self, path: str, **kwargs: Any) -> str:
        try:
            file_path = _resolve_path(path, self._workspace, self._allowed_dir)
            if not file_path.exists():
                return f"Error: File not found: {path}"
            if not file_path.is_file():
                return f"Error: Not a file: {path}"

            content = file_path.read_text(encoding="utf-8")
            if self._audit:
                audit_log.info("file_read", path=str(file_path))
            return content
        except PermissionError as e:
            if self._audit:
                audit_log.warning("file_read_blocked", path=path, reason=str(e))
            return f"Error: {e}"
        except Exception as e:
            return f"Error reading file: {str(e)}"


class WriteFileTool(Tool):
    """Tool to write content to a file."""

    def __init__(self, workspace: Path | None = None, allowed_dir: Path | None = None, audit_operations: bool = True):
        self._workspace = workspace
        self._allowed_dir = allowed_dir
        self._audit = audit_operations

    @property
    def name(self) -> str:
        return "write_file"

    @property
    def description(self) -> str:
        return "Write content to a file at the given path. Creates parent directories if needed."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "The file path to write to"
                },
                "content": {
                    "type": "string",
                    "description": "The content to write"
                }
            },
            "required": ["path", "content"]
        }

    async def execute(self, path: str, content: str, **kwargs: Any) -> str:
        try:
            file_path = _resolve_path(path, self._workspace, self._allowed_dir)
            # Check parent symlink chain before mkdir
            if self._allowed_dir:
                _check_symlink_chain(file_path.parent, self._allowed_dir)
            file_path.parent.mkdir(parents=True, exist_ok=True)
            _safe_write(file_path, content)
            if self._audit:
                audit_log.info("file_written", path=str(file_path))
            return f"Successfully wrote {len(content)} bytes to {file_path}"
        except PermissionError as e:
            if self._audit:
                audit_log.warning("file_write_blocked", path=path, reason=str(e))
            return f"Error: {e}"
        except Exception as e:
            return f"Error writing file: {str(e)}"


class EditFileTool(Tool):
    """Tool to edit a file by replacing text."""

    def __init__(self, workspace: Path | None = None, allowed_dir: Path | None = None, audit_operations: bool = True):
        self._workspace = workspace
        self._allowed_dir = allowed_dir
        self._audit = audit_operations

    @property
    def name(self) -> str:
        return "edit_file"

    @property
    def description(self) -> str:
        return "Edit a file by replacing old_text with new_text. The old_text must exist exactly in the file."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "The file path to edit"
                },
                "old_text": {
                    "type": "string",
                    "description": "The exact text to find and replace"
                },
                "new_text": {
                    "type": "string",
                    "description": "The text to replace with"
                }
            },
            "required": ["path", "old_text", "new_text"]
        }

    async def execute(self, path: str, old_text: str, new_text: str, **kwargs: Any) -> str:
        try:
            file_path = _resolve_path(path, self._workspace, self._allowed_dir)
            if not file_path.exists():
                return f"Error: File not found: {path}"

            content = file_path.read_text(encoding="utf-8")

            if old_text not in content:
                return self._not_found_message(old_text, content, path)

            # Count occurrences
            count = content.count(old_text)
            if count > 1:
                return f"Warning: old_text appears {count} times. Please provide more context to make it unique."

            new_content = content.replace(old_text, new_text, 1)
            _safe_write(file_path, new_content)
            if self._audit:
                audit_log.info("file_edited", path=str(file_path))
            return f"Successfully edited {file_path}"
        except PermissionError as e:
            if self._audit:
                audit_log.warning("file_edit_blocked", path=path, reason=str(e))
            return f"Error: {e}"
        except Exception as e:
            return f"Error editing file: {str(e)}"

    @staticmethod
    def _not_found_message(old_text: str, content: str, path: str) -> str:
        """Build a helpful error when old_text is not found."""
        lines = content.splitlines(keepends=True)
        old_lines = old_text.splitlines(keepends=True)
        window = len(old_lines)

        best_ratio, best_start = 0.0, 0
        for i in range(max(1, len(lines) - window + 1)):
            ratio = difflib.SequenceMatcher(None, old_lines, lines[i : i + window]).ratio()
            if ratio > best_ratio:
                best_ratio, best_start = ratio, i

        if best_ratio > 0.5:
            diff = "\n".join(difflib.unified_diff(
                old_lines, lines[best_start : best_start + window],
                fromfile="old_text (provided)", tofile=f"{path} (actual, line {best_start + 1})",
                lineterm="",
            ))
            return f"Error: old_text not found in {path}.\nBest match ({best_ratio:.0%} similar) at line {best_start + 1}:\n{diff}"
        return f"Error: old_text not found in {path}. No similar text found. Verify the file content."


class ListDirTool(Tool):
    """Tool to list directory contents."""

    def __init__(self, workspace: Path | None = None, allowed_dir: Path | None = None, audit_operations: bool = True):
        self._workspace = workspace
        self._allowed_dir = allowed_dir
        self._audit = audit_operations

    @property
    def name(self) -> str:
        return "list_dir"

    @property
    def description(self) -> str:
        return "List the contents of a directory."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "The directory path to list"
                }
            },
            "required": ["path"]
        }

    async def execute(self, path: str, **kwargs: Any) -> str:
        try:
            dir_path = _resolve_path(path, self._workspace, self._allowed_dir)
            if not dir_path.exists():
                return f"Error: Directory not found: {path}"
            if not dir_path.is_dir():
                return f"Error: Not a directory: {path}"

            items = []
            for item in sorted(dir_path.iterdir()):
                prefix = "📁 " if item.is_dir() else "📄 "
                items.append(f"{prefix}{item.name}")

            if self._audit:
                audit_log.info("dir_listed", path=str(dir_path))

            if not items:
                return f"Directory {path} is empty"

            return "\n".join(items)
        except PermissionError as e:
            if self._audit:
                audit_log.warning("dir_list_blocked", path=path, reason=str(e))
            return f"Error: {e}"
        except Exception as e:
            return f"Error listing directory: {str(e)}"
