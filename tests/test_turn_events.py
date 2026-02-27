from nanobot.agent.turn_events import (
    TURN_EVENT_CAPABILITIES,
    TURN_EVENT_KIND_MESSAGE_DELTA,
    TURN_EVENT_KIND_TOOL_END,
    TURN_EVENT_KIND_TOOL_START,
    TURN_EVENT_KIND_TURN_END,
    TURN_EVENT_KIND_TURN_START,
    TURN_EVENT_MESSAGE_DELTA,
    TURN_EVENT_NAMESPACE,
    TURN_EVENT_SCHEMA_VERSION,
    TURN_EVENT_TOOL_END,
    TURN_EVENT_TOOL_START,
    TURN_EVENT_TURN_END,
    TURN_EVENT_TURN_START,
    turn_event_capabilities,
)


def test_turn_event_capabilities_manifest_matches_protocol_constants() -> None:
    caps = turn_event_capabilities()

    assert caps["namespace"] == TURN_EVENT_NAMESPACE
    assert caps["version"] == TURN_EVENT_SCHEMA_VERSION
    assert "kind" in caps["base_fields"]
    assert caps["events"] == [
        {
            "type": TURN_EVENT_TURN_START,
            "kind": TURN_EVENT_KIND_TURN_START,
            "fields": ["initial_message_count", "max_iterations"],
        },
        {
            "type": TURN_EVENT_MESSAGE_DELTA,
            "kind": TURN_EVENT_KIND_MESSAGE_DELTA,
            "fields": ["delta", "content_len"],
        },
        {
            "type": TURN_EVENT_TOOL_START,
            "kind": TURN_EVENT_KIND_TOOL_START,
            "fields": ["iteration", "tool", "tool_call_id", "arguments"],
        },
        {
            "type": TURN_EVENT_TOOL_END,
            "kind": TURN_EVENT_KIND_TOOL_END,
            "fields": ["iteration", "tool", "tool_call_id", "is_error", "has_details", "detail_op"],
        },
        {
            "type": TURN_EVENT_TURN_END,
            "kind": TURN_EVENT_KIND_TURN_END,
            "fields": [
                "iterations",
                "tool_count",
                "completed",
                "max_iterations_reached",
                "interrupted_for_followup",
                "llm_retry_count",
            ],
        },
    ]


def test_turn_event_capabilities_returns_copy() -> None:
    caps = turn_event_capabilities()
    caps["events"].append({"type": "x", "kind": "y"})
    caps["base_fields"].append("extra")

    assert len(caps["events"]) == len(TURN_EVENT_CAPABILITIES["events"]) + 1
    assert len(TURN_EVENT_CAPABILITIES["events"]) == 5
    assert "extra" not in TURN_EVENT_CAPABILITIES["base_fields"]
