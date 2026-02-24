"""File system tools: read, write, edit."""

import difflib
import errno
import os
from pathlib import Path
from typing import Any

from nanobot.agent.tools.base import Tool, ToolExecutionResult
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


def _first_changed_line(old_content: str, new_content: str) -> int | None:
    """Return 1-based line number of the first detected change."""
    old_lines = old_content.splitlines()
    new_lines = new_content.splitlines()
    for idx, (old_line, new_line) in enumerate(zip(old_lines, new_lines), start=1):
        if old_line != new_line:
            return idx
    if len(old_lines) != len(new_lines):
        return min(len(old_lines), len(new_lines)) + 1
    return None


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
                },
                "offset": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Optional 1-based line number to start reading from"
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Optional maximum number of lines to return"
                },
            },
            "required": ["path"]
        }

    async def execute(
        self,
        path: str,
        offset: int | None = None,
        limit: int | None = None,
        **kwargs: Any,
    ) -> str | ToolExecutionResult:
        try:
            file_path = _resolve_path(path, self._workspace, self._allowed_dir)
            if not file_path.exists():
                return f"Error: File not found: {path}"
            if not file_path.is_file():
                return f"Error: Not a file: {path}"

            content = file_path.read_text(encoding="utf-8")
            total_lines = len(content.splitlines()) if content else 0
            paged = False
            page_start_line: int | None = None
            page_end_line: int | None = None
            if offset is not None or limit is not None:
                paged = True
                if not content:
                    result = ToolExecutionResult(
                        text="",
                        details={
                            "op": "read_file",
                            "path": str(file_path),
                            "requested_path": path,
                            "bytes_read": 0,
                            "total_lines": 0,
                            "paged": True,
                            "offset": offset,
                            "limit": limit,
                        },
                    )
                    if self._audit:
                        audit_log.info("file_read", path=str(file_path))
                    return result
                lines = content.splitlines()
                start = (offset or 1) - 1
                if start >= len(lines):
                    return f"Error: Offset {offset} is beyond end of file ({len(lines)} lines total)"
                end = len(lines) if limit is None else min(start + limit, len(lines))
                page_start_line, page_end_line = start + 1, end
                selected = "\n".join(lines[start:end])
                if end < len(lines):
                    selected += (
                        f"\n\n[Showing lines {start + 1}-{end} of {len(lines)}. "
                        f"Use offset={end + 1} to continue.]"
                    )
                content = selected
            if self._audit:
                audit_log.info("file_read", path=str(file_path))
            return ToolExecutionResult(
                text=content,
                details={
                    "op": "read_file",
                    "path": str(file_path),
                    "requested_path": path,
                    "bytes_read": len(content.encode("utf-8")),
                    "total_lines": total_lines,
                    "paged": paged,
                    "offset": offset,
                    "limit": limit,
                    "page_start_line": page_start_line,
                    "page_end_line": page_end_line,
                },
            )
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

    async def execute(self, path: str, content: str, **kwargs: Any) -> str | ToolExecutionResult:
        try:
            file_path = _resolve_path(path, self._workspace, self._allowed_dir)
            existed_before = file_path.exists()
            # Check parent symlink chain before mkdir
            if self._allowed_dir:
                _check_symlink_chain(file_path.parent, self._allowed_dir)
            file_path.parent.mkdir(parents=True, exist_ok=True)
            _safe_write(file_path, content)
            if self._audit:
                audit_log.info("file_written", path=str(file_path))
            return ToolExecutionResult(
                text=f"Successfully wrote {len(content)} bytes to {file_path}",
                details={
                    "op": "write_file",
                    "path": str(file_path),
                    "requested_path": path,
                    "bytes_written": len(content.encode("utf-8")),
                    "file_existed": existed_before,
                },
            )
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

    async def execute(
        self,
        path: str,
        old_text: str,
        new_text: str,
        **kwargs: Any,
    ) -> str | ToolExecutionResult:
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
            diff_text = "\n".join(
                difflib.unified_diff(
                    content.splitlines(),
                    new_content.splitlines(),
                    fromfile=f"{path} (before)",
                    tofile=f"{path} (after)",
                    lineterm="",
                )
            )
            first_changed = _first_changed_line(content, new_content)
            _safe_write(file_path, new_content)
            if self._audit:
                audit_log.info("file_edited", path=str(file_path))
            diff_truncated = False
            if len(diff_text) > 4000:
                diff_text = diff_text[:4000] + "\n... (diff truncated)"
                diff_truncated = True
            line_hint = f" (first change at line {first_changed})" if first_changed else ""
            text = f"Successfully edited {file_path}{line_hint}\n\nDiff:\n{diff_text}"
            return ToolExecutionResult(
                text=text,
                details={
                    "op": "edit_file",
                    "path": str(file_path),
                    "requested_path": path,
                    "first_changed_line": first_changed,
                    "replacement_count": 1,
                    "diff_preview": diff_text,
                    "diff_truncated": diff_truncated,
                    "old_text_len": len(old_text),
                    "new_text_len": len(new_text),
                },
            )
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

    async def execute(self, path: str, **kwargs: Any) -> str | ToolExecutionResult:
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
                return ToolExecutionResult(
                    text=f"Directory {path} is empty",
                    details={
                        "op": "list_dir",
                        "path": str(dir_path),
                        "requested_path": path,
                        "item_count": 0,
                        "has_directories": False,
                    },
                )

            return ToolExecutionResult(
                text="\n".join(items),
                details={
                    "op": "list_dir",
                    "path": str(dir_path),
                    "requested_path": path,
                    "item_count": len(items),
                    "has_directories": any(item.is_dir() for item in dir_path.iterdir()),
                },
            )
        except PermissionError as e:
            if self._audit:
                audit_log.warning("dir_list_blocked", path=path, reason=str(e))
            return f"Error: {e}"
        except Exception as e:
            return f"Error listing directory: {str(e)}"
