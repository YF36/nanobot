from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nanobot.agent.memory import MemoryStore
from nanobot.providers.base import LLMResponse, ToolCallRequest
from nanobot.session.manager import Session


def test_consolidation_system_prompt_restricts_memory_update_to_long_term_facts() -> None:
    prompt = MemoryStore._consolidation_system_prompt()
    assert "long-term stable facts only" in prompt
    assert "Do NOT copy recent discussion topics" in prompt
    assert "history_entry only" in prompt


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
