"""Message processing orchestration extracted from AgentLoop."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

import structlog

from nanobot.agent.consolidation_coordinator import ConsolidationCoordinator
from nanobot.agent.tools.message import MessageTool
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.logging import get_logger

logger = get_logger(__name__)


class MessageProcessor:
    """Handle inbound message processing using AgentLoop-provided callbacks."""

    def __init__(
        self,
        *,
        sessions: Any,
        context: Any,
        tools: Any,
        bus: MessageBus,
        command_handler: Any,
        consolidation: ConsolidationCoordinator,
        memory_window: int,
        set_tool_context: Callable[[str, str, str | None], None],
        run_agent_loop: Callable[..., Awaitable[tuple[str | None, list[str], list[dict[str, Any]]]]],
        save_turn: Callable[[Any, list[dict[str, Any]], int], None],
        consolidate_memory: Callable[[Any], Awaitable[bool]],
    ) -> None:
        self.sessions = sessions
        self.context = context
        self.tools = tools
        self.bus = bus
        self.command_handler = command_handler
        self.consolidation = consolidation
        self.memory_window = memory_window
        self.set_tool_context = set_tool_context
        self.run_agent_loop = run_agent_loop
        self.save_turn = save_turn
        self.consolidate_memory = consolidate_memory

    async def process(
        self,
        msg: InboundMessage,
        session_key: str | None = None,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> OutboundMessage | None:
        """Process a single inbound message and return the response."""
        if msg.channel == "system":
            return await self._process_system_message(msg)
        return await self._process_regular_message(msg, session_key=session_key, on_progress=on_progress)

    async def _process_system_message(self, msg: InboundMessage) -> OutboundMessage:
        channel, chat_id = (msg.chat_id.split(":", 1) if ":" in msg.chat_id else ("cli", msg.chat_id))
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            channel=channel, sender_id=msg.sender_id, session_key=f"{channel}:{chat_id}",
        )
        logger.info("Processing system message")
        key = f"{channel}:{chat_id}"
        session = self.sessions.get_or_create(key)
        self.set_tool_context(channel, chat_id, (msg.metadata or {}).get("message_id"))
        history = session.get_history(max_messages=self.memory_window)
        messages = self.context.build_messages(
            history=history,
            current_message=msg.content,
            channel=channel,
            chat_id=chat_id,
        )
        skip = len(messages) - 1  # include current user message in saved turn
        final_content, _, all_msgs = await self.run_agent_loop(messages)
        self.save_turn(session, all_msgs, skip)
        self.sessions.save(session)
        return OutboundMessage(
            channel=channel,
            chat_id=chat_id,
            content=final_content or "Background task completed.",
        )

    async def _process_regular_message(
        self,
        msg: InboundMessage,
        *,
        session_key: str | None,
        on_progress: Callable[[str], Awaitable[None]] | None,
    ) -> OutboundMessage | None:
        preview = msg.content[:80] + "..." if len(msg.content) > 80 else msg.content

        key = session_key or msg.session_key
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            channel=msg.channel, sender_id=msg.sender_id,
            session_key=key, chat_id=msg.chat_id,
        )
        logger.info("Processing message", preview=preview)

        session = self.sessions.get_or_create(key)

        command_response = await self.command_handler.handle(msg, session)
        if command_response is not None:
            return command_response

        if len(session.messages) > self.memory_window:
            self.consolidation.start_background(session.key, lambda: self.consolidate_memory(session))

        self.set_tool_context(msg.channel, msg.chat_id, (msg.metadata or {}).get("message_id"))
        if message_tool := self.tools.get("message"):
            if isinstance(message_tool, MessageTool):
                message_tool.start_turn()

        history = session.get_history(max_messages=self.memory_window)
        initial_messages = self.context.build_messages(
            history=history,
            current_message=msg.content,
            media=msg.media if msg.media else None,
            channel=msg.channel,
            chat_id=msg.chat_id,
        )

        progress_cb = on_progress or self._make_bus_progress(msg)

        # initial_messages = [system, compacted_history..., current_user]
        # Save current_user + all LLM responses, so skip system + history.
        skip = len(initial_messages) - 1

        final_content, _, all_msgs = await self.run_agent_loop(initial_messages, on_progress=progress_cb)

        if final_content is None:
            final_content = "I've completed processing but have no response to give."

        preview = final_content[:120] + "..." if len(final_content) > 120 else final_content
        logger.info("Response sent", preview=preview)

        self.save_turn(session, all_msgs, skip)
        self.sessions.save(session)

        if message_tool := self.tools.get("message"):
            if isinstance(message_tool, MessageTool) and message_tool._sent_in_turn:
                return None

        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=final_content,
            metadata=msg.metadata or {},
        )

    def _make_bus_progress(self, msg: InboundMessage) -> Callable[..., Awaitable[None]]:
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
