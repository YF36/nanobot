import json
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nanobot.agent.memory import MemoryStore
from nanobot.providers.base import LLMResponse, ToolCallRequest
from nanobot.session.manager import Session


def test_append_daily_history_entry_creates_template_and_appends_topics(tmp_path: Path) -> None:
    mm = MemoryStore(workspace=tmp_path)

    path1 = mm.append_daily_history_entry("[2026-02-25 10:00] First summary.")
    path2 = mm.append_daily_history_entry("[2026-02-25 11:00] Second summary.")

    assert path1 == path2
    assert path1.name == "2026-02-25.md"
    content = path1.read_text(encoding="utf-8")
    assert content.startswith("# 2026-02-25\n\n")
    assert "## Topics" in content
    assert "## Decisions" in content
    assert "## Tool Activity" in content
    assert "## Open Questions" in content
    assert "- First summary." in content
    assert "- Second summary." in content


def test_append_daily_history_entry_routes_to_sections_by_simple_heuristics(tmp_path: Path) -> None:
    mm = MemoryStore(workspace=tmp_path)

    path = mm.append_daily_history_entry("[2026-02-25 10:00] Decision: use M2-min first for safety.")
    mm.append_daily_history_entry("[2026-02-25 10:05] Ran exec command to inspect memory files.")

    content = path.read_text(encoding="utf-8")
    decisions_idx = content.index("## Decisions")
    tools_idx = content.index("## Tool Activity")
    assert "Decision: use M2-min first" in content[decisions_idx:tools_idx]
    open_idx = content.index("## Open Questions")
    assert "Ran exec command" in content[tools_idx:open_idx]


def test_append_daily_history_entry_keeps_legacy_entries_file_compatible(tmp_path: Path) -> None:
    mm = MemoryStore(workspace=tmp_path)
    legacy = mm.memory_dir / "2026-02-25.md"
    legacy.write_text("# 2026-02-25\n\n## Entries\n\n", encoding="utf-8")

    mm.append_daily_history_entry("[2026-02-25 10:00] Legacy append path works.")

    content = legacy.read_text(encoding="utf-8")
    assert "## Entries" in content
    assert "Legacy append path works." in content


def test_append_daily_history_entry_deduplicates_exact_bullets_within_section(tmp_path: Path) -> None:
    mm = MemoryStore(workspace=tmp_path)

    mm.append_daily_history_entry("[2026-02-25 10:00] Duplicate summary.")
    mm.append_daily_history_entry("[2026-02-25 10:05] Duplicate summary.")

    content = (mm.memory_dir / "2026-02-25.md").read_text(encoding="utf-8")
    assert content.count("- Duplicate summary.") == 1


def test_compact_fallback_daily_bullet_removes_templated_prefix_and_meta_clause() -> None:
    text = (
        "User asked about memory design. This interaction indicates a requirement for long-term retention."
    )
    compact = MemoryStore._compact_fallback_daily_bullet(text)
    assert compact == "about memory design."


def test_append_daily_history_entry_applies_compact_fallback(tmp_path: Path) -> None:
    mm = MemoryStore(workspace=tmp_path)
    mm.append_daily_history_entry(
        "[2026-02-25 10:00] User asked about memory design. No new information added."
    )
    content = (mm.memory_dir / "2026-02-25.md").read_text(encoding="utf-8")
    assert "- about memory design." in content
    assert "No new information added" not in content


def test_normalize_daily_sections_detailed_reports_quality_reasons() -> None:
    normalized, reason = MemoryStore._normalize_daily_sections_detailed(None)
    assert normalized is None and reason == "missing"

    normalized, reason = MemoryStore._normalize_daily_sections_detailed({"topics": []})
    assert normalized is None and reason == "empty"

    normalized, reason = MemoryStore._normalize_daily_sections_detailed({"topics": "bad"})
    assert normalized is None and reason == "invalid_type:topics"

    normalized, reason = MemoryStore._normalize_daily_sections_detailed({"topics": ["  A  ", ""]})
    assert reason == "ok"
    assert normalized == {"topics": ["A"]}


def test_normalize_daily_sections_drops_code_block_like_bullets() -> None:
    normalized, reason = MemoryStore._normalize_daily_sections_detailed(
        {"topics": ["keep this", "```drop me```"]}
    )
    assert reason == "ok"
    assert normalized == {"topics": ["keep this"]}


def test_normalize_history_entry_adds_timestamp_and_trims() -> None:
    text, reason = MemoryStore._normalize_history_entry("Discussed memory cleanup strategy.")
    assert reason == "ok"
    assert text is not None
    assert text.startswith("[20")
    assert "Discussed memory cleanup strategy." in text

    long_text = "x" * 1000
    text2, reason2 = MemoryStore._normalize_history_entry(long_text)
    assert reason2 == "ok"
    assert text2 is not None
    assert len(text2) <= MemoryStore._HISTORY_ENTRY_MAX_CHARS + 32


def test_append_daily_sections_detailed_returns_observability_details(tmp_path: Path) -> None:
    mm = MemoryStore(workspace=tmp_path)

    path, ok, details = mm.append_daily_sections_detailed("2026-02-25", {"topics": ["hello"]})

    assert ok is True
    assert path.name == "2026-02-25.md"
    assert details["reason"] == "ok"
    assert details["keys"] == ["topics"]
    assert details["bullet_count"] == 1
    assert details["created"] is True

    _, ok2, details2 = mm.append_daily_sections_detailed("2026-02-25", {"topics": "bad"})
    assert ok2 is False
    assert details2["reason"] == "invalid_type:topics"
    assert details2["bullet_count"] == 0


def test_append_daily_sections_writes_structured_bullets(tmp_path: Path) -> None:
    mm = MemoryStore(workspace=tmp_path)

    path, ok = mm.append_daily_sections(
        "2026-02-25",
        {
            "topics": ["讨论了 memory 分层"],
            "decisions": ["先做 M2-min 再做 M2-full"],
            "tool_activity": ["运行 pytest 验证"],
            "open_questions": ["是否引入 TTL"],
        },
    )

    assert ok is True
    content = path.read_text(encoding="utf-8")
    assert "- 讨论了 memory 分层" in content
    assert "- 先做 M2-min 再做 M2-full" in content
    assert "- 运行 pytest 验证" in content
    assert "- 是否引入 TTL" in content


def test_append_daily_sections_deduplicates_exact_bullets(tmp_path: Path) -> None:
    mm = MemoryStore(workspace=tmp_path)

    path, ok, details = mm.append_daily_sections_detailed(
        "2026-02-25",
        {
            "topics": ["same bullet", "same bullet"],
            "tool_activity": ["run pytest", "run pytest"],
        },
    )

    assert ok is True
    assert details["bullet_count"] == 2
    content = path.read_text(encoding="utf-8")
    assert content.count("- same bullet") == 1
    assert content.count("- run pytest") == 1


def test_get_recent_daily_context_returns_recent_bullets_only(tmp_path: Path) -> None:
    mm = MemoryStore(workspace=tmp_path)
    today = datetime.now().strftime("%Y-%m-%d")
    mm.append_daily_sections(
        today,
        {"topics": ["recent topic"], "decisions": ["recent decision"], "tool_activity": ["run pytest"]},
    )
    old = mm.memory_dir / "2020-01-01.md"
    old.write_text("# 2020-01-01\n\n## Topics\n\n- old topic\n", encoding="utf-8")

    context = mm.get_recent_daily_context(days=1, max_bullets=5, max_chars=500)
    assert "recent topic" in context
    assert "recent decision" in context
    assert "[Topics]" in context
    assert "[Decisions]" in context
    assert "run pytest" not in context
    assert "old topic" not in context


def test_get_recent_daily_context_can_include_tool_activity(tmp_path: Path) -> None:
    mm = MemoryStore(workspace=tmp_path)
    today = datetime.now().strftime("%Y-%m-%d")
    mm.append_daily_sections(today, {"tool_activity": ["run pytest"]})

    context = mm.get_recent_daily_context(days=1, max_bullets=5, max_chars=500, include_tool_activity=True)
    assert "run pytest" in context
    assert "[Tool Activity]" in context


def test_consolidation_system_prompt_restricts_memory_update_to_long_term_facts() -> None:
    prompt = MemoryStore._consolidation_system_prompt()
    assert "long-term stable facts only" in prompt
    assert "Do NOT copy recent discussion topics" in prompt
    assert "history_entry only" in prompt


def test_save_memory_tool_schema_supports_optional_daily_sections() -> None:
    tool = MemoryStore.__dict__.get("_SAVE_MEMORY_TOOL") if False else None
    # Access module-level schema through imported module object
    from nanobot.agent import memory as memory_module

    props = memory_module._SAVE_MEMORY_TOOL[0]["function"]["parameters"]["properties"]
    assert "daily_sections" in props
    daily_props = props["daily_sections"]["properties"]
    assert set(daily_props) == {"topics", "decisions", "tool_activity", "open_questions"}


def test_consolidation_system_prompt_discourages_transient_system_status() -> None:
    prompt = MemoryStore._consolidation_system_prompt()
    assert "Temporary system/API error statuses" in prompt
    assert "dated operational notes" in prompt


def test_sanitize_memory_update_removes_recent_topic_sections() -> None:
    current = "# Long-term Memory\n\n## Preferences\n- 中文沟通\n"
    update = (
        "# Long-term Memory\n\n"
        "## Preferences\n- 中文沟通\n\n"
        "## 今天讨论的主题 (2026-02-25)\n"
        "- 动漫剧情\n- 百科资料表格\n"
    )

    sanitized, removed = MemoryStore._sanitize_memory_update(update, current)

    assert "今天讨论的主题" in removed[0]
    assert "## 今天讨论的主题" not in sanitized
    assert "## Preferences" in sanitized


def test_sanitize_memory_update_strips_transient_status_lines_but_keeps_durable_facts() -> None:
    current = "# Long-term Memory\n\n## System Technical Issues\n- Feishu is primary channel.\n"
    update = (
        "# Long-term Memory\n\n"
        "## System Technical Issues\n"
        "- 2026-02-25 Brave Search API returned 422 error today\n"
        "- Temporary timeout observed on a tool call\n"
        "- Feishu is primary channel for notifications.\n\n"
        "## Preferences\n- 中文沟通\n"
    )

    sanitized, removed = MemoryStore._sanitize_memory_update(update, current)

    assert removed == []
    assert "422 error" not in sanitized
    assert "Temporary timeout" not in sanitized
    assert "Feishu is primary channel" in sanitized
    assert "## System Technical Issues" in sanitized


def test_sanitize_memory_update_detailed_reports_reason_categories() -> None:
    current = "# Long-term Memory\n"
    update = (
        "# Long-term Memory\n\n"
        "## 今天讨论的主题 (2026-02-25)\n- 百科问答\n\n"
        "## System Technical Issues\n"
        "- 2026-02-25 service timeout error\n"
        "- Feishu is primary channel.\n"
    )

    sanitized, details = MemoryStore._sanitize_memory_update_detailed(update, current)

    assert "今天讨论的主题" not in sanitized
    assert details["removed_recent_topic_sections"]
    assert "今天讨论的主题" in details["removed_recent_topic_sections"][0]
    assert details["removed_transient_status_line_count"] == 1
    assert "System Technical Issues" in details["removed_transient_status_sections"]
    assert details["recent_topic_section_samples"]
    assert "今天讨论的主题" in details["recent_topic_section_samples"][0]
    assert details["transient_status_line_samples"]
    assert "service timeout error" in details["transient_status_line_samples"][0]


def test_memory_update_guard_detects_excessive_shrink() -> None:
    current = (
        "# Long-term Memory\n\n"
        "## Preferences\n- 中文沟通\n\n"
        "## Project Context\n- "
        + ("memory roadmap details " * 20)
        + "\n\n"
        "## Constraints\n- local-first\n"
    )
    candidate = "# Long-term Memory\n\n## Preferences\n- 中文沟通\n"
    reason = MemoryStore._memory_update_guard_reason(current, candidate)
    assert reason == "excessive_shrink"


def test_memory_update_guard_detects_heading_retention_drop() -> None:
    current = (
        "# Long-term Memory\n\n"
        "## Preferences\n- 中文沟通\n\n"
        "## Project Context\n- memory roadmap\n\n"
        "## Constraints\n- local-first\n"
    )
    candidate = (
        "# Long-term Memory\n\n"
        "## New Section\n- unrelated\n"
    )
    reason = MemoryStore._memory_update_guard_reason(current, candidate)
    assert reason == "heading_retention_too_low"


@pytest.mark.asyncio
async def test_consolidate_accepts_json_string_tool_arguments(tmp_path: Path) -> None:
    mm = MemoryStore(workspace=tmp_path)
    mm.write_long_term("# Long-term Memory\n")

    session = Session(key="test:json_args")
    for i in range(60):
        session.add_message("user", f"msg{i}")

    provider = MagicMock()

    async def _fake_chat(**kwargs):
        return LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(
                    id="t1",
                    name="save_memory",
                    arguments=json.dumps({
                        "history_entry": "[2026-02-25 10:00] Discussed JSON string tool args.",
                        "memory_update": "# Long-term Memory\n",
                        "daily_sections": {"topics": ["JSON args path works"]},
                    }),
                )
            ],
        )

    provider.chat = _fake_chat
    result = await mm.consolidate(session=session, provider=provider, model="test", memory_window=50)

    assert result is True
    assert "Discussed JSON string tool args" in mm.history_file.read_text(encoding="utf-8")
    daily_text = (mm.memory_dir / "2026-02-25.md").read_text(encoding="utf-8")
    assert "JSON args path works" in daily_text


@pytest.mark.asyncio
async def test_consolidate_prefers_structured_daily_sections_when_present(tmp_path: Path) -> None:
    mm = MemoryStore(workspace=tmp_path)
    mm.write_long_term("# Long-term Memory\n")

    session = Session(key="test:daily_sections_ok")
    for i in range(60):
        session.add_message("user", f"msg{i}")

    provider = MagicMock()

    async def _fake_chat(**kwargs):
        return LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(
                    id="t1",
                    name="save_memory",
                    arguments={
                        "history_entry": "[2026-02-25 10:00] Discussed memory migration plan.",
                        "memory_update": "# Long-term Memory\n",
                        "daily_sections": {
                            "decisions": ["Use structured daily sections first."],
                            "open_questions": ["When to add TTL janitor?"],
                        },
                    },
                )
            ],
        )

    provider.chat = _fake_chat
    result = await mm.consolidate(session=session, provider=provider, model="test", memory_window=50)

    assert result is True
    daily_text = (mm.memory_dir / "2026-02-25.md").read_text(encoding="utf-8")
    assert "Use structured daily sections first." in daily_text
    assert "When to add TTL janitor?" in daily_text
    assert "Discussed memory migration plan." not in daily_text
    metrics_path = mm.memory_dir / "daily-routing-metrics.jsonl"
    metrics = [json.loads(line) for line in metrics_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(metrics) == 1
    assert metrics[0]["session_key"] == "test:daily_sections_ok"
    assert metrics[0]["structured_daily_ok"] is True
    assert metrics[0]["fallback_reason"] == "ok"
    assert metrics[0]["structured_bullet_count"] == 2


@pytest.mark.asyncio
async def test_consolidate_falls_back_when_daily_sections_invalid(tmp_path: Path) -> None:
    mm = MemoryStore(workspace=tmp_path)
    mm.write_long_term("# Long-term Memory\n")

    session = Session(key="test:daily_sections_bad")
    for i in range(60):
        session.add_message("user", f"msg{i}")

    provider = MagicMock()

    async def _fake_chat(**kwargs):
        return LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(
                    id="t1",
                    name="save_memory",
                    arguments={
                        "history_entry": "[2026-02-25 10:00] Ran exec command to inspect memory files.",
                        "memory_update": "# Long-term Memory\n",
                        "daily_sections": {
                            "tool_activity": "not-a-list",
                        },
                    },
                )
            ],
        )

    provider.chat = _fake_chat
    result = await mm.consolidate(session=session, provider=provider, model="test", memory_window=50)

    assert result is True
    daily_text = (mm.memory_dir / "2026-02-25.md").read_text(encoding="utf-8")
    assert "Ran exec command to inspect memory files." in daily_text
    metrics_path = mm.memory_dir / "daily-routing-metrics.jsonl"
    metrics = [json.loads(line) for line in metrics_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(metrics) == 1
    assert metrics[0]["session_key"] == "test:daily_sections_bad"
    assert metrics[0]["structured_daily_ok"] is False
    assert metrics[0]["fallback_used"] is True
    assert metrics[0]["fallback_reason"] == "invalid_type:tool_activity"


@pytest.mark.asyncio
async def test_consolidate_sanitizes_memory_update_before_write(tmp_path: Path) -> None:
    mm = MemoryStore(workspace=tmp_path)
    mm.write_long_term("# Long-term Memory\n\n## Preferences\n- 中文沟通\n")

    session = Session(key="test:memory_rules")
    for i in range(60):
        session.add_message("user", f"msg{i}")

    provider = MagicMock()

    async def _fake_chat(**kwargs):
        return LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(
                    id="t1",
                    name="save_memory",
                    arguments={
                        "history_entry": "[2026-02-25 10:00] Discussed anime topics and preferences.",
                        "memory_update": (
                            "# Long-term Memory\n\n"
                            "## Preferences\n- 中文沟通\n\n"
                            "## 今天讨论的主题 (2026-02-25)\n"
                            "- 动漫剧情\n- 百科资料表格\n"
                        ),
                    },
                )
            ],
        )

    provider.chat = _fake_chat

    result = await mm.consolidate(session=session, provider=provider, model="test", memory_window=50)

    assert result is True
    written_memory = mm.read_long_term()
    assert "## Preferences" in written_memory
    assert "今天讨论的主题" not in written_memory
    history_text = mm.history_file.read_text(encoding="utf-8")
    assert "Discussed anime topics" in history_text
    daily_text = (mm.memory_dir / "2026-02-25.md").read_text(encoding="utf-8")
    assert "## Topics" in daily_text
    assert "Discussed anime topics and preferences" in daily_text


@pytest.mark.asyncio
async def test_consolidate_skips_history_entry_when_quality_gate_rejects(tmp_path: Path) -> None:
    mm = MemoryStore(workspace=tmp_path)
    mm.write_long_term("# Long-term Memory\n\n## Preferences\n- 中文沟通\n")

    session = Session(key="test:history_gate")
    for i in range(60):
        session.add_message("user", f"msg{i}")

    provider = MagicMock()

    async def _fake_chat(**kwargs):
        return LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(
                    id="t1",
                    name="save_memory",
                    arguments={
                        "history_entry": "```raw dump```",
                        "memory_update": "# Long-term Memory\n\n## Preferences\n- 中文沟通\n",
                    },
                )
            ],
        )

    provider.chat = _fake_chat
    result = await mm.consolidate(session=session, provider=provider, model="test", memory_window=50)

    assert result is True
    history_text = mm.history_file.read_text(encoding="utf-8") if mm.history_file.exists() else ""
    assert history_text.strip() == ""


@pytest.mark.asyncio
async def test_consolidate_skips_memory_update_when_guard_triggers(tmp_path: Path) -> None:
    mm = MemoryStore(workspace=tmp_path)
    current = (
        "# Long-term Memory\n\n"
        "## Preferences\n- 中文沟通\n\n"
        "## Project Context\n- memory roadmap\n\n"
        "## Constraints\n- local-first\n"
    )
    mm.write_long_term(current)

    session = Session(key="test:memory_update_guard")
    for i in range(60):
        session.add_message("user", f"msg{i}")

    provider = MagicMock()

    async def _fake_chat(**kwargs):
        return LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(
                    id="t1",
                    name="save_memory",
                    arguments={
                        "history_entry": "[2026-02-25 10:00] Keep stable memory only.",
                        "memory_update": "# Long-term Memory\n\n## Preferences\n- 中文沟通\n",
                    },
                )
            ],
        )

    provider.chat = _fake_chat
    result = await mm.consolidate(session=session, provider=provider, model="test", memory_window=50)

    assert result is True
    assert mm.read_long_term() == current
    guard_metrics_path = mm.memory_dir / "memory-update-guard-metrics.jsonl"
    rows = [json.loads(line) for line in guard_metrics_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(rows) == 1
    assert rows[0]["session_key"] == "test:memory_update_guard"
    assert rows[0]["reason"] in {"excessive_shrink", "heading_retention_too_low"}


@pytest.mark.asyncio
async def test_consolidate_records_preference_conflict_metric(tmp_path: Path) -> None:
    mm = MemoryStore(workspace=tmp_path)
    current = (
        "# Long-term Memory\n\n"
        "## Preferences\n"
        "- 语言: 中文\n"
        "- 沟通风格: 技术讨论\n"
    )
    mm.write_long_term(current)

    session = Session(key="test:memory_conflict")
    for i in range(60):
        session.add_message("user", f"msg{i}")

    provider = MagicMock()

    async def _fake_chat(**kwargs):
        return LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(
                    id="t1",
                    name="save_memory",
                    arguments={
                        "history_entry": "[2026-02-25 10:00] Preference updated.",
                        "memory_update": (
                            "# Long-term Memory\n\n"
                            "## Preferences\n"
                            "- 语言: English\n"
                            "- 沟通风格: 技术讨论\n"
                        ),
                    },
                )
            ],
        )

    provider.chat = _fake_chat
    result = await mm.consolidate(session=session, provider=provider, model="test", memory_window=50)

    assert result is True
    metrics_path = mm.memory_dir / "memory-conflict-metrics.jsonl"
    rows = [json.loads(line) for line in metrics_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(rows) == 1
    assert rows[0]["session_key"] == "test:memory_conflict"
    assert rows[0]["conflict_key"] == "language"
