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

    chunk2_budget = sum(builder._estimate_message_tokens(m) for m in chunk2)
    trimmed = builder._trim_history(history, budget_tokens=chunk2_budget)

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

    tokens = builder._estimate_message_tokens(msg)
    # Must be positive and account for the arguments string (at least a few tokens)
    assert tokens > 0
    # Must be less than len(args) (tiktoken is more efficient than char/4)
    assert tokens < len(args)


def test_build_messages_drops_history_when_budget_is_negative(tmp_path) -> None:
    builder = _builder(tmp_path)
    # system prompt = 50 tokens, current message = 25 tokens → budget exhausted
    builder.build_system_prompt = lambda skill_names=None: "word " * 50  # type: ignore[method-assign]
    builder._max_context_tokens = 80  # leaves no room for history

    history = [
        {"role": "user", "content": "older question"},
        {"role": "assistant", "content": "older answer"},
    ]

    messages = builder.build_messages(history=history, current_message="word " * 25)

    assert [m["role"] for m in messages] == ["system", "user"]


def test_build_messages_counts_current_image_payload_in_budget(tmp_path) -> None:
    builder = _builder(tmp_path)
    builder.build_system_prompt = lambda skill_names=None: "sys"  # type: ignore[method-assign]
    # Image url is ~150 chars → ~37 tokens; system ~1 token; reply reserve 4096
    # Set budget so image + system + reserve exhausts it
    builder._max_context_tokens = 4100
    builder._build_user_content = lambda text, media: [  # type: ignore[method-assign]
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + ("x" * 120)}},
        {"type": "text", "text": text},
    ]

    history = [
        {"role": "user", "content": "older question"},
        {"role": "assistant", "content": "older answer"},
    ]

    messages = builder.build_messages(history=history, current_message="hi", media=["dummy.jpg"])

    # Image payload consumes the budget; history should be dropped.
    assert [m["role"] for m in messages] == ["system", "user"]
    assert isinstance(messages[-1]["content"], list)
