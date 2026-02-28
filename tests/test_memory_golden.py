import json
from pathlib import Path

import pytest

from nanobot.agent.memory import MemoryStore


_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "memory_golden"


def _load_fixture(name: str) -> dict:
    return json.loads((_FIXTURE_DIR / name).read_text(encoding="utf-8"))


@pytest.mark.golden
def test_golden_routing_missing_daily_sections(tmp_path: Path) -> None:
    case = _load_fixture("routing_missing_daily_sections.json")
    mm = MemoryStore(workspace=tmp_path)
    plan = mm._resolve_daily_routing_plan(
        entry_text=str(case["entry_text"]),
        raw_daily_sections=case["raw_daily_sections"],
    )
    assert plan.structured_source == case["expected_structured_source"]
    assert plan.model_daily_sections_reason == case["expected_model_daily_sections_reason"]


@pytest.mark.golden
def test_golden_sanitize_recent_topic_section(tmp_path: Path) -> None:
    case = _load_fixture("sanitize_recent_topic_section.json")
    mm = MemoryStore(workspace=tmp_path)
    sanitized, details = mm._sanitize_memory_update_detailed(
        str(case["candidate_update"]),
        str(case["current_memory"]),
    )
    assert len(list(details["removed_recent_topic_sections"])) == int(
        case["expected_removed_recent_topic_section_count"]
    )
    assert int(details["removed_transient_status_line_count"]) == int(
        case["expected_removed_transient_status_line_count"]
    )
    assert int(details["removed_duplicate_bullet_count"]) == int(
        case["expected_removed_duplicate_bullet_count"]
    )
    for text in case["expected_output_contains"]:
        assert str(text) in sanitized
    for text in case["expected_output_not_contains"]:
        assert str(text) not in sanitized


@pytest.mark.golden
def test_golden_guard_heading_retention_low(tmp_path: Path) -> None:
    case = _load_fixture("guard_heading_retention_low.json")
    mm = MemoryStore(workspace=tmp_path)
    reason = mm._memory_update_guard_reason(
        str(case["current_memory"]),
        str(case["candidate_update"]),
    )
    assert reason == case["expected_guard_reason"]

