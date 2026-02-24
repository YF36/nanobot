from pathlib import Path

import pytest

from nanobot.agent.tools.base import ToolExecutionResult
from nanobot.agent.tools.filesystem import EditFileTool, ReadFileTool


@pytest.mark.asyncio
async def test_read_file_supports_offset_and_limit(tmp_path: Path) -> None:
    file_path = tmp_path / "sample.txt"
    file_path.write_text("a\nb\nc\nd\n", encoding="utf-8")

    tool = ReadFileTool(workspace=tmp_path)
    result = await tool.execute(path="sample.txt", offset=2, limit=2)

    assert "b\nc" in result
    assert "Use offset=4 to continue" in result


@pytest.mark.asyncio
async def test_read_file_offset_out_of_range(tmp_path: Path) -> None:
    file_path = tmp_path / "sample.txt"
    file_path.write_text("a\nb\n", encoding="utf-8")

    tool = ReadFileTool(workspace=tmp_path)
    result = await tool.execute(path="sample.txt", offset=10)

    assert "beyond end of file" in result


@pytest.mark.asyncio
async def test_edit_file_returns_diff_preview(tmp_path: Path) -> None:
    file_path = tmp_path / "sample.txt"
    file_path.write_text("hello\nworld\n", encoding="utf-8")

    tool = EditFileTool(workspace=tmp_path)
    result = await tool.execute(path="sample.txt", old_text="world", new_text="nanobot")

    assert isinstance(result, ToolExecutionResult)
    assert "Successfully edited" in result.text
    assert "first change at line 2" in result.text
    assert "Diff:" in result.text
    assert "-world" in result.text
    assert "+nanobot" in result.text
    assert result.details["op"] == "edit_file"
    assert result.details["first_changed_line"] == 2
    assert result.details["replacement_count"] == 1
    assert result.details["diff_truncated"] is False
    assert "-world" in result.details["diff_preview"]
