"""Shared types/protocols for message processor modules."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol, TypeAlias

from nanobot.agent.consolidation_coordinator import ConsolidationCoordinator
from nanobot.agent.turn_events import TurnEventCallback
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus

ProgressCallback: TypeAlias = Callable[..., Awaitable[None]]
SteerDecision: TypeAlias = bool | dict[str, Any]
SteerCheckCallback: TypeAlias = Callable[[], SteerDecision | Awaitable[SteerDecision]]
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
    def get_definitions(self) -> list[dict[str, Any]]: ...


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
    progress_max_messages_per_turn: int
    hooks: MessageProcessingHooks
