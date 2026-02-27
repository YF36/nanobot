from pathlib import Path
from datetime import datetime, timedelta

from nanobot.agent.memory_maintenance import (
    apply_conservative_cleanup,
    build_cleanup_plan,
    render_cleanup_conversion_index_markdown,
    render_cleanup_drop_preview_markdown,
    render_cleanup_stage_metrics_markdown,
    render_context_trace_markdown,
    render_memory_observability_dashboard,
    render_daily_archive_dry_run_markdown,
    render_cleanup_effect_markdown,
    render_memory_conflict_metrics_markdown,
    render_daily_routing_metrics_markdown,
    render_memory_update_guard_metrics_markdown,
    render_memory_update_sanitize_metrics_markdown,
    run_memory_audit,
    summarize_context_trace,
    summarize_daily_archive_dry_run,
    summarize_cleanup_stage_metrics,
    summarize_cleanup_conversion_index,
    summarize_cleanup_drop_preview,
    summarize_memory_conflict_metrics,
    summarize_memory_update_guard_metrics,
    summarize_memory_update_sanitize_metrics,
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
    assert result.daily_dropped_tool_activity_bullets == 0
    assert result.daily_dropped_non_decision_bullets == 0
    assert result.conversion_index_rows == 4
    assert result.scoped_daily_files == 1
    assert result.skipped_daily_files == 0

    history_after = (memory_dir / "HISTORY.md").read_text(encoding="utf-8")
    assert history_after.count("repeat") == 1
    daily_after = (memory_dir / "2026-02-27.md").read_text(encoding="utf-8")
    assert daily_after.count("- repeat") == 1

    assert result.backup_dir.exists()
    assert (result.backup_dir / "HISTORY.md").exists()
    assert (result.backup_dir / "2026-02-27.md").exists()
    conversion_index_lines = (memory_dir / "cleanup-conversion-index.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(conversion_index_lines) == 4
    assert '"action":"trim"' in conversion_index_lines[0] or '"action":"dedupe"' in conversion_index_lines[0]
    stage_metrics_lines = (memory_dir / "cleanup-stage-metrics.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(stage_metrics_lines) == 1
    assert '"trim":2' in stage_metrics_lines[0]
    assert '"dedupe":2' in stage_metrics_lines[0]
    assert '"conversion_index_rows":4' in stage_metrics_lines[0]


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
    text = render_memory_update_guard_metrics_markdown(summary)
    assert "## Suggested Fixes" in text
    assert "incremental edits" in text


def test_render_memory_update_guard_metrics_markdown_handles_missing_file(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    summary = summarize_memory_update_guard_metrics(memory_dir)
    text = render_memory_update_guard_metrics_markdown(summary)
    assert "Metrics file: not found" in text


def test_summarize_memory_update_sanitize_metrics_counts(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    _write(
        memory_dir / "memory-update-sanitize-metrics.jsonl",
        "\n".join(
            [
                '{"session_key":"s1","removed_recent_topic_section_count":2,"removed_transient_status_line_count":1}',
                '{"session_key":"s1","removed_recent_topic_section_count":1,"removed_transient_status_line_count":0}',
                '{"session_key":"s2","removed_recent_topic_section_count":0,"removed_transient_status_line_count":3}',
                "not-json",
            ]
        )
        + "\n",
    )
    summary = summarize_memory_update_sanitize_metrics(memory_dir)
    assert summary.metrics_file_exists is True
    assert summary.total_rows == 4
    assert summary.parse_error_rows == 1
    assert summary.total_recent_topic_sections_removed == 3
    assert summary.total_transient_status_lines_removed == 4
    assert summary.by_session["s1"] == 2
    assert summary.by_session["s2"] == 1
    text = render_memory_update_sanitize_metrics_markdown(summary)
    assert "Memory Update Sanitize Metrics Summary" in text
    assert "removed_recent_topic_sections(total)" in text


def test_render_memory_update_sanitize_metrics_markdown_handles_missing_file(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    summary = summarize_memory_update_sanitize_metrics(memory_dir)
    text = render_memory_update_sanitize_metrics_markdown(summary)
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


def test_summarize_cleanup_stage_metrics_counts_distribution(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    _write(
        memory_dir / "cleanup-stage-metrics.jsonl",
        "\n".join(
            [
                '{"stage_counts":{"trim":2,"dedupe":1,"drop_tool_activity":0}}',
                '{"stage_counts":{"trim":0,"dedupe":0,"drop_tool_activity":3}}',
                "not-json",
            ]
        )
        + "\n",
    )

    summary = summarize_cleanup_stage_metrics(memory_dir)
    assert summary.metrics_file_exists is True
    assert summary.total_rows == 3
    assert summary.parse_error_rows == 1
    assert summary.total_stage_counts["trim"] == 2
    assert summary.total_stage_counts["dedupe"] == 1
    assert summary.total_stage_counts["drop_tool_activity"] == 3
    assert summary.runs_with_stage["trim"] == 1
    assert summary.runs_with_stage["drop_tool_activity"] == 1
    text = render_cleanup_stage_metrics_markdown(summary)
    assert "Stage Distribution" in text
    assert "drop_tool_activity" in text


def test_summarize_cleanup_conversion_index_counts_actions(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    _write(
        memory_dir / "cleanup-conversion-index.jsonl",
        "\n".join(
            [
                '{"run_id":"cleanup_1","action":"trim","source_file":"HISTORY.md"}',
                '{"run_id":"cleanup_1","action":"dedupe","source_file":"2026-02-27.md"}',
                '{"run_id":"cleanup_2","action":"dedupe","source_file":"2026-02-27.md"}',
                "not-json",
            ]
        )
        + "\n",
    )

    summary = summarize_cleanup_conversion_index(memory_dir)
    assert summary.index_file_exists is True
    assert summary.total_rows == 4
    assert summary.parse_error_rows == 1
    assert summary.action_counts["dedupe"] == 2
    assert summary.source_file_counts["2026-02-27.md"] == 2
    assert summary.latest_run_id == "cleanup_2"
    assert summary.latest_run_action_counts["dedupe"] == 1
    text = render_cleanup_conversion_index_markdown(summary)
    assert "Cleanup Conversion Index Summary" in text
    assert "## Actions" in text
    assert "## Latest Run" in text


def test_summarize_cleanup_drop_preview_counts_candidates(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    old_day = (datetime.now() - timedelta(days=40)).strftime("%Y-%m-%d")
    _write(
        memory_dir / f"{old_day}.md",
        (
            f"# {old_day}\n\n"
            "## Topics\n\n- old topic\n\n"
            "## Decisions\n\n- keep decision\n\n"
            "## Open Questions\n\n- old question\n\n"
            "## Tool Activity\n\n- old cmd\n"
        ),
    )
    summary = summarize_cleanup_drop_preview(
        memory_dir,
        drop_tool_activity_older_than_days=30,
        drop_non_decision_older_than_days=30,
    )
    assert summary.scoped_daily_files == 1
    assert summary.drop_tool_activity_candidates == 1
    assert summary.drop_non_decision_candidates == 2
    assert summary.risk_level == "low"
    assert summary.dominant_driver == "non_decision"
    assert summary.by_file[f"{old_day}.md"]["drop_non_decision"] == 2
    text = render_cleanup_drop_preview_markdown(summary)
    assert "Cleanup Drop Preview" in text
    assert "drop_non_decision_candidates" in text
    assert "Risk level" in text
    assert "Dominant driver" in text
    assert "Top candidate files" in text
    assert "Recommended Next Command" in text


def test_summarize_cleanup_drop_preview_reports_high_risk(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    old_day = (datetime.now() - timedelta(days=40)).strftime("%Y-%m-%d")
    bullets = "\n".join([f"- item {i}" for i in range(55)])
    _write(
        memory_dir / f"{old_day}.md",
        (
            f"# {old_day}\n\n"
            "## Topics\n\n"
            f"{bullets}\n"
        ),
    )
    summary = summarize_cleanup_drop_preview(
        memory_dir,
        drop_non_decision_older_than_days=30,
    )
    assert summary.drop_non_decision_candidates == 55
    assert summary.risk_level == "high"
    assert summary.dominant_driver == "non_decision"
    text = render_cleanup_drop_preview_markdown(summary)
    assert "--apply-drop-preview --apply-recent-days 7" in text


def test_render_cleanup_drop_preview_markdown_medium_risk_recommendation(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    old_day = (datetime.now() - timedelta(days=40)).strftime("%Y-%m-%d")
    bullets = "\n".join([f"- item {i}" for i in range(25)])
    _write(memory_dir / f"{old_day}.md", f"# {old_day}\n\n## Topics\n\n{bullets}\n")
    summary = summarize_cleanup_drop_preview(
        memory_dir,
        drop_non_decision_older_than_days=30,
    )
    assert summary.risk_level == "medium"
    text = render_cleanup_drop_preview_markdown(summary)
    assert "--apply --apply-recent-days 7" in text


def test_render_cleanup_drop_preview_top_candidate_files_sorted_by_impact(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    _write(
        memory_dir / "2020-01-01.md",
        "# 2020-01-01\n\n## Topics\n\n- one\n",
    )
    _write(
        memory_dir / "2020-01-02.md",
        "# 2020-01-02\n\n## Topics\n\n- a\n- b\n- c\n- d\n- e\n",
    )
    summary = summarize_cleanup_drop_preview(
        memory_dir,
        drop_non_decision_older_than_days=30,
    )
    text = render_cleanup_drop_preview_markdown(summary)
    assert "Top candidate files: `2020-01-02.md:5, 2020-01-01.md:1`" in text


def test_render_cleanup_drop_preview_respects_top_limit(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    _write(memory_dir / "2020-01-01.md", "# 2020-01-01\n\n## Topics\n\n- a\n")
    _write(memory_dir / "2020-01-02.md", "# 2020-01-02\n\n## Topics\n\n- a\n- b\n")
    _write(memory_dir / "2020-01-03.md", "# 2020-01-03\n\n## Topics\n\n- a\n- b\n- c\n")
    summary = summarize_cleanup_drop_preview(
        memory_dir,
        drop_non_decision_older_than_days=30,
    )
    text = render_cleanup_drop_preview_markdown(summary, top_limit=2)
    assert "Top candidate files: `2020-01-03.md:3, 2020-01-02.md:2`" in text


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
    assert "## Pruning Stage Distribution" in text
    assert "## Cleanup Conversion Traceability" in text
    assert "latest cleanup run" in text
    assert "## Half-Life Drop Preview (30d)" in text
    assert "memory_update sanitize events" in text
    assert "preview risk level" in text
    assert "preview dominant driver" in text
    assert "preview top candidate files" in text
    assert "Low-risk rollout" in text
    assert "## Suggested Next Actions" in text


def test_render_memory_observability_dashboard_warns_on_high_non_decision_drop(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    _write(memory_dir / "MEMORY.md", "# Long-term Memory\n")
    _write(memory_dir / "HISTORY.md", "")
    _write(memory_dir / "2020-01-01.md", "# 2020-01-01\n\n## Topics\n\n- old\n")
    _write(
        memory_dir / "cleanup-stage-metrics.jsonl",
        '{"stage_counts":{"trim":1,"dedupe":1,"drop_tool_activity":1,"drop_non_decision":10}}\n',
    )

    text = render_memory_observability_dashboard(memory_dir)
    assert "drop_non_decision" in text
    assert "ratio is high" in text


def test_render_memory_observability_dashboard_warns_on_missing_conversion_run_id(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    _write(memory_dir / "MEMORY.md", "# Long-term Memory\n")
    _write(memory_dir / "HISTORY.md", "")
    _write(memory_dir / "2020-01-01.md", "# 2020-01-01\n\n## Topics\n\n- old\n")
    _write(
        memory_dir / "cleanup-conversion-index.jsonl",
        '{"action":"dedupe","source_file":"2020-01-01.md"}\n',
    )

    text = render_memory_observability_dashboard(memory_dir)
    assert "Conversion index rows found without `run_id`" in text


def test_render_memory_observability_dashboard_recommends_sanitize_summary(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    _write(memory_dir / "MEMORY.md", "# Long-term Memory\n")
    _write(memory_dir / "HISTORY.md", "")
    _write(memory_dir / "2020-01-01.md", "# 2020-01-01\n\n## Topics\n\n- old\n")
    _write(
        memory_dir / "memory-update-sanitize-metrics.jsonl",
        '{"session_key":"s1","removed_recent_topic_section_count":1,"removed_transient_status_line_count":0}\n',
    )

    text = render_memory_observability_dashboard(memory_dir)
    assert "Review sanitize hits: `nanobot memory-audit --sanitize-metrics-summary`" in text


def test_render_memory_observability_dashboard_shows_high_risk_preview_command(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    _write(memory_dir / "MEMORY.md", "# Long-term Memory\n")
    _write(memory_dir / "HISTORY.md", "")
    old_day = (datetime.now() - timedelta(days=40)).strftime("%Y-%m-%d")
    bullets = "\n".join([f"- old {i}" for i in range(55)])
    _write(memory_dir / f"{old_day}.md", f"# {old_day}\n\n## Topics\n\n{bullets}\n")

    text = render_memory_observability_dashboard(memory_dir)
    assert "preview risk level: `high`" in text
    assert "High-risk preview" in text


def test_render_memory_observability_dashboard_shows_no_candidates_hint(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    _write(memory_dir / "MEMORY.md", "# Long-term Memory\n")
    _write(memory_dir / "HISTORY.md", "")
    today = datetime.now().strftime("%Y-%m-%d")
    _write(memory_dir / f"{today}.md", f"# {today}\n\n## Decisions\n\n- keep decision\n")

    text = render_memory_observability_dashboard(memory_dir)
    assert "No half-life cleanup candidates in 30d preview window." in text


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
    assert result.daily_dropped_tool_activity_bullets == 0
    assert result.daily_dropped_non_decision_bullets == 0
    assert result.conversion_index_rows == 1
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


def test_apply_conservative_cleanup_drops_old_tool_activity_only(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    old_day = (datetime.now() - timedelta(days=40)).strftime("%Y-%m-%d")
    _write(
        memory_dir / f"{old_day}.md",
        (
            f"# {old_day}\n\n"
            "## Topics\n\n- keep topic\n\n"
            "## Tool Activity\n\n- old cmd 1\n- old cmd 2\n\n"
            "## Open Questions\n\n- keep question\n"
        ),
    )

    result = apply_conservative_cleanup(
        memory_dir,
        daily_recent_days=None,
        include_history=False,
        drop_tool_activity_older_than_days=30,
    )
    assert result.daily_dropped_tool_activity_bullets == 2
    assert result.daily_dropped_non_decision_bullets == 0
    assert result.conversion_index_rows == 2
    text = (memory_dir / f"{old_day}.md").read_text(encoding="utf-8")
    assert "keep topic" in text
    assert "keep question" in text
    assert "old cmd 1" not in text
    assert "old cmd 2" not in text


def test_apply_conservative_cleanup_drops_old_non_decision_keeps_decisions(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    old_day = (datetime.now() - timedelta(days=40)).strftime("%Y-%m-%d")
    _write(
        memory_dir / f"{old_day}.md",
        (
            f"# {old_day}\n\n"
            "## Topics\n\n- old topic\n\n"
            "## Decisions\n\n- keep decision\n\n"
            "## Open Questions\n\n- old question\n"
        ),
    )

    result = apply_conservative_cleanup(
        memory_dir,
        daily_recent_days=None,
        include_history=False,
        drop_non_decision_older_than_days=30,
    )
    assert result.daily_dropped_tool_activity_bullets == 0
    assert result.daily_dropped_non_decision_bullets == 2
    assert result.conversion_index_rows == 2
    text = (memory_dir / f"{old_day}.md").read_text(encoding="utf-8")
    assert "old topic" not in text
    assert "old question" not in text
    assert "keep decision" in text


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
