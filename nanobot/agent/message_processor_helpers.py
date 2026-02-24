"""Helper classes used by message processor handlers."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from nanobot.agent.tools.message import MessageTool
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.logging import get_logger

if TYPE_CHECKING:
    from nanobot.agent.message_processor import MessageProcessingHooks, MessageProcessorDeps, ToolRegistryProtocol
    from nanobot.agent.message_processor import ProgressCallback

logger = get_logger(__name__)


class ProgressPublisher:
    """Publish incremental progress messages to the outbound bus."""

    def __init__(self, bus: MessageBus) -> None:
        self.bus = bus

    def for_message(self, msg: InboundMessage) -> "ProgressCallback":
        async def _bus_progress(content: str, *, tool_hint: bool = False) -> None:
            meta = dict(msg.metadata or {})
            meta["_progress"] = True
            meta["_tool_hint"] = tool_hint
            await self.bus.publish_outbound(OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=content,
                metadata=meta,
            ))

        return _bus_progress


class MessageToolTurnController:
    """Thin adapter for message-tool turn lifecycle and reply detection."""

    def __init__(self, tools: "ToolRegistryProtocol") -> None:
        self.tools = tools

    def _get_message_tool(self) -> MessageTool | None:
        tool = self.tools.get("message")
        return tool if isinstance(tool, MessageTool) else None

    def start_turn(self) -> None:
        if tool := self._get_message_tool():
            tool.start_turn()

    def sent_reply_in_turn(self) -> bool:
        tool = self._get_message_tool()
        if tool is None:
            return False
        return bool(getattr(tool, "sent_in_turn", getattr(tool, "_sent_in_turn", False)))


class RequestContextBinder:
    """Bind per-request logging context for system/user message processing."""

    @staticmethod
    def bind_system(msg: InboundMessage, channel: str, chat_id: str) -> None:
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            channel=channel,
            sender_id=msg.sender_id,
            session_key=f"{channel}:{chat_id}",
        )
        logger.info("Processing system message")

    @staticmethod
    def bind_user(msg: InboundMessage, session_key: str) -> None:
        preview = msg.content[:80] + "..." if len(msg.content) > 80 else msg.content
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            channel=msg.channel,
            sender_id=msg.sender_id,
            session_key=session_key,
            chat_id=msg.chat_id,
        )
        logger.info("Processing message", preview=preview)


class TurnExecutionCoordinator:
    """Run a turn and persist its resulting messages to the session."""

    def __init__(self, deps: "MessageProcessorDeps") -> None:
        self.deps = deps

    async def run_and_persist(
        self,
        *,
        session: Any,
        messages: list[dict[str, Any]],
        skip: int,
        on_progress: "ProgressCallback" | None = None,
    ) -> tuple[str | None, list[str], list[dict[str, Any]]]:
        final_content, tools_used, all_msgs = await self.deps.hooks.run_agent_loop(
            messages,
            on_progress=on_progress,
        )
        self.deps.hooks.save_turn(session, all_msgs, skip)
        self.deps.sessions.save(session)
        return final_content, tools_used, all_msgs


class TurnMessageBuilder:
    """Build initial LLM messages for a turn from session history + inbound message."""

    def __init__(self, deps: "MessageProcessorDeps") -> None:
        self.deps = deps

    def build(
        self,
        *,
        session: Any,
        msg: InboundMessage,
        channel: str,
        chat_id: str,
        include_media: bool,
    ) -> list[dict[str, Any]]:
        history = session.get_history(max_messages=self.deps.memory_window)
        kwargs: dict[str, Any] = {
            "history": history,
            "current_message": msg.content,
            "channel": channel,
            "chat_id": chat_id,
        }
        if include_media:
            kwargs["media"] = msg.media if msg.media else None
        return self.deps.context.build_messages(**kwargs)


class ToolContextInitializer:
    """Initialize tool routing context from inbound message metadata."""

    def __init__(self, hooks: "MessageProcessingHooks") -> None:
        self.hooks = hooks

    def set_from_message(self, msg: InboundMessage, *, channel: str, chat_id: str) -> None:
        self.hooks.set_tool_context(channel, chat_id, (msg.metadata or {}).get("message_id"))
