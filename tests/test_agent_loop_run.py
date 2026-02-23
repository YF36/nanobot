from pathlib import Path

import pytest

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
