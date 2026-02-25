"""Typed internal turn-event payloads used by TurnRunner and observers."""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Literal, NotRequired, TypeAlias, TypedDict

TURN_EVENT_TURN_START = "turn_start"
TURN_EVENT_TOOL_START = "tool_start"
TURN_EVENT_TOOL_END = "tool_end"
TURN_EVENT_TURN_END = "turn_end"
TURN_EVENT_NAMESPACE = "nanobot.turn"
TURN_EVENT_SCHEMA_VERSION = 1
TURN_EVENT_KIND_TURN_START = "turn.start"
TURN_EVENT_KIND_TOOL_START = "tool.start"
TURN_EVENT_KIND_TOOL_END = "tool.end"
TURN_EVENT_KIND_TURN_END = "turn.end"

TurnEventType: TypeAlias = Literal[
    "turn_start",
    "tool_start",
    "tool_end",
    "turn_end",
]
TurnEventKind: TypeAlias = Literal[
    "turn.start",
    "tool.start",
    "tool.end",
    "turn.end",
]

_TURN_EVENT_KIND_MAP: dict[str, str] = {
    TURN_EVENT_TURN_START: TURN_EVENT_KIND_TURN_START,
    TURN_EVENT_TOOL_START: TURN_EVENT_KIND_TOOL_START,
    TURN_EVENT_TOOL_END: TURN_EVENT_KIND_TOOL_END,
    TURN_EVENT_TURN_END: TURN_EVENT_KIND_TURN_END,
}


class BaseTurnEvent(TypedDict):
    namespace: str
    version: int
    type: TurnEventType
    kind: TurnEventKind
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
    interrupted_for_followup: NotRequired[bool]
    interruption_reason: NotRequired[str]
    interrupted_at_iteration: NotRequired[int]
    interrupted_after_tool: NotRequired[str]
    pending_followup_count: NotRequired[int]
    next_followup_preview: NotRequired[str]
    llm_retry_count: NotRequired[int]
    llm_exception_retry_count: NotRequired[int]
    llm_error_finish_retry_count: NotRequired[int]
    llm_overflow_compaction_retries: NotRequired[int]
    llm_error_finish_overflow_count: NotRequired[int]
    llm_error_finish_retryable_count: NotRequired[int]
    llm_error_finish_fatal_count: NotRequired[int]


TurnEventPayload: TypeAlias = TurnStartEvent | ToolStartEvent | ToolEndEvent | TurnEndEvent
TurnEventCallback: TypeAlias = Callable[[TurnEventPayload], Awaitable[None]]


def turn_event_trace_fields(event: TurnEventPayload) -> dict[str, Any]:
    """Common trace fields for event logging sinks."""
    return {
        "namespace": event.get("namespace"),
        "version": event.get("version"),
        "source": event.get("source"),
        "turn_id": event.get("turn_id"),
        "sequence": event.get("sequence"),
    }


def turn_event_kind(event_type: str) -> str:
    """Return hierarchical event kind for a legacy flat event type."""
    return _TURN_EVENT_KIND_MAP.get(event_type, event_type.replace("_", "."))


TURN_EVENT_CAPABILITIES = {
    "namespace": TURN_EVENT_NAMESPACE,
    "version": TURN_EVENT_SCHEMA_VERSION,
    "events": [
        {"type": TURN_EVENT_TURN_START, "kind": TURN_EVENT_KIND_TURN_START},
        {"type": TURN_EVENT_TOOL_START, "kind": TURN_EVENT_KIND_TOOL_START},
        {"type": TURN_EVENT_TOOL_END, "kind": TURN_EVENT_KIND_TOOL_END},
        {"type": TURN_EVENT_TURN_END, "kind": TURN_EVENT_KIND_TURN_END},
    ],
}


def turn_event_capabilities() -> dict[str, Any]:
    """Return a copy of the supported internal turn-event protocol manifest."""
    return {
        "namespace": TURN_EVENT_CAPABILITIES["namespace"],
        "version": TURN_EVENT_CAPABILITIES["version"],
        "events": [dict(item) for item in TURN_EVENT_CAPABILITIES["events"]],
    }
