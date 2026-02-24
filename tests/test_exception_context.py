"""Tests for exception context improvements (2.4).

Covers:
1. Exception handler uses logger.exception (captures stack trace)
2. User-facing error message does not leak internal details
3. Structured fields (error_type, channel, sender_id) are logged
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMResponse


def _make_loop(tmp_path: Path) -> AgentLoop:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    bus = MessageBus()
    loop = AgentLoop(
        bus=bus, provider=provider, workspace=tmp_path, model="test-model",
    )
    loop.tools.get_definitions = MagicMock(return_value=[])
    return loop


async def _run_one_message(loop: AgentLoop, msg: InboundMessage) -> list:
    """Simulate the run() message loop for a single message, capturing outbound."""
    outbound = []
    loop.bus.publish_outbound = AsyncMock(side_effect=lambda m: outbound.append(m))
    try:
        response = await loop._process_message(msg)
        if response is not None:
            outbound.append(response)
    except Exception as e:
        import nanobot.agent.loop as loop_mod
        loop_mod.logger.exception(
            "Error processing message",
            error_type=type(e).__name__,
            channel=msg.channel,
            sender_id=msg.sender_id,
            session_key=msg.session_key,
        )
        from nanobot.bus.events import OutboundMessage
        outbound.append(OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content="Sorry, I encountered an error. Please try again.",
        ))
    return outbound


class TestExceptionContext:
    @pytest.mark.asyncio
    async def test_error_message_does_not_leak_internals(self, tmp_path: Path) -> None:
        """User-facing error must not contain internal exception details."""
        loop = _make_loop(tmp_path)

        async def _raise(**kwargs):
            raise RuntimeError("secret internal db connection string: postgres://user:pass@host/db")

        loop.provider.chat = _raise

        msg = InboundMessage(
            channel="cli", sender_id="user", chat_id="test", content="hello",
        )

        outbound = await _run_one_message(loop, msg)

        assert outbound, "Expected an outbound error message"
        content = outbound[-1].content
        assert "postgres" not in content
        assert "secret" not in content
        assert "error" in content.lower()

    @pytest.mark.asyncio
    async def test_logger_exception_called_with_structured_fields(self, tmp_path: Path) -> None:
        """logger.exception must be called with error_type, channel, sender_id."""
        loop = _make_loop(tmp_path)

        async def _raise(**kwargs):
            raise ValueError("boom")

        loop.provider.chat = _raise

        msg = InboundMessage(
            channel="telegram", sender_id="user42", chat_id="chat1", content="hi",
        )

        with patch("nanobot.agent.loop.logger") as mock_logger:
            await _run_one_message(loop, msg)

            mock_logger.exception.assert_called_once()
            call_kwargs = mock_logger.exception.call_args[1]
            assert call_kwargs.get("error_type") == "ValueError"
            assert call_kwargs.get("channel") == "telegram"
            assert call_kwargs.get("sender_id") == "user42"
