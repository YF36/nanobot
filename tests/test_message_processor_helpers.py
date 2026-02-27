import pytest

from nanobot.agent.message_processor_helpers import TurnEventStatsCollector
from nanobot.agent.turn_events import (
    TURN_EVENT_KIND_MESSAGE_DELTA,
    TURN_EVENT_KIND_TOOL_END,
    TURN_EVENT_KIND_TOOL_START,
    TURN_EVENT_KIND_TURN_END,
    TURN_EVENT_KIND_TURN_START,
    TURN_EVENT_MESSAGE_DELTA,
    TURN_EVENT_TOOL_END,
    TURN_EVENT_TOOL_START,
    TURN_EVENT_TURN_END,
    TURN_EVENT_TURN_START,
)


def _event(event_type: str, **kwargs):
    kind_map = {
        TURN_EVENT_TURN_START: TURN_EVENT_KIND_TURN_START,
        TURN_EVENT_MESSAGE_DELTA: TURN_EVENT_KIND_MESSAGE_DELTA,
        TURN_EVENT_TOOL_START: TURN_EVENT_KIND_TOOL_START,
        TURN_EVENT_TOOL_END: TURN_EVENT_KIND_TOOL_END,
        TURN_EVENT_TURN_END: TURN_EVENT_KIND_TURN_END,
    }
    base = {
        "namespace": "nanobot.turn",
        "version": 1,
        "type": event_type,
        "kind": kind_map[event_type],
        "turn_id": "turn_test",
        "sequence": 1,
        "timestamp_ms": 1,
        "source": "test",
    }
    base.update(kwargs)
    return base


@pytest.mark.asyncio
async def test_turn_event_stats_collector_tracks_stream_deltas() -> None:
    c = TurnEventStatsCollector()
    await c.on_event(_event(TURN_EVENT_TURN_START, initial_message_count=1, max_iterations=5))
    await c.on_event(_event(TURN_EVENT_MESSAGE_DELTA, delta="Hello ", content_len=6))
    await c.on_event(_event(TURN_EVENT_MESSAGE_DELTA, delta="world", content_len=11))
    await c.on_event(
        _event(
            TURN_EVENT_TOOL_START,
            iteration=1,
            tool="exec",
            tool_call_id="call_1",
            arguments={"command": "echo hi"},
        )
    )
    await c.on_event(
        _event(
            TURN_EVENT_TOOL_END,
            iteration=1,
            tool="exec",
            tool_call_id="call_1",
            is_error=False,
            has_details=True,
            detail_op="exec",
        )
    )
    await c.on_event(
        _event(
            TURN_EVENT_TURN_END,
            iterations=1,
            tool_count=1,
            completed=True,
            max_iterations_reached=False,
        )
    )

    assert c.turns_started == 1
    assert c.turns_ended == 1
    assert c.tool_starts == 1
    assert c.tool_ends == 1
    assert c.message_delta_count == 2
    assert c.streamed_text_chars == len("Hello world")
    assert c.detail_ops == {"exec"}
