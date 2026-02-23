"""Security tests for filesystem tool hardening.

Covers:
- Symlink escape detection (_check_symlink_chain)
- _safe_write refusing symlinks
- Path traversal blocking
- Valid internal symlinks working normally
- Audit logging
- Normal read/write/edit/list operations unaffected
"""

import os
from pathlib import Path

import pytest

from nanobot.agent.tools.filesystem import (
    _check_symlink_chain,
    _safe_write,
    _resolve_path,
    ReadFileTool,
    WriteFileTool,
    EditFileTool,
    ListDirTool,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def workspace(tmp_path):
    """Create a workspace directory with a sample file."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "hello.txt").write_text("hello world", encoding="utf-8")
    (ws / "subdir").mkdir()
    (ws / "subdir" / "nested.txt").write_text("nested", encoding="utf-8")
    return ws


@pytest.fixture
def outside_dir(tmp_path):
    """Create a directory outside the workspace."""
    out = tmp_path / "outside"
    out.mkdir()
    (out / "secret.txt").write_text("secret data", encoding="utf-8")
    return out


# ---------------------------------------------------------------------------
# _check_symlink_chain tests
# ---------------------------------------------------------------------------

class TestCheckSymlinkChain:
    def test_no_symlinks(self, workspace):
        """Normal path with no symlinks should pass."""
        _check_symlink_chain(workspace / "subdir" / "nested.txt", workspace)

    def test_internal_symlink_allowed(self, workspace):
        """Symlink pointing within workspace should pass."""
        link = workspace / "link_to_subdir"
        link.symlink_to(workspace / "subdir")
        _check_symlink_chain(link / "nested.txt", workspace)

    def test_external_symlink_blocked(self, workspace, outside_dir):
        """Symlink pointing outside workspace should raise PermissionError."""
        link = workspace / "escape"
        link.symlink_to(outside_dir)
        with pytest.raises(PermissionError, match="outside allowed directory"):
            _check_symlink_chain(link / "secret.txt", workspace)

    def test_nested_symlink_escape(self, workspace, outside_dir):
        """Symlink in a subdirectory pointing outside should be caught."""
        link = workspace / "subdir" / "sneaky"
        link.symlink_to(outside_dir)
        with pytest.raises(PermissionError, match="outside allowed directory"):
            _check_symlink_chain(link / "secret.txt", workspace)


# ---------------------------------------------------------------------------
# _safe_write tests
# ---------------------------------------------------------------------------

class TestSafeWrite:
    def test_write_normal_file(self, workspace):
        """Writing to a regular file should work."""
        target = workspace / "new_file.txt"
        _safe_write(target, "content")
        assert target.read_text() == "content"

    def test_overwrite_existing_file(self, workspace):
        """Overwriting an existing regular file should work."""
        target = workspace / "hello.txt"
        _safe_write(target, "updated")
        assert target.read_text() == "updated"

    def test_refuse_symlink(self, workspace, outside_dir):
        """Writing through a symlink should raise PermissionError."""
        link = workspace / "link_to_secret"
        link.symlink_to(outside_dir / "secret.txt")
        with pytest.raises(PermissionError, match="symlink"):
            _safe_write(link, "pwned")
        # Original file should be untouched
        assert (outside_dir / "secret.txt").read_text() == "secret data"

    def test_refuse_internal_symlink_write(self, workspace):
        """Even internal symlinks should be refused for writes."""
        link = workspace / "link_to_hello"
        link.symlink_to(workspace / "hello.txt")
        with pytest.raises(PermissionError, match="symlink"):
            _safe_write(link, "pwned")


# ---------------------------------------------------------------------------
# _resolve_path tests
# ---------------------------------------------------------------------------

class TestResolvePath:
    def test_path_traversal_blocked(self, workspace):
        """../../../etc/passwd style traversal should be blocked."""
        with pytest.raises(PermissionError, match="outside allowed directory"):
            _resolve_path("../../../etc/passwd", workspace, workspace)

    def test_normal_relative_path(self, workspace):
        """Normal relative path should resolve correctly."""
        result = _resolve_path("hello.txt", workspace, workspace)
        assert result == (workspace / "hello.txt").resolve()

    def test_symlink_escape_via_resolve(self, workspace, outside_dir):
        """Symlink that resolves outside workspace should be blocked."""
        link = workspace / "escape_link"
        link.symlink_to(outside_dir / "secret.txt")
        with pytest.raises(PermissionError):
            _resolve_path("escape_link", workspace, workspace)


# ---------------------------------------------------------------------------
# Tool integration tests
# ---------------------------------------------------------------------------

class TestReadFileTool:
    @pytest.mark.asyncio
    async def test_read_normal(self, workspace):
        tool = ReadFileTool(workspace=workspace, allowed_dir=workspace)
        result = await tool.execute(path="hello.txt")
        assert result == "hello world"

    @pytest.mark.asyncio
    async def test_read_symlink_escape_blocked(self, workspace, outside_dir):
        link = workspace / "bad_link"
        link.symlink_to(outside_dir / "secret.txt")
        tool = ReadFileTool(workspace=workspace, allowed_dir=workspace)
        result = await tool.execute(path="bad_link")
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_read_traversal_blocked(self, workspace):
        tool = ReadFileTool(workspace=workspace, allowed_dir=workspace)
        result = await tool.execute(path="../../../etc/passwd")
        assert "Error" in result


class TestWriteFileTool:
    @pytest.mark.asyncio
    async def test_write_normal(self, workspace):
        tool = WriteFileTool(workspace=workspace, allowed_dir=workspace)
        result = await tool.execute(path="new.txt", content="data")
        assert "Successfully" in result
        assert (workspace / "new.txt").read_text() == "data"

    @pytest.mark.asyncio
    async def test_write_through_symlink_blocked(self, workspace, outside_dir):
        link = workspace / "write_link"
        link.symlink_to(outside_dir / "secret.txt")
        tool = WriteFileTool(workspace=workspace, allowed_dir=workspace)
        result = await tool.execute(path="write_link", content="pwned")
        assert "Error" in result
        assert (outside_dir / "secret.txt").read_text() == "secret data"

    @pytest.mark.asyncio
    async def test_write_parent_symlink_escape(self, workspace, outside_dir):
        """mkdir through a symlinked parent pointing outside should fail."""
        link = workspace / "parent_escape"
        link.symlink_to(outside_dir)
        tool = WriteFileTool(workspace=workspace, allowed_dir=workspace)
        result = await tool.execute(path="parent_escape/evil.txt", content="pwned")
        assert "Error" in result


class TestEditFileTool:
    @pytest.mark.asyncio
    async def test_edit_normal(self, workspace):
        tool = EditFileTool(workspace=workspace, allowed_dir=workspace)
        result = await tool.execute(path="hello.txt", old_text="hello", new_text="goodbye")
        assert "Successfully" in result
        assert (workspace / "hello.txt").read_text() == "goodbye world"

    @pytest.mark.asyncio
    async def test_edit_symlink_blocked(self, workspace, outside_dir):
        """Edit through a symlink should be blocked at write time."""
        # Create a file in workspace, then symlink to outside
        link = workspace / "edit_link"
        link.symlink_to(outside_dir / "secret.txt")
        tool = EditFileTool(workspace=workspace, allowed_dir=workspace)
        result = await tool.execute(path="edit_link", old_text="secret", new_text="pwned")
        assert "Error" in result


class TestListDirTool:
    @pytest.mark.asyncio
    async def test_list_normal(self, workspace):
        tool = ListDirTool(workspace=workspace, allowed_dir=workspace)
        result = await tool.execute(path=".")
        assert "hello.txt" in result

    @pytest.mark.asyncio
    async def test_list_traversal_blocked(self, workspace):
        tool = ListDirTool(workspace=workspace, allowed_dir=workspace)
        result = await tool.execute(path="../../")
        assert "Error" in result


# ---------------------------------------------------------------------------
# Audit logging tests
# ---------------------------------------------------------------------------

class TestAuditLogging:
    @pytest.mark.asyncio
    async def test_read_success_logged(self, workspace, capsys):
        tool = ReadFileTool(workspace=workspace, allowed_dir=workspace, audit_operations=True)
        await tool.execute(path="hello.txt")
        captured = capsys.readouterr()
        assert "file_read" in captured.out or "file_read" in captured.err

    @pytest.mark.asyncio
    async def test_read_blocked_logged(self, workspace, capsys):
        tool = ReadFileTool(workspace=workspace, allowed_dir=workspace, audit_operations=True)
        await tool.execute(path="../../../etc/passwd")
        captured = capsys.readouterr()
        assert "file_read_blocked" in captured.out or "file_read_blocked" in captured.err

    @pytest.mark.asyncio
    async def test_write_success_logged(self, workspace, capsys):
        tool = WriteFileTool(workspace=workspace, allowed_dir=workspace, audit_operations=True)
        await tool.execute(path="audit_test.txt", content="data")
        captured = capsys.readouterr()
        assert "file_written" in captured.out or "file_written" in captured.err

    @pytest.mark.asyncio
    async def test_edit_success_logged(self, workspace, capsys):
        tool = EditFileTool(workspace=workspace, allowed_dir=workspace, audit_operations=True)
        await tool.execute(path="hello.txt", old_text="hello", new_text="hi")
        captured = capsys.readouterr()
        assert "file_edited" in captured.out or "file_edited" in captured.err

    @pytest.mark.asyncio
    async def test_list_success_logged(self, workspace, capsys):
        tool = ListDirTool(workspace=workspace, allowed_dir=workspace, audit_operations=True)
        await tool.execute(path=".")
        captured = capsys.readouterr()
        assert "dir_listed" in captured.out or "dir_listed" in captured.err

    @pytest.mark.asyncio
    async def test_audit_disabled(self, workspace, capsys):
        """When audit_operations=False, no audit logs should be emitted."""
        tool = ReadFileTool(workspace=workspace, allowed_dir=workspace, audit_operations=False)
        await tool.execute(path="hello.txt")
        captured = capsys.readouterr()
        assert "file_read" not in captured.out and "file_read" not in captured.err
