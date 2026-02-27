from pathlib import Path
from datetime import datetime, timedelta

from nanobot.agent.memory_maintenance import (
    apply_conservative_cleanup,
    build_cleanup_plan,
    render_context_trace_markdown,
    render_memory_observability_dashboard,
    render_daily_archive_dry_run_markdown,
    render_cleanup_effect_markdown,
    render_memory_conflict_metrics_markdown,
    render_daily_routing_metrics_markdown,
    render_memory_update_guard_metrics_markdown,
    run_memory_audit,
    summarize_context_trace,
    summarize_daily_archive_dry_run,
    summarize_memory_conflict_metrics,
    summarize_memory_update_guard_metrics,
    summarize_daily_routing_metrics,
)


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


def test_apply_conservative_cleanup_trims_and_deduplicates_with_backup(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()

    _write(
        memory_dir / "HISTORY.md",
        "[2026-02-27 10:00] " + ("x" * 620) + "\n\n"
        "[2026-02-27 10:00] repeat\n\n"
        "[2026-02-27 10:00] repeat\n\n",
    )
    _write(
        memory_dir / "2026-02-27.md",
        "# 2026-02-27\n\n## Topics\n\n- repeat\n- repeat\n- " + ("y" * 260) + "\n",
    )

    result = apply_conservative_cleanup(memory_dir)
    assert result.touched_files == ["2026-02-27.md", "HISTORY.md"]
    assert result.history_trimmed_entries == 1
    assert result.history_deduplicated_entries == 1
    assert result.daily_trimmed_bullets == 1
    assert result.daily_deduplicated_bullets == 1
    assert result.scoped_daily_files == 1
    assert result.skipped_daily_files == 0

    history_after = (memory_dir / "HISTORY.md").read_text(encoding="utf-8")
    assert history_after.count("repeat") == 1
    daily_after = (memory_dir / "2026-02-27.md").read_text(encoding="utf-8")
    assert daily_after.count("- repeat") == 1

    assert result.backup_dir.exists()
    assert (result.backup_dir / "HISTORY.md").exists()
    assert (result.backup_dir / "2026-02-27.md").exists()


def test_summarize_daily_routing_metrics_counts_and_reasons(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    _write(
        memory_dir / "daily-routing-metrics.jsonl",
        "\n".join(
            [
                '{"date":"2026-02-27","structured_daily_ok":true,"fallback_reason":"ok"}',
                '{"date":"2026-02-27","structured_daily_ok":false,"fallback_reason":"missing"}',
                '{"date":"2026-02-28","structured_daily_ok":false,"fallback_reason":"invalid_type:topics"}',
                "not-json",
            ]
        )
        + "\n",
    )

    summary = summarize_daily_routing_metrics(memory_dir)
    assert summary.metrics_file_exists is True
    assert summary.total_rows == 4
    assert summary.parse_error_rows == 1
    assert summary.structured_ok_count == 1
    assert summary.fallback_count == 2
    assert summary.fallback_reason_counts["missing"] == 1
    assert summary.fallback_reason_counts["invalid_type:topics"] == 1
    assert summary.by_date["2026-02-27"]["total"] == 2
    assert summary.by_date["2026-02-27"]["structured_ok"] == 1
    assert summary.by_date["2026-02-28"]["fallback"] == 1
    rendered = render_daily_routing_metrics_markdown(summary)
    assert "## Suggested Fixes" in rendered
    assert "invalid_type:topics" in rendered
    assert "should be `string[]`" in rendered


def test_render_daily_routing_metrics_markdown_handles_missing_file(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    summary = summarize_daily_routing_metrics(memory_dir)
    text = render_daily_routing_metrics_markdown(summary)
    assert "Metrics file: not found" in text


def test_summarize_memory_update_guard_metrics_counts_reasons(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    _write(
        memory_dir / "memory-update-guard-metrics.jsonl",
        "\n".join(
            [
                '{"session_key":"s1","reason":"excessive_shrink"}',
                '{"session_key":"s1","reason":"excessive_shrink"}',
                '{"session_key":"s2","reason":"heading_retention_too_low"}',
                "not-json",
            ]
        )
        + "\n",
    )
    summary = summarize_memory_update_guard_metrics(memory_dir)
    assert summary.metrics_file_exists is True
    assert summary.total_rows == 4
    assert summary.parse_error_rows == 1
    assert summary.reason_counts["excessive_shrink"] == 2
    assert summary.reason_counts["heading_retention_too_low"] == 1
    assert summary.by_session["s1"] == 2
    assert summary.by_session["s2"] == 1


def test_render_memory_update_guard_metrics_markdown_handles_missing_file(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    summary = summarize_memory_update_guard_metrics(memory_dir)
    text = render_memory_update_guard_metrics_markdown(summary)
    assert "Metrics file: not found" in text


def test_summarize_memory_conflict_metrics_counts_keys(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    _write(
        memory_dir / "memory-conflict-metrics.jsonl",
        "\n".join(
            [
                '{"session_key":"s1","conflict_key":"language"}',
                '{"session_key":"s1","conflict_key":"language"}',
                '{"session_key":"s2","conflict_key":"communication_style"}',
                "not-json",
            ]
        )
        + "\n",
    )
    summary = summarize_memory_conflict_metrics(memory_dir)
    assert summary.metrics_file_exists is True
    assert summary.total_rows == 4
    assert summary.parse_error_rows == 1
    assert summary.key_counts["language"] == 2
    assert summary.key_counts["communication_style"] == 1
    text = render_memory_conflict_metrics_markdown(summary)
    assert "Conflict Keys" in text


def test_render_memory_conflict_metrics_markdown_handles_missing_file(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    summary = summarize_memory_conflict_metrics(memory_dir)
    text = render_memory_conflict_metrics_markdown(summary)
    assert "Metrics file: not found" in text


def test_summarize_context_trace_reports_stage_counts_and_stability(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    _write(
        memory_dir / "context-trace.jsonl",
        "\n".join(
            [
                '{"stage":"before_compact","estimated_tokens":100,"prefix_hash":"a1"}',
                '{"stage":"after_compact","estimated_tokens":80,"prefix_hash":"a1"}',
                '{"stage":"before_send","estimated_tokens":120,"prefix_hash":"a1"}',
                '{"stage":"before_send","estimated_tokens":110,"prefix_hash":"a1"}',
                '{"stage":"before_send","estimated_tokens":130,"prefix_hash":"b2"}',
                "not-json",
            ]
        )
        + "\n",
    )
    summary = summarize_context_trace(memory_dir)
    assert summary.trace_file_exists is True
    assert summary.total_rows == 6
    assert summary.parse_error_rows == 1
    assert summary.by_stage["before_send"] == 3
    assert summary.avg_tokens_by_stage["before_send"] == 120
    assert 0.0 <= summary.prefix_stability_ratio <= 1.0
    text = render_context_trace_markdown(summary)
    assert "Prefix stability ratio" in text


def test_render_context_trace_markdown_handles_missing_file(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    summary = summarize_context_trace(memory_dir)
    text = render_context_trace_markdown(summary)
    assert "Trace file: not found" in text


def test_render_memory_observability_dashboard_contains_sections(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    _write(memory_dir / "MEMORY.md", "# Long-term Memory\n")
    _write(memory_dir / "HISTORY.md", "")
    _write(memory_dir / "2020-01-01.md", "# 2020-01-01\n\n## Topics\n\n- old\n- old\n")
    _write(
        memory_dir / "daily-routing-metrics.jsonl",
        '{"date":"2026-02-27","structured_daily_ok":false,"fallback_reason":"missing"}\n',
    )
    _write(memory_dir / "context-trace.jsonl", '{"stage":"before_send","estimated_tokens":100,"prefix_hash":"a1"}\n')

    text = render_memory_observability_dashboard(memory_dir)
    assert "# Memory Observability Dashboard" in text
    assert "## Quality Snapshot" in text
    assert "## Routing" in text
    assert "## Suggested Next Actions" in text


def test_apply_conservative_cleanup_scopes_recent_daily_files(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    _write(memory_dir / "HISTORY.md", "[2026-02-27 10:00] keep\n\n")
    today = datetime.now().date()
    old_day = today - timedelta(days=40)
    today_name = today.strftime("%Y-%m-%d")
    old_name = old_day.strftime("%Y-%m-%d")
    _write(memory_dir / f"{today_name}.md", f"# {today_name}\n\n## Topics\n\n- repeat\n- repeat\n")
    _write(memory_dir / f"{old_name}.md", f"# {old_name}\n\n## Topics\n\n- old\n- old\n")

    result = apply_conservative_cleanup(memory_dir, daily_recent_days=1, include_history=False)

    assert result.scoped_daily_files == 1
    assert result.skipped_daily_files == 1
    old_content = (memory_dir / f"{old_name}.md").read_text(encoding="utf-8")
    assert old_content.count("- old") == 2


def test_render_cleanup_effect_markdown_contains_delta(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    _write(memory_dir / "HISTORY.md", "[2026-02-27 10:00] " + ("x" * 620) + "\n\n")
    _write(memory_dir / "2026-02-27.md", "# 2026-02-27\n\n## Topics\n\n- repeat\n- repeat\n")
    before = run_memory_audit(memory_dir)
    result = apply_conservative_cleanup(memory_dir)
    after = run_memory_audit(memory_dir)

    text = render_cleanup_effect_markdown(before, after, result)
    assert "Audit Delta (Before -> After)" in text
    assert "HISTORY long(>600)" in text
    assert "DAILY duplicates" in text


def test_summarize_daily_archive_dry_run_respects_keep_window(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    _write(memory_dir / "MEMORY.md", "# Long-term Memory\n")
    _write(memory_dir / "HISTORY.md", "")
    _write(memory_dir / "2020-01-01.md", "# 2020-01-01\n\n## Topics\n\n- old\n- old2\n")
    today = datetime.now().strftime("%Y-%m-%d")
    _write(memory_dir / f"{today}.md", f"# {today}\n\n## Topics\n\n- recent\n")

    summary = summarize_daily_archive_dry_run(memory_dir, keep_days=30)
    assert summary.candidate_file_count == 1
    assert summary.candidate_bullet_count == 2
    assert summary.candidate_files == ["2020-01-01.md"]
    rendered = render_daily_archive_dry_run_markdown(summary)
    assert "Candidate files: `1`" in rendered
