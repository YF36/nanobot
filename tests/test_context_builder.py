import json

from nanobot.agent.context import ContextBuilder


def _builder(tmp_path):
    return ContextBuilder(tmp_path)


def test_compact_history_preserves_consecutive_tool_messages(tmp_path) -> None:
    builder = _builder(tmp_path)
    history = [
        {"role": "user", "content": "Run two checks"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "check_a", "arguments": "{}"},
                },
                {
                    "id": "call_2",
                    "type": "function",
                    "function": {"name": "check_b", "arguments": "{}"},
                },
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "name": "check_a", "content": "ok"},
        {"role": "tool", "tool_call_id": "call_2", "name": "check_b", "content": "ok"},
    ]

    compacted = builder._compact_history(history)

    assert [m["role"] for m in compacted] == ["user", "assistant", "tool", "tool"]
    assert compacted[2]["tool_call_id"] == "call_1"
    assert compacted[3]["tool_call_id"] == "call_2"
    assert compacted[2]["content"] == "ok"
    assert compacted[3]["content"] == "ok"


def test_trim_history_keeps_whole_recent_turn_chunk(tmp_path) -> None:
    builder = _builder(tmp_path)
    long_args = json.dumps({"payload": "x" * 400}, ensure_ascii=False)
    chunk1 = [
        {"role": "user", "content": "First question"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "search", "arguments": long_args},
                },
                {
                    "id": "call_2",
                    "type": "function",
                    "function": {"name": "fetch", "arguments": long_args},
                },
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "name": "search", "content": "result A"},
        {"role": "tool", "tool_call_id": "call_2", "name": "fetch", "content": "result B"},
        {"role": "assistant", "content": "Combined result"},
    ]
    chunk2 = [
        {"role": "user", "content": "Second question"},
        {"role": "assistant", "content": "Short answer"},
    ]
    history = chunk1 + chunk2

    chunk2_budget = sum(builder._estimate_message_chars(m) for m in chunk2)
    trimmed = builder._trim_history(history, budget_chars=chunk2_budget)

    assert trimmed == chunk2
    assert trimmed[0]["role"] == "user"
    assert trimmed[0]["content"] == "Second question"


def test_estimate_message_chars_counts_tool_calls_arguments(tmp_path) -> None:
    builder = _builder(tmp_path)
    args = json.dumps({"query": "x" * 120}, ensure_ascii=False)
    msg = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "search", "arguments": args},
            }
        ],
    }

    assert builder._estimate_message_chars(msg) >= len(args)


def test_build_messages_drops_history_when_budget_is_negative(tmp_path) -> None:
    builder = _builder(tmp_path)
    builder.build_system_prompt = lambda skill_names=None: "s" * 200  # type: ignore[method-assign]
    builder._MAX_CONTEXT_TOKENS = 10  # 40 chars total budget

    history = [
        {"role": "user", "content": "older question"},
        {"role": "assistant", "content": "older answer"},
    ]

    messages = builder.build_messages(history=history, current_message="x" * 100)

    assert [m["role"] for m in messages] == ["system", "user"]
    assert messages[-1]["content"] == "x" * 100
