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


def test_build_messages_strips_internal_tool_details_from_history(tmp_path) -> None:
    builder = _builder(tmp_path)
    history = [
        {"role": "user", "content": "edit file"},
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "name": "edit_file",
            "content": "Successfully edited ...",
            "_tool_details": {"op": "edit_file", "path": "/tmp/x"},
        },
    ]

    messages = builder.build_messages(history=history, current_message="next")

    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert "_tool_details" not in tool_msgs[0]


def test_add_tool_result_can_store_internal_metadata(tmp_path) -> None:
    builder = _builder(tmp_path)
    messages: list[dict] = []

    out = builder.add_tool_result(
        messages,
        tool_call_id="call_1",
        tool_name="edit_file",
        result="ok",
        metadata={"op": "edit_file", "first_changed_line": 3},
    )

    assert out is messages
    assert messages[-1]["_tool_details"]["op"] == "edit_file"


def test_build_system_prompt_parts_moves_rules_to_static_and_time_to_dynamic(tmp_path) -> None:
    builder = _builder(tmp_path)

    static, dynamic = builder.build_system_prompt_parts()

    assert "## Tool Call Guidelines" in static
    assert "## Current Time" not in static
    assert "## Current Time" in dynamic


def test_build_messages_includes_runtime_tool_catalog_when_provided(tmp_path) -> None:
    builder = _builder(tmp_path)
    tool_defs = [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read a file from the workspace with optional pagination.",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}, "offset": {"type": "integer"}},
                    "required": ["path"],
                },
            },
        }
    ]

    messages = builder.build_messages(
        history=[],
        current_message="hi",
        tool_definitions=tool_defs,
    )

    system_msg = messages[0]
    assert system_msg["role"] == "system"
    content = system_msg["content"]
    if isinstance(content, list):
        joined = "\n".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    else:
        joined = str(content)
    assert "## Runtime Tool Catalog" in joined
    assert "`read_file`" in joined
    assert "params: path, offset" in joined
    assert "required: path" in joined


def test_build_messages_omits_runtime_tool_catalog_when_no_tools(tmp_path) -> None:
    builder = _builder(tmp_path)
    messages = builder.build_messages(history=[], current_message="hi", tool_definitions=[])
    content = messages[0]["content"]
    if isinstance(content, list):
        joined = "\n".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    else:
        joined = str(content)
    assert "## Runtime Tool Catalog" not in joined


def test_build_messages_groups_runtime_tool_catalog_by_capability(tmp_path) -> None:
    builder = _builder(tmp_path)
    tool_defs = [
        {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "Read file.",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "exec",
                "description": "Run shell command.",
                "parameters": {"type": "object", "properties": {"command": {"type": "string"}}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "message",
                "description": "Send message to chat.",
                "parameters": {"type": "object", "properties": {"content": {"type": "string"}}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "spawn",
                "description": "Spawn subagent.",
                "parameters": {"type": "object", "properties": {"task": {"type": "string"}}},
            },
        },
    ]

    messages = builder.build_messages(history=[], current_message="hi", tool_definitions=tool_defs)
    content = messages[0]["content"]
    if isinstance(content, list):
        joined = "\n".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    else:
        joined = str(content)

    assert "### Filesystem" in joined
    assert "_Guidance: Prefer inspect/read before write; confirm paths in workspace._" in joined
    assert "### Shell" in joined
    assert "_Guidance: Prefer non-destructive checks first and avoid risky commands._" in joined
    assert "### Messaging" in joined
    assert "### Subagents" in joined
    assert "`read_file`" in joined
    assert "`exec`" in joined
    assert "`message`" in joined
    assert "`spawn`" in joined
    assert "note: prefer read-only checks first" in joined


def test_build_messages_adds_high_risk_tool_notes(tmp_path) -> None:
    builder = _builder(tmp_path)
    tool_defs = [
        {
            "type": "function",
            "function": {
                "name": "edit_file",
                "description": "Edit file content.",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}},
                    "required": ["path", "old_text", "new_text"],
                },
            },
        }
    ]

    messages = builder.build_messages(history=[], current_message="hi", tool_definitions=tool_defs)
    content = messages[0]["content"]
    if isinstance(content, list):
        joined = "\n".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    else:
        joined = str(content)

    assert "`edit_file`" in joined
    assert "note: read target first and verify path before modifying" in joined


def test_build_messages_sorts_tools_within_group_for_stability(tmp_path) -> None:
    builder = _builder(tmp_path)
    tool_defs = [
        {
            "type": "function",
            "function": {"name": "write_file", "description": "Write file.", "parameters": {"type": "object"}},
        },
        {
            "type": "function",
            "function": {"name": "edit_file", "description": "Edit file.", "parameters": {"type": "object"}},
        },
        {
            "type": "function",
            "function": {"name": "read_file", "description": "Read file.", "parameters": {"type": "object"}},
        },
    ]

    messages = builder.build_messages(history=[], current_message="hi", tool_definitions=tool_defs)
    content = messages[0]["content"]
    if isinstance(content, list):
        joined = "\n".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    else:
        joined = str(content)

    catalog = joined.split("## Runtime Tool Catalog", 1)[1]
    read_pos = catalog.index("`read_file`")
    edit_pos = catalog.index("`edit_file`")
    write_pos = catalog.index("`write_file`")
    assert edit_pos < read_pos < write_pos


def test_build_messages_uses_compact_tool_catalog_mode_for_many_tools(tmp_path) -> None:
    builder = _builder(tmp_path)
    tool_defs = []
    for i in range(12):
        tool_defs.append(
            {
                "type": "function",
                "function": {
                    "name": f"tool_{i}",
                    "description": "This is a long enough description to be visible in full mode.",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                    },
                },
            }
        )
    # Add one known high-risk tool so note suppression can be asserted in compact mode.
    tool_defs.append(
        {
            "type": "function",
            "function": {
                "name": "exec",
                "description": "Run shell command.",
                "parameters": {
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"],
                },
            },
        }
    )

    messages = builder.build_messages(history=[], current_message="hi", tool_definitions=tool_defs)
    content = messages[0]["content"]
    if isinstance(content, list):
        joined = "\n".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    else:
        joined = str(content)

    catalog = joined.split("## Runtime Tool Catalog", 1)[1]
    assert "Compact summary mode enabled due to tool count/length." in catalog
    assert "required: command" in catalog
    assert "note: prefer read-only checks first" not in catalog


def test_build_messages_uses_compact_tool_catalog_mode_for_long_descriptions(tmp_path, monkeypatch) -> None:
    builder = _builder(tmp_path)
    tool_defs = []
    for i in range(4):
        tool_defs.append(
            {
                "type": "function",
                "function": {
                    "name": f"search_tool_{i}",
                    "description": ("Very long description segment. " * 40).strip(),
                    "parameters": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                    },
                },
            }
        )
    # Force compact fallback by length with fewer than count threshold tools.
    monkeypatch.setattr(ContextBuilder, "_TOOL_CATALOG_MAX_CHARS_BEFORE_COMPACT", 250)

    messages = builder.build_messages(history=[], current_message="hi", tool_definitions=tool_defs)
    content = messages[0]["content"]
    if isinstance(content, list):
        joined = "\n".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    else:
        joined = str(content)

    catalog = joined.split("## Runtime Tool Catalog", 1)[1]
    assert "Compact summary mode enabled due to tool count/length." in catalog
    # In compact mode, verbose descriptions are removed while required hints remain.
    assert "required: query" in catalog
