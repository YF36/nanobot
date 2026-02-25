from nanobot.agent.turn_events import (
    TURN_EVENT_CAPABILITIES,
    TURN_EVENT_KIND_TOOL_END,
    TURN_EVENT_KIND_TOOL_START,
    TURN_EVENT_KIND_TURN_END,
    TURN_EVENT_KIND_TURN_START,
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
    assert caps["events"] == [
        {"type": TURN_EVENT_TURN_START, "kind": TURN_EVENT_KIND_TURN_START},
        {"type": TURN_EVENT_TOOL_START, "kind": TURN_EVENT_KIND_TOOL_START},
        {"type": TURN_EVENT_TOOL_END, "kind": TURN_EVENT_KIND_TOOL_END},
        {"type": TURN_EVENT_TURN_END, "kind": TURN_EVENT_KIND_TURN_END},
    ]


def test_turn_event_capabilities_returns_copy() -> None:
    caps = turn_event_capabilities()
    caps["events"].append({"type": "x", "kind": "y"})

    assert len(caps["events"]) == len(TURN_EVENT_CAPABILITIES["events"]) + 1
    assert len(TURN_EVENT_CAPABILITIES["events"]) == 4
