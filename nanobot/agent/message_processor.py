"""Message processing orchestration extracted from AgentLoop."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol, TypeAlias

import structlog

from nanobot.agent.consolidation_coordinator import ConsolidationCoordinator
from nanobot.agent.tools.message import MessageTool
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.logging import get_logger

logger = get_logger(__name__)


ProgressCallback: TypeAlias = Callable[..., Awaitable[None]]
RunAgentLoopCallback: TypeAlias = Callable[..., Awaitable[tuple[str | None, list[str], list[dict[str, Any]]]]]
SaveTurnCallback: TypeAlias = Callable[[Any, list[dict[str, Any]], int], None]
ConsolidateSessionCallback: TypeAlias = Callable[[Any], Awaitable[bool]]
SetToolContextCallback: TypeAlias = Callable[[str, str, str | None], None]


class SessionStoreProtocol(Protocol):
    def get_or_create(self, key: str) -> Any: ...
    def save(self, session: Any) -> None: ...


class ContextBuilderProtocol(Protocol):
    def build_messages(self, **kwargs: Any) -> list[dict[str, Any]]: ...


class ToolRegistryProtocol(Protocol):
    def get(self, name: str) -> Any: ...


class CommandHandlerProtocol(Protocol):
    async def handle(self, msg: InboundMessage, session: Any) -> OutboundMessage | None: ...


@dataclass(frozen=True)
class MessageProcessingHooks:
    """AgentLoop callbacks used by MessageProcessor."""

    set_tool_context: SetToolContextCallback
    run_agent_loop: RunAgentLoopCallback
    save_turn: SaveTurnCallback
    consolidate_memory: ConsolidateSessionCallback


@dataclass(frozen=True)
class MessageProcessorDeps:
    """Shared dependencies for system/user message handlers."""

    sessions: SessionStoreProtocol
    context: ContextBuilderProtocol
    tools: ToolRegistryProtocol
    bus: MessageBus
    command_handler: CommandHandlerProtocol
    consolidation: ConsolidationCoordinator
    memory_window: int
    hooks: MessageProcessingHooks


class SystemMessageHandler:
    """Handle internal/system messages."""

    def __init__(self, deps: MessageProcessorDeps) -> None:
        self.deps = deps
        self.turn_executor = TurnExecutionCoordinator(deps)

    async def handle(self, msg: InboundMessage) -> OutboundMessage:
        channel, chat_id = (msg.chat_id.split(":", 1) if ":" in msg.chat_id else ("cli", msg.chat_id))
        RequestContextBinder.bind_system(msg, channel, chat_id)
        key = f"{channel}:{chat_id}"
        session = self.deps.sessions.get_or_create(key)
        self.deps.hooks.set_tool_context(channel, chat_id, (msg.metadata or {}).get("message_id"))
        history = session.get_history(max_messages=self.deps.memory_window)
        messages = self.deps.context.build_messages(
            history=history,
            current_message=msg.content,
            channel=channel,
            chat_id=chat_id,
        )
        skip = len(messages) - 1  # include current user message in saved turn
        final_content, _, _ = await self.turn_executor.run_and_persist(
            session=session,
            messages=messages,
            skip=skip,
        )
        return OutboundMessage(
            channel=channel,
            chat_id=chat_id,
            content=final_content or "Background task completed.",
        )


class ProgressPublisher:
    """Publish incremental progress messages to the outbound bus."""

    def __init__(self, bus: MessageBus) -> None:
        self.bus = bus

    def for_message(self, msg: InboundMessage) -> ProgressCallback:
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

    def __init__(self, tools: ToolRegistryProtocol) -> None:
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

    def __init__(self, deps: MessageProcessorDeps) -> None:
        self.deps = deps

    async def run_and_persist(
        self,
        *,
        session: Any,
        messages: list[dict[str, Any]],
        skip: int,
        on_progress: ProgressCallback | None = None,
    ) -> tuple[str | None, list[str], list[dict[str, Any]]]:
        final_content, tools_used, all_msgs = await self.deps.hooks.run_agent_loop(
            messages,
            on_progress=on_progress,
        )
        self.deps.hooks.save_turn(session, all_msgs, skip)
        self.deps.sessions.save(session)
        return final_content, tools_used, all_msgs


class UserMessageHandler:
    """Handle normal user messages and progress events."""

    def __init__(self, deps: MessageProcessorDeps) -> None:
        self.deps = deps
        self.progress = ProgressPublisher(deps.bus)
        self.message_tool = MessageToolTurnController(deps.tools)
        self.turn_executor = TurnExecutionCoordinator(deps)

    def _schedule_background_consolidation_if_needed(self, session: Any) -> None:
        if len(session.messages) <= self.deps.memory_window:
            return
        self.deps.consolidation.start_background(
            session.key,
            lambda: self.deps.hooks.consolidate_memory(session),
        )

    def _prepare_turn(self, msg: InboundMessage, session: Any) -> None:
        self._schedule_background_consolidation_if_needed(session)
        self.deps.hooks.set_tool_context(msg.channel, msg.chat_id, (msg.metadata or {}).get("message_id"))
        self.message_tool.start_turn()

    def _build_initial_messages(self, msg: InboundMessage, session: Any) -> list[dict[str, Any]]:
        history = session.get_history(max_messages=self.deps.memory_window)
        return self.deps.context.build_messages(
            history=history,
            current_message=msg.content,
            media=msg.media if msg.media else None,
            channel=msg.channel,
            chat_id=msg.chat_id,
        )

    def _finalize_response(
        self,
        msg: InboundMessage,
        session: Any,
        final_content: str,
        all_msgs: list[dict[str, Any]],
        skip: int,
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

        final_content, _, all_msgs = await self.turn_executor.run_and_persist(
            session=session,
            messages=initial_messages,
            skip=skip,
            on_progress=progress_cb,
        )

        if final_content is None:
            final_content = "I've completed processing but have no response to give."

        return self._finalize_response(msg, session, final_content, all_msgs, skip)


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
