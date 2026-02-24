"""Message processing orchestration extracted from AgentLoop."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from nanobot.agent.message_processor_helpers import (
    MessageToolTurnController,
    ProgressPublisher,
    RequestContextBinder,
    ToolContextInitializer,
    TurnEventStatsCollector,
    TurnExecutionCoordinator,
    TurnMessageBuilder,
)
from nanobot.agent.message_processor_types import (
    CommandHandlerProtocol,
    ContextBuilderProtocol,
    MessageProcessingHooks,
    MessageProcessorDeps,
    SessionStoreProtocol,
    ToolRegistryProtocol,
)
from nanobot.agent.consolidation_coordinator import ConsolidationCoordinator
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.logging import get_logger

logger = get_logger(__name__)


class BaseTurnHandler:
    """Common helper wiring for handlers that execute/persist turns."""

    def __init__(self, deps: MessageProcessorDeps) -> None:
        self.deps = deps
        self.turn_executor = TurnExecutionCoordinator(deps)
        self.message_builder = TurnMessageBuilder(deps)
        self.tool_context = ToolContextInitializer(deps.hooks)


class SystemMessageHandler(BaseTurnHandler):
    """Handle internal/system messages."""

    async def handle(self, msg: InboundMessage) -> OutboundMessage:
        channel, chat_id = (msg.chat_id.split(":", 1) if ":" in msg.chat_id else ("cli", msg.chat_id))
        RequestContextBinder.bind_system(msg, channel, chat_id)
        key = f"{channel}:{chat_id}"
        session = self.deps.sessions.get_or_create(key)
        self.tool_context.set_from_message(msg, channel=channel, chat_id=chat_id)
        messages = self.message_builder.build(
            session=session,
            msg=msg,
            channel=channel,
            chat_id=chat_id,
            include_media=False,
        )
        skip = len(messages) - 1  # include current user message in saved turn
        turn_events = TurnEventStatsCollector()
        final_content, _, _ = await self.turn_executor.run_and_persist(
            session=session,
            messages=messages,
            skip=skip,
            on_event=turn_events.on_event,
        )
        turn_events.log_summary(route="system", msg=msg)
        return OutboundMessage(
            channel=channel,
            chat_id=chat_id,
            content=final_content or "Background task completed.",
        )


class UserMessageHandler(BaseTurnHandler):
    """Handle normal user messages and progress events."""

    def __init__(self, deps: MessageProcessorDeps) -> None:
        super().__init__(deps)
        self.progress = ProgressPublisher(deps.bus)
        self.message_tool = MessageToolTurnController(deps.tools)

    def _schedule_background_consolidation_if_needed(self, session: Any) -> None:
        if len(session.messages) <= self.deps.memory_window:
            return
        self.deps.consolidation.start_background(
            session.key,
            lambda: self.deps.hooks.consolidate_memory(session),
        )

    def _prepare_turn(self, msg: InboundMessage, session: Any) -> None:
        self._schedule_background_consolidation_if_needed(session)
        self.tool_context.set_from_message(msg, channel=msg.channel, chat_id=msg.chat_id)
        self.message_tool.start_turn()

    def _build_initial_messages(self, msg: InboundMessage, session: Any) -> list[dict[str, Any]]:
        return self.message_builder.build(
            session=session,
            msg=msg,
            channel=msg.channel,
            chat_id=msg.chat_id,
            include_media=True,
        )

    def _finalize_response(
        self,
        msg: InboundMessage,
        final_content: str,
    ) -> OutboundMessage | None:
        preview = final_content[:120] + "..." if len(final_content) > 120 else final_content
        logger.info("Response sent", preview=preview)

        if self.message_tool.sent_reply_in_turn():
            return None

        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=final_content,
            metadata=msg.metadata or {},
        )

    async def handle(
        self,
        msg: InboundMessage,
        *,
        session_key: str | None,
        on_progress: Callable[[str], Awaitable[None]] | None,
    ) -> OutboundMessage | None:
        key = session_key or msg.session_key
        RequestContextBinder.bind_user(msg, key)

        session = self.deps.sessions.get_or_create(key)

        command_response = await self.deps.command_handler.handle(msg, session)
        if command_response is not None:
            return command_response

        self._prepare_turn(msg, session)
        initial_messages = self._build_initial_messages(msg, session)

        progress_cb = on_progress or self.progress.for_message(msg)

        # initial_messages = [system, compacted_history..., current_user]
        # Save current_user + all LLM responses, so skip system + history.
        skip = len(initial_messages) - 1

        turn_events = TurnEventStatsCollector()
        final_content, _, _ = await self.turn_executor.run_and_persist(
            session=session,
            messages=initial_messages,
            skip=skip,
            on_progress=progress_cb,
            on_event=turn_events.on_event,
        )
        turn_events.log_summary(route="user", msg=msg)

        if final_content is None:
            final_content = "I've completed processing but have no response to give."

        return self._finalize_response(msg, final_content)


class MessageProcessor:
    """Dispatch inbound messages to system/user handlers."""

    def __init__(
        self,
        *,
        sessions: SessionStoreProtocol,
        context: ContextBuilderProtocol,
        tools: ToolRegistryProtocol,
        bus: MessageBus,
        command_handler: CommandHandlerProtocol,
        consolidation: ConsolidationCoordinator,
        memory_window: int,
        hooks: MessageProcessingHooks,
    ) -> None:
        deps = MessageProcessorDeps(
            sessions=sessions,
            context=context,
            tools=tools,
            bus=bus,
            command_handler=command_handler,
            consolidation=consolidation,
            memory_window=memory_window,
            hooks=hooks,
        )
        self._system = SystemMessageHandler(deps)
        self._user = UserMessageHandler(deps)

    async def process(
        self,
        msg: InboundMessage,
        session_key: str | None = None,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> OutboundMessage | None:
        """Process a single inbound message and return the response."""
        if msg.channel == "system":
            return await self._system.handle(msg)
        return await self._user.handle(msg, session_key=session_key, on_progress=on_progress)
