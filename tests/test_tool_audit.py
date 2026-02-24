"""Tests for ToolRegistry audit logging."""

import pytest
from typing import Any
from unittest.mock import patch, MagicMock

from nanobot.agent.tools.base import Tool, ToolExecutionResult
from nanobot.agent.tools.registry import ToolRegistry


class EchoTool(Tool):
    @property
    def name(self) -> str:
        return "echo"

    @property
    def description(self) -> str:
        return "echoes input"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "message": {"type": "string"},
            },
            "required": ["message"],
        }

    async def execute(self, **kwargs: Any) -> str:
        return kwargs["message"]


class FailTool(Tool):
    @property
    def name(self) -> str:
        return "fail"

    @property
    def description(self) -> str:
        return "always fails"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "reason": {"type": "string"},
            },
            "required": ["reason"],
        }

    async def execute(self, **kwargs: Any) -> str:
        raise RuntimeError("boom")


class WriteTool(Tool):
    """Tool with sensitive params for redaction testing."""

    @property
    def name(self) -> str:
        return "write_file"

    @property
    def description(self) -> str:
        return "writes a file"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "new_content": {"type": "string"},
            },
            "required": ["path", "new_content"],
        }

    async def execute(self, **kwargs: Any) -> str:
        return "written"


class StructuredTool(Tool):
    @property
    def name(self) -> str:
        return "structured"

    @property
    def description(self) -> str:
        return "returns structured result"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {},
        }

    async def execute(self, **kwargs: Any) -> ToolExecutionResult:
        return ToolExecutionResult(text="ok", details={"foo": "bar"})


@pytest.fixture
def registry():
    reg = ToolRegistry(audit=True)
    reg.register(EchoTool())
    reg.register(FailTool())
    reg.register(WriteTool())
    reg.register(StructuredTool())
    return reg


@pytest.fixture
def silent_registry():
    reg = ToolRegistry(audit=False)
    reg.register(EchoTool())
    reg.register(FailTool())
    return reg


@pytest.mark.asyncio
async def test_audit_logs_on_success(registry):
    """Successful tool call emits started + completed events."""
    with patch("nanobot.agent.tools.registry.audit_log") as mock_log:
        result = await registry.execute("echo", {"message": "hello"})

    assert result == "hello"
    calls = [c for c in mock_log.info.call_args_list]
    assert len(calls) == 2

    # started
    assert calls[0].args[0] == "tool_call_started"
    assert calls[0].kwargs["tool"] == "echo"

    # completed
    assert calls[1].args[0] == "tool_call_completed"
    assert calls[1].kwargs["tool"] == "echo"
    assert "duration_ms" in calls[1].kwargs
    assert calls[1].kwargs["result_length"] == 5


@pytest.mark.asyncio
async def test_audit_logs_on_failure(registry):
    """Failed tool call emits started + failed events."""
    with patch("nanobot.agent.tools.registry.audit_log") as mock_log:
        result = await registry.execute("fail", {"reason": "test"})

    assert "Error executing fail" in result
    started = mock_log.info.call_args_list
    failed = mock_log.warning.call_args_list
    assert len(started) == 1
    assert started[0].args[0] == "tool_call_started"
    assert len(failed) == 1
    assert failed[0].args[0] == "tool_call_failed"
    assert failed[0].kwargs["error"] == "boom"
    assert "duration_ms" in failed[0].kwargs


@pytest.mark.asyncio
async def test_audit_logs_on_invalid_params(registry):
    """Invalid params emits started + failed with invalid_params."""
    with patch("nanobot.agent.tools.registry.audit_log") as mock_log:
        result = await registry.execute("echo", {})

    assert "Invalid parameters" in result
    started = mock_log.info.call_args_list
    failed = mock_log.warning.call_args_list
    assert started[0].args[0] == "tool_call_started"
    assert failed[0].args[0] == "tool_call_failed"
    assert failed[0].kwargs["error"] == "invalid_params"


@pytest.mark.asyncio
async def test_no_audit_when_disabled(silent_registry):
    """No audit logs when audit=False."""
    with patch("nanobot.agent.tools.registry.audit_log") as mock_log:
        await silent_registry.execute("echo", {"message": "hi"})

    mock_log.info.assert_not_called()
    mock_log.warning.assert_not_called()


@pytest.mark.asyncio
async def test_sanitize_redacts_new_content(registry):
    """new_content param is redacted to length only."""
    with patch("nanobot.agent.tools.registry.audit_log") as mock_log:
        await registry.execute("write_file", {"path": "/tmp/x", "new_content": "secret data here"})

    started = mock_log.info.call_args_list[0]
    params = started.kwargs["params"]
    assert params["path"] == "/tmp/x"
    assert params["new_content"] == "<16 chars>"


@pytest.mark.asyncio
async def test_sanitize_truncates_long_message(registry):
    """Long message param is truncated to 200 chars."""
    long_msg = "a" * 300
    with patch("nanobot.agent.tools.registry.audit_log") as mock_log:
        await registry.execute("echo", {"message": long_msg})

    started = mock_log.info.call_args_list[0]
    params = started.kwargs["params"]
    assert params["message"] == "a" * 200 + "..."


@pytest.mark.asyncio
async def test_execute_result_supports_structured_tool_response(registry):
    result = await registry.execute_result("structured", {})

    assert result.text == "ok"
    assert result.details == {"foo": "bar"}
    assert result.is_error is False


@pytest.mark.asyncio
async def test_execute_keeps_legacy_string_api_for_structured_tool(registry):
    result = await registry.execute("structured", {})
    assert result == "ok"


@pytest.mark.asyncio
async def test_sanitize_short_message_unchanged(registry):
    """Short message param is not truncated."""
    with patch("nanobot.agent.tools.registry.audit_log") as mock_log:
        await registry.execute("echo", {"message": "hi"})

    started = mock_log.info.call_args_list[0]
    params = started.kwargs["params"]
    assert params["message"] == "hi"


def test_sanitize_params_unit():
    """Unit test _sanitize_params directly."""
    reg = ToolRegistry(audit=True)
    result = reg._sanitize_params("any", {
        "path": "/tmp/file",
        "new_content": "x" * 500,
        "content": "y" * 300,
        "command": "short",
        "other": 42,
    })
    assert result["path"] == "/tmp/file"
    assert result["new_content"] == "<500 chars>"
    assert result["content"] == "y" * 200 + "..."
    assert result["command"] == "short"
    assert result["other"] == 42
