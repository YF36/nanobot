from pathlib import Path

from nanobot.agent.memory_maintenance import build_cleanup_plan, run_memory_audit


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def test_run_memory_audit_detects_long_and_duplicate_content(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()

    _write(memory_dir / "MEMORY.md", "# Long-term memory\n\n[2026-02-27 10:00] should not be here\n")
    _write(
        memory_dir / "HISTORY.md",
        "[2026-02-27 10:00] " + ("x" * 620) + "\n\n"
        "[2026-02-27 10:00] repeat\n\n"
        "[2026-02-27 10:00] repeat\n\n",
    )
    _write(
        memory_dir / "2026-02-27.md",
        "# 2026-02-27\n\n## Topics\n\n- repeat\n- repeat\n- " + ("y" * 260) + "\n\n## Decisions\n\n",
    )
    _write(memory_dir / "2025-02-26.md", "# 2025-02-26\n\n## Topics\n\n- old file\n")

    audit = run_memory_audit(memory_dir)
    assert audit.history_entry_count == 3
    assert audit.history_long_entry_count == 1
    assert audit.history_duplicate_count == 1
    assert audit.daily_long_bullet_count == 1
    assert audit.daily_duplicate_count == 1
    assert "2025-02-26.md" in audit.daily_orphan_files
    assert audit.memory_timestamp_line_count == 1


def test_build_cleanup_plan_contains_expected_actions(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()

    _write(memory_dir / "MEMORY.md", "# Long-term memory\n\n[2026-02-27 10:00] should not be here\n")
    _write(memory_dir / "HISTORY.md", "[2026-02-27 10:00] " + ("x" * 620) + "\n\n")
    _write(memory_dir / "2026-02-27.md", "# 2026-02-27\n\n## Topics\n\n- " + ("y" * 260) + "\n")

    plan = build_cleanup_plan(memory_dir)
    actions = {item["type"] for item in plan["actions"]}  # type: ignore[index]
    assert "history_trim_long_entries" in actions
    assert "daily_trim_long_bullets" in actions
    assert "memory_remove_timestamp_like_lines" in actions
