from unittest.mock import AsyncMock

import pytest

from nanobot.agent.tools.base import ToolExecutionResult
from nanobot.agent.tools.message import MessageTool
from nanobot.agent.tools.spawn import SpawnTool
from nanobot.bus.events import OutboundMessage


@pytest.mark.asyncio
async def test_message_tool_returns_structured_result_on_success() -> None:
    sent: list[OutboundMessage] = []

    async def _send(msg: OutboundMessage) -> None:
        sent.append(msg)

    tool = MessageTool(send_callback=_send, default_channel="cli", default_chat_id="direct")
    result = await tool.execute(content="hello", media=["a.png", "b.png"])

    assert isinstance(result, ToolExecutionResult)
    assert result.is_error is False
    assert result.details["op"] == "message"
    assert result.details["channel"] == "cli"
    assert result.details["chat_id"] == "direct"
    assert result.details["attachment_count"] == 2
    assert result.details["sent"] is True
    assert tool.sent_in_turn is True
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_message_tool_returns_structured_error_when_unconfigured() -> None:
    tool = MessageTool()
    result = await tool.execute(content="hi")

    assert isinstance(result, ToolExecutionResult)
    assert result.is_error is True
    assert result.details["op"] == "message"
    assert result.details["sent"] is False


@pytest.mark.asyncio
async def test_spawn_tool_returns_structured_result_on_accept() -> None:
    manager = AsyncMock()
    manager.spawn.return_value = "Subagent [x] started (id: 1234abcd). I'll notify you when it completes."

    tool = SpawnTool(manager)
    tool.set_context("cli", "chat1")
    result = await tool.execute(task="do something", label="x")

    assert isinstance(result, ToolExecutionResult)
    assert result.is_error is False
    assert result.details["op"] == "spawn"
    assert result.details["accepted"] is True
    assert result.details["origin_channel"] == "cli"
    assert result.details["origin_chat_id"] == "chat1"
    assert result.details["label"] == "x"
    assert result.details["task_len"] == len("do something")
    _, kwargs = manager.spawn.await_args
    assert kwargs["session_key"] == "cli:chat1"


@pytest.mark.asyncio
async def test_spawn_tool_returns_structured_error_when_rejected() -> None:
    manager = AsyncMock()
    manager.spawn.return_value = "Cannot spawn subagent: limit of 1 concurrent subagents reached."

    tool = SpawnTool(manager)
    result = await tool.execute(task="do something")

    assert isinstance(result, ToolExecutionResult)
    assert result.is_error is True
    assert result.details["op"] == "spawn"
    assert result.details["accepted"] is False
