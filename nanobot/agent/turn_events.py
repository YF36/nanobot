"""Typed internal turn-event payloads used by TurnRunner and observers."""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Literal, TypeAlias, TypedDict

TURN_EVENT_TURN_START = "turn_start"
TURN_EVENT_TOOL_START = "tool_start"
TURN_EVENT_TOOL_END = "tool_end"
TURN_EVENT_TURN_END = "turn_end"

TurnEventType: TypeAlias = Literal[
    "turn_start",
    "tool_start",
    "tool_end",
    "turn_end",
]


class BaseTurnEvent(TypedDict):
    type: TurnEventType
    turn_id: str
    sequence: int
    timestamp_ms: int
    source: str


class TurnStartEvent(BaseTurnEvent):
    type: Literal["turn_start"]
    initial_message_count: int
    max_iterations: int


class ToolStartEvent(BaseTurnEvent):
    type: Literal["tool_start"]
    iteration: int
    tool: str
    tool_call_id: str
    arguments: dict[str, Any]


class ToolEndEvent(BaseTurnEvent):
    type: Literal["tool_end"]
    iteration: int
    tool: str
    tool_call_id: str
    is_error: bool
    has_details: bool
    detail_op: str | None


class TurnEndEvent(BaseTurnEvent):
    type: Literal["turn_end"]
    iterations: int
    tool_count: int
    completed: bool
    max_iterations_reached: bool


TurnEventPayload: TypeAlias = TurnStartEvent | ToolStartEvent | ToolEndEvent | TurnEndEvent
TurnEventCallback: TypeAlias = Callable[[TurnEventPayload], Awaitable[None]]


def turn_event_trace_fields(event: TurnEventPayload) -> dict[str, Any]:
    """Common trace fields for event logging sinks."""
    return {
        "source": event.get("source"),
        "turn_id": event.get("turn_id"),
        "sequence": event.get("sequence"),
    }
