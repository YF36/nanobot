from types import SimpleNamespace
from unittest.mock import patch

import pytest

from nanobot.providers.litellm_provider import LiteLLMProvider


class _FakeStream:
    def __init__(self, chunks):
        self._chunks = list(chunks)
        self._index = 0

    def __aiter__(self):
        self._index = 0
        return self

    async def __anext__(self):
        if self._index >= len(self._chunks):
            raise StopAsyncIteration
        chunk = self._chunks[self._index]
        self._index += 1
        return chunk


@pytest.mark.asyncio
async def test_stream_chat_emits_text_delta_and_done_with_tool_calls() -> None:
    provider = LiteLLMProvider(api_key="fake-key")

    chunks = [
        SimpleNamespace(
            choices=[SimpleNamespace(
                delta=SimpleNamespace(content="Hello "),
                finish_reason=None,
            )],
            usage=None,
        ),
        SimpleNamespace(
            choices=[SimpleNamespace(
                delta=SimpleNamespace(
                    tool_calls=[
                        SimpleNamespace(
                            index=0,
                            id="call_1",
                            function=SimpleNamespace(name="web_search", arguments='{"q":"nanobot"}'),
                        )
                    ]
                ),
                finish_reason=None,
            )],
            usage=None,
        ),
        SimpleNamespace(
            choices=[SimpleNamespace(
                delta=SimpleNamespace(content="world"),
                finish_reason="tool_calls",
            )],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        ),
    ]

    async def fake_acompletion(**kwargs):
        return _FakeStream(chunks)

    with patch("nanobot.providers.litellm_provider.acompletion", side_effect=fake_acompletion):
        events = [event async for event in provider.stream_chat(
            messages=[{"role": "user", "content": "hi"}],
            tools=[{"type": "function", "function": {"name": "web_search", "parameters": {"type": "object"}}}],
        )]

    assert [e["type"] for e in events] == ["text_delta", "text_delta", "done"]
    assert [e["delta"] for e in events[:2]] == ["Hello ", "world"]
    response = events[-1]["response"]
    assert response.content == "Hello world"
    assert response.finish_reason == "tool_calls"
    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].name == "web_search"
    assert response.tool_calls[0].arguments == {"q": "nanobot"}
    assert response.usage["total_tokens"] == 15


@pytest.mark.asyncio
async def test_stream_chat_passes_request_extras_to_litellm() -> None:
    provider = LiteLLMProvider(
        api_key="fake-key",
        request_extras={
            "extra_body": {"tool_stream": True},
            "stream_options": {"include_usage": True},
        },
    )

    captured_kwargs = {}

    async def fake_acompletion(**kwargs):
        captured_kwargs.update(kwargs)
        return _FakeStream([
            SimpleNamespace(
                choices=[SimpleNamespace(
                    delta=SimpleNamespace(content="ok"),
                    finish_reason="stop",
                )],
                usage=None,
            )
        ])

    with patch("nanobot.providers.litellm_provider.acompletion", side_effect=fake_acompletion):
        events = [event async for event in provider.stream_chat(
            messages=[{"role": "user", "content": "hi"}],
        )]

    assert captured_kwargs["stream"] is True
    assert captured_kwargs["extra_body"] == {"tool_stream": True}
    assert captured_kwargs["stream_options"] == {"include_usage": True}
    assert events[-1]["type"] == "done"
    assert events[-1]["response"].content == "ok"
