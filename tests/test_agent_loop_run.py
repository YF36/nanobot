import asyncio
from pathlib import Path

import pytest

from nanobot.bus.queue import MessageBus
from nanobot.agent.context import ContextBuilder
from nanobot.agent.loop import AgentLoop
from nanobot.providers.base import LLMProvider, LLMResponse


class _NoToolProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__()

    async def chat(
        self,
        messages,
        tools=None,
        model=None,
        max_tokens=4096,
        temperature=0.7,
    ) -> LLMResponse:
        return LLMResponse(content="final answer", finish_reason="stop")

    def get_default_model(self) -> str:
        return "test-model"


class _ToolsStub:
    def get_definitions(self):
        return []


@pytest.mark.asyncio
async def test_run_agent_loop_appends_final_assistant_message(tmp_path: Path) -> None:
    agent = AgentLoop.__new__(AgentLoop)
    agent.provider = _NoToolProvider()
    agent.tools = _ToolsStub()
    agent.context = ContextBuilder(tmp_path)
    agent.model = "test-model"
    agent.temperature = 0.1
    agent.max_tokens = 256
    agent.max_iterations = 3

    initial_messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hello"},
    ]

    final_content, tools_used, messages = await agent._run_agent_loop(initial_messages)

    assert final_content == "final answer"
    assert tools_used == []
    assert messages[-1]["role"] == "assistant"
    assert messages[-1]["content"] == "final answer"


@pytest.mark.asyncio
async def test_run_sets_stopped_event_when_cancelled() -> None:
    agent = AgentLoop.__new__(AgentLoop)
    agent.bus = MessageBus()
    agent._running = False
    agent._stopped_event = asyncio.Event()
    agent._mcp_stack = None
    agent._mcp_connected = False
    agent._mcp_connecting = False
    agent._mcp_servers = {}

    async def _noop_connect():
        return None

    agent._connect_mcp = _noop_connect  # type: ignore[method-assign]

    task = asyncio.create_task(agent.run())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert await agent.wait_stopped(timeout=0.2) is True


def test_guard_loop_messages_trims_history_before_current_turn(tmp_path: Path) -> None:
    agent = AgentLoop.__new__(AgentLoop)
    agent.context = ContextBuilder(tmp_path, max_context_tokens=120)
    agent.max_tokens = 64

    # Build a sizable history, then a compact current turn.
    history = []
    for i in range(8):
        history.append({"role": "user", "content": f"old question {i} " + ("x " * 20)})
        history.append({"role": "assistant", "content": f"old answer {i} " + ("y " * 20)})

    messages = [{"role": "system", "content": "sys"}] + history + [
        {"role": "user", "content": "current question"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "t", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "name": "t", "content": "ok"},
    ]
    current_turn_start = 1 + len(history)

    guarded, new_start = agent._guard_loop_messages(messages, current_turn_start)

    assert guarded[0]["role"] == "system"
    assert guarded[new_start]["role"] == "user"
    assert guarded[new_start]["content"] == "current question"
    assert len(guarded) < len(messages)  # history should be trimmed


def test_guard_loop_messages_truncates_large_tool_result_in_current_turn(tmp_path: Path) -> None:
    agent = AgentLoop.__new__(AgentLoop)
    agent.context = ContextBuilder(tmp_path, max_context_tokens=80)
    agent.max_tokens = 64

    large_tool_output = "z" * 10000
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "current question"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "fetch", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "name": "fetch", "content": large_tool_output},
    ]

    guarded, new_start = agent._guard_loop_messages(messages, current_turn_start=1)

    assert new_start == 1
    assert guarded[-1]["role"] == "tool"
    assert isinstance(guarded[-1]["content"], str)
    assert guarded[-1]["content"].endswith("\n... (truncated)")
    assert len(guarded[-1]["content"]) < len(large_tool_output)
