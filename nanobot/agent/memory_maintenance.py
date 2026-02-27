"""Memory audit and conservative cleanup utilities."""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path


_DATE_FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.md$")
_HISTORY_ENTRY_RE = re.compile(r"^\[(20\d{2}-\d{2}-\d{2})(?:\s+\d{2}:\d{2})?\]\s*(.*)$")
_FALLBACK_REASON_HINTS = {
    "missing": "Model did not provide `daily_sections`; consider prompt nudge to always emit structured arrays.",
    "empty": "Structured payload was empty; ask model to include at least one concise bullet in a relevant section.",
    "not_object": "`daily_sections` was not an object; enforce tool-call schema and retry with stricter instruction.",
    "invalid_item:topics": "`topics` contains non-string items; require string arrays only.",
    "invalid_item:decisions": "`decisions` contains non-string items; require string arrays only.",
    "invalid_item:tool_activity": "`tool_activity` contains non-string items; require string arrays only.",
    "invalid_item:open_questions": "`open_questions` contains non-string items; require string arrays only.",
    "invalid_type:topics": "`topics` should be `string[]`; fix serializer to always emit arrays.",
    "invalid_type:decisions": "`decisions` should be `string[]`; fix serializer to always emit arrays.",
    "invalid_type:tool_activity": "`tool_activity` should be `string[]`; fix serializer to always emit arrays.",
    "invalid_type:open_questions": "`open_questions` should be `string[]`; fix serializer to always emit arrays.",
}
_GUARD_REASON_HINTS = {
    "contains_code_block": "Candidate contains code blocks/raw output; keep executable snippets and logs in history/daily, not long-term memory.",
    "url_line_overflow": "Candidate contains too many URL lines; keep source links in history/daily and retain only durable conclusions in long-term memory.",
    "duplicate_line_overflow": "Candidate repeats the same content too many times; deduplicate repetitive bullets and keep one durable statement.",
    "candidate_too_long": "Candidate memory_update is too large; reduce scope and keep only durable, compact facts.",
    "excessive_shrink": "Candidate update shrank too much vs current memory; keep prior headings/facts and apply incremental edits.",
    "heading_retention_too_low": "Too many existing H2 headings were dropped; preserve durable sections and only patch changed parts.",
    "unstructured_candidate": "Candidate is long but lacks markdown structure; require `##` sections and concise `-` bullets.",
    "date_line_overflow": "Candidate contains too many dated lines; move dated timeline details to daily/history instead of long-term memory.",
}
_STRUCTURED_DAILY_OK_RATE_WARN_THRESHOLD = 60.0


@dataclass
class MemoryAudit:
    memory_dir: Path
    memory_file_exists: bool
    history_file_exists: bool
    daily_files: list[str]
    memory_code_fence_count: int
    memory_timestamp_line_count: int
    memory_url_line_count: int
    history_entry_count: int
    history_long_entry_count: int
    history_code_fence_count: int
    history_duplicate_count: int
    daily_long_bullet_count: int
    daily_duplicate_count: int
    daily_timestamp_bullet_count: int
    daily_orphan_files: list[str]


@dataclass
class CleanupApplyResult:
    memory_dir: Path
    backup_dir: Path
    history_trimmed_entries: int
    history_deduplicated_entries: int
    daily_trimmed_bullets: int
    daily_deduplicated_bullets: int
    daily_dropped_tool_activity_bullets: int
    daily_dropped_non_decision_bullets: int
    conversion_index_rows: int
    scoped_daily_files: int
    skipped_daily_files: int
    touched_files: list[str]


@dataclass
class DailyRoutingMetricsSummary:
    metrics_file_exists: bool
    total_rows: int
    parse_error_rows: int
    structured_ok_count: int
    fallback_count: int
    sessions_with_routing_events: int
    sessions_with_fallback_events: int
    by_session: dict[str, int]
    structured_source_counts: dict[str, int]
    fallback_reason_counts: dict[str, int]
    fallback_sessions_by_reason: dict[str, int]
    by_date: dict[str, dict[str, int]]


@dataclass
class MemoryUpdateGuardMetricsSummary:
    metrics_file_exists: bool
    total_rows: int
    parse_error_rows: int
    reason_counts: dict[str, int]
    sessions_with_hits_by_reason: dict[str, int]
    by_session: dict[str, int]
    preview_by_reason: dict[str, str]
    avg_current_memory_chars: int
    avg_returned_memory_chars: int
    sessions_with_guard_hits: int


@dataclass
class MemoryUpdateSanitizeMetricsSummary:
    metrics_file_exists: bool
    total_rows: int
    parse_error_rows: int
    total_recent_topic_sections_removed: int
    total_transient_status_lines_removed: int
    total_duplicate_bullets_removed: int
    dominant_focus: str
    sessions_with_sanitize_hits: int
    sessions_with_effective_sanitize_hits: int
    by_session: dict[str, int]
    top_recent_topic_sections: dict[str, int]
    top_transient_status_sections: dict[str, int]
    top_duplicate_bullet_sections: dict[str, int]


@dataclass
class DailyArchiveDryRunSummary:
    keep_days: int
    candidate_file_count: int
    candidate_bullet_count: int
    candidate_files: list[str]


@dataclass
class MemoryConflictMetricsSummary:
    metrics_file_exists: bool
    total_rows: int
    parse_error_rows: int
    key_counts: dict[str, int]
    by_session: dict[str, int]


@dataclass
class ContextTraceSummary:
    trace_file_exists: bool
    total_rows: int
    parse_error_rows: int
    by_stage: dict[str, int]
    avg_tokens_by_stage: dict[str, int]
    prefix_stability_ratio: float


@dataclass
class CleanupStageMetricsSummary:
    metrics_file_exists: bool
    total_rows: int
    parse_error_rows: int
    total_stage_counts: dict[str, int]
    runs_with_stage: dict[str, int]


@dataclass
class CleanupConversionIndexSummary:
    index_file_exists: bool
    total_rows: int
    parse_error_rows: int
    action_counts: dict[str, int]
    source_file_counts: dict[str, int]
    latest_run_id: str
    latest_run_action_counts: dict[str, int]


@dataclass
class CleanupDropPreviewSummary:
    scoped_daily_files: int
    skipped_daily_files: int
    drop_tool_activity_candidates: int
    drop_non_decision_candidates: int
    risk_level: str
    dominant_driver: str
    by_file: dict[str, dict[str, int]]


def _iter_daily_files(memory_dir: Path) -> list[Path]:
    items: list[Path] = []
    for p in sorted(memory_dir.glob("*.md")):
        if p.name in {"MEMORY.md", "HISTORY.md"}:
            continue
        if _DATE_FILE_RE.match(p.name):
            items.append(p)
    return items


def _daily_file_date(path: Path) -> date | None:
    m = _DATE_FILE_RE.match(path.name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d").date()
    except ValueError:
        return None


def _parse_history_entries(history_text: str) -> list[str]:
    entries: list[str] = []
    cur: list[str] = []
    for line in history_text.splitlines():
        if line.strip():
            cur.append(line.rstrip())
            continue
        if cur:
            entries.append(" ".join(cur).strip())
            cur = []
    if cur:
        entries.append(" ".join(cur).strip())
    return entries


def _backup_file(src: Path, backup_dir: Path) -> None:
    backup_dir.mkdir(parents=True, exist_ok=True)
    (backup_dir / src.name).write_text(src.read_text(encoding="utf-8"), encoding="utf-8")


def _append_jsonl(path: Path, payload: dict[str, object]) -> None:
    line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    with path.open("a", encoding="utf-8") as fp:
        fp.write(line)


def _observability_file(memory_dir: Path, filename: str) -> Path:
    return memory_dir / "observability" / filename


def _metrics_file_for_write(memory_dir: Path, filename: str) -> Path:
    target = _observability_file(memory_dir, filename)
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def _metrics_file_for_read(memory_dir: Path, filename: str) -> Path:
    return _observability_file(memory_dir, filename)


def _extract_history_dates(entries: list[str]) -> set[str]:
    dates: set[str] = set()
    for entry in entries:
        m = _HISTORY_ENTRY_RE.match(entry)
        if m:
            dates.add(m.group(1))
    return dates


def run_memory_audit(memory_dir: Path) -> MemoryAudit:
    memory_file = memory_dir / "MEMORY.md"
    history_file = memory_dir / "HISTORY.md"
    daily_files = _iter_daily_files(memory_dir)

    memory_text = memory_file.read_text(encoding="utf-8") if memory_file.exists() else ""
    history_text = history_file.read_text(encoding="utf-8") if history_file.exists() else ""
    history_entries = _parse_history_entries(history_text)

    history_counter = Counter(entry.strip() for entry in history_entries if entry.strip())
    history_duplicate_count = sum(v - 1 for v in history_counter.values() if v > 1)
    history_dates = _extract_history_dates(history_entries)

    daily_long_bullet_count = 0
    daily_duplicate_count = 0
    daily_timestamp_bullet_count = 0
    daily_orphan_files: list[str] = []

    for daily in daily_files:
        text = daily.read_text(encoding="utf-8")
        bullets = [line[2:].strip() for line in text.splitlines() if line.startswith("- ")]
        bullet_counter = Counter(b for b in bullets if b)
        daily_duplicate_count += sum(v - 1 for v in bullet_counter.values() if v > 1)
        daily_long_bullet_count += sum(1 for b in bullets if len(b) > 240)
        daily_timestamp_bullet_count += sum(1 for b in bullets if b.startswith("[20") and "]" in b)

        file_date = daily.stem
        if file_date not in history_dates:
            daily_orphan_files.append(daily.name)

    return MemoryAudit(
        memory_dir=memory_dir,
        memory_file_exists=memory_file.exists(),
        history_file_exists=history_file.exists(),
        daily_files=[p.name for p in daily_files],
        memory_code_fence_count=memory_text.count("```"),
        memory_timestamp_line_count=sum(1 for line in memory_text.splitlines() if line.strip().startswith("[20")),
        memory_url_line_count=sum(1 for line in memory_text.splitlines() if "http://" in line or "https://" in line),
        history_entry_count=len(history_entries),
        history_long_entry_count=sum(1 for e in history_entries if len(e) > 600),
        history_code_fence_count=sum(1 for e in history_entries if "```" in e),
        history_duplicate_count=history_duplicate_count,
        daily_long_bullet_count=daily_long_bullet_count,
        daily_duplicate_count=daily_duplicate_count,
        daily_timestamp_bullet_count=daily_timestamp_bullet_count,
        daily_orphan_files=sorted(daily_orphan_files),
    )


def _trim_text(value: str, max_chars: int) -> tuple[str, bool]:
    if len(value) <= max_chars:
        return value, False
    return value[: max_chars - 3].rstrip() + "...", True


def apply_conservative_cleanup(
    memory_dir: Path,
    *,
    daily_recent_days: int | None = None,
    include_history: bool = True,
    drop_tool_activity_older_than_days: int | None = None,
    drop_non_decision_older_than_days: int | None = None,
) -> CleanupApplyResult:
    history_file = memory_dir / "HISTORY.md"
    all_daily_files = _iter_daily_files(memory_dir)
    if daily_recent_days is None:
        daily_files = all_daily_files
    else:
        window_days = max(1, int(daily_recent_days))
        cutoff = datetime.now().date() - timedelta(days=window_days - 1)
        daily_files = []
        for p in all_daily_files:
            d = _daily_file_date(p)
            if d is not None and d >= cutoff:
                daily_files.append(p)
    backup_dir = memory_dir / f"_cleanup_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    history_trimmed_entries = 0
    history_deduplicated_entries = 0
    daily_trimmed_bullets = 0
    daily_deduplicated_bullets = 0
    daily_dropped_tool_activity_bullets = 0
    daily_dropped_non_decision_bullets = 0
    conversion_rows: list[dict[str, object]] = []
    touched_files: list[str] = []
    drop_tool_cutoff = None
    if drop_tool_activity_older_than_days is not None:
        window_days = max(1, int(drop_tool_activity_older_than_days))
        drop_tool_cutoff = datetime.now().date() - timedelta(days=window_days - 1)
    drop_non_decision_cutoff = None
    if drop_non_decision_older_than_days is not None:
        window_days = max(1, int(drop_non_decision_older_than_days))
        drop_non_decision_cutoff = datetime.now().date() - timedelta(days=window_days - 1)

    if include_history and history_file.exists():
        original = history_file.read_text(encoding="utf-8")
        entries = _parse_history_entries(original)
        seen: set[str] = set()
        cleaned: list[str] = []
        for e in entries:
            raw_entry = e
            trimmed, changed = _trim_text(" ".join(e.split()).strip(), 600)
            if changed:
                history_trimmed_entries += 1
                conversion_rows.append(
                    {
                        "scope": "history",
                        "source_file": history_file.name,
                        "action": "trim",
                        "before": raw_entry,
                        "after": trimmed,
                    }
                )
            if not trimmed:
                conversion_rows.append(
                    {
                        "scope": "history",
                        "source_file": history_file.name,
                        "action": "drop_empty",
                        "before": raw_entry,
                    }
                )
                continue
            if trimmed in seen:
                history_deduplicated_entries += 1
                conversion_rows.append(
                    {
                        "scope": "history",
                        "source_file": history_file.name,
                        "action": "dedupe",
                        "before": raw_entry,
                        "normalized": trimmed,
                    }
                )
                continue
            seen.add(trimmed)
            cleaned.append(trimmed)
        new_content = ("\n\n".join(cleaned).rstrip() + "\n") if cleaned else ""
        if new_content != original:
            _backup_file(history_file, backup_dir)
            history_file.write_text(new_content, encoding="utf-8")
            touched_files.append(history_file.name)

    for daily in daily_files:
        original = daily.read_text(encoding="utf-8")
        lines = original.splitlines()
        seen_bullets: set[str] = set()
        changed = False
        new_lines: list[str] = []
        daily_date = _daily_file_date(daily)
        current_section = ""
        for line in lines:
            if line.startswith("## "):
                current_section = line[3:].strip()
                new_lines.append(line)
                continue
            if not line.startswith("- "):
                new_lines.append(line)
                continue
            if (
                drop_tool_cutoff is not None
                and daily_date is not None
                and daily_date < drop_tool_cutoff
                and current_section == "Tool Activity"
            ):
                daily_dropped_tool_activity_bullets += 1
                conversion_rows.append(
                    {
                        "scope": "daily",
                        "source_file": daily.name,
                        "section": current_section or "unknown",
                        "action": "drop_tool_activity",
                        "before": line[2:].strip(),
                    }
                )
                changed = True
                continue
            if (
                drop_non_decision_cutoff is not None
                and daily_date is not None
                and daily_date < drop_non_decision_cutoff
                and current_section in {"Topics", "Open Questions"}
            ):
                daily_dropped_non_decision_bullets += 1
                conversion_rows.append(
                    {
                        "scope": "daily",
                        "source_file": daily.name,
                        "section": current_section or "unknown",
                        "action": "drop_non_decision",
                        "before": line[2:].strip(),
                    }
                )
                changed = True
                continue
            bullet = line[2:].strip()
            trimmed, was_trimmed = _trim_text(" ".join(bullet.split()).strip(), 240)
            if was_trimmed:
                daily_trimmed_bullets += 1
                conversion_rows.append(
                    {
                        "scope": "daily",
                        "source_file": daily.name,
                        "section": current_section or "unknown",
                        "action": "trim",
                        "before": bullet,
                        "after": trimmed,
                    }
                )
                changed = True
            if not trimmed:
                conversion_rows.append(
                    {
                        "scope": "daily",
                        "source_file": daily.name,
                        "section": current_section or "unknown",
                        "action": "drop_empty",
                        "before": bullet,
                    }
                )
                changed = True
                continue
            if trimmed in seen_bullets:
                daily_deduplicated_bullets += 1
                conversion_rows.append(
                    {
                        "scope": "daily",
                        "source_file": daily.name,
                        "section": current_section or "unknown",
                        "action": "dedupe",
                        "before": bullet,
                        "normalized": trimmed,
                    }
                )
                changed = True
                continue
            seen_bullets.add(trimmed)
            if trimmed != bullet:
                changed = True
            new_lines.append(f"- {trimmed}")
        new_content = "\n".join(new_lines)
        if original.endswith("\n"):
            new_content += "\n"
        if changed and new_content != original:
            _backup_file(daily, backup_dir)
            daily.write_text(new_content, encoding="utf-8")
            touched_files.append(daily.name)

    if not touched_files and backup_dir.exists():
        backup_dir.rmdir()

    conversion_index_rows = _write_cleanup_conversion_index(memory_dir, conversion_rows)

    result = CleanupApplyResult(
        memory_dir=memory_dir,
        backup_dir=backup_dir,
        history_trimmed_entries=history_trimmed_entries,
        history_deduplicated_entries=history_deduplicated_entries,
        daily_trimmed_bullets=daily_trimmed_bullets,
        daily_deduplicated_bullets=daily_deduplicated_bullets,
        daily_dropped_tool_activity_bullets=daily_dropped_tool_activity_bullets,
        daily_dropped_non_decision_bullets=daily_dropped_non_decision_bullets,
        conversion_index_rows=conversion_index_rows,
        scoped_daily_files=len(daily_files),
        skipped_daily_files=max(0, len(all_daily_files) - len(daily_files)),
        touched_files=sorted(touched_files),
    )
    _write_cleanup_stage_metrics(memory_dir, result)
    return result


def _write_cleanup_stage_metrics(memory_dir: Path, result: CleanupApplyResult) -> None:
    metrics_file = _metrics_file_for_write(memory_dir, "cleanup-stage-metrics.jsonl")
    stage_counts = {
        "trim": int(result.history_trimmed_entries + result.daily_trimmed_bullets),
        "dedupe": int(result.history_deduplicated_entries + result.daily_deduplicated_bullets),
        "drop_tool_activity": int(result.daily_dropped_tool_activity_bullets),
        "drop_non_decision": int(result.daily_dropped_non_decision_bullets),
    }
    _append_jsonl(
        metrics_file,
        {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "changed": bool(result.touched_files),
            "scoped_daily_files": int(result.scoped_daily_files),
            "skipped_daily_files": int(result.skipped_daily_files),
            "stage_counts": stage_counts,
            "conversion_index_rows": int(result.conversion_index_rows),
            "files_touched_count": int(len(result.touched_files)),
        },
    )


def _write_cleanup_conversion_index(memory_dir: Path, rows: list[dict[str, object]]) -> int:
    if not rows:
        return 0
    index_file = _metrics_file_for_write(memory_dir, "cleanup-conversion-index.jsonl")
    run_id = f"cleanup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    ts = datetime.now().isoformat(timespec="seconds")
    for row in rows:
        payload = {"run_id": run_id, "timestamp": ts}
        payload.update(row)
        _append_jsonl(index_file, payload)
    return len(rows)


def summarize_cleanup_drop_preview(
    memory_dir: Path,
    *,
    daily_recent_days: int | None = None,
    drop_tool_activity_older_than_days: int | None = None,
    drop_non_decision_older_than_days: int | None = None,
) -> CleanupDropPreviewSummary:
    all_daily_files = _iter_daily_files(memory_dir)
    if daily_recent_days is None:
        daily_files = all_daily_files
    else:
        window_days = max(1, int(daily_recent_days))
        cutoff = datetime.now().date() - timedelta(days=window_days - 1)
        daily_files = []
        for p in all_daily_files:
            d = _daily_file_date(p)
            if d is not None and d >= cutoff:
                daily_files.append(p)

    drop_tool_cutoff = None
    if drop_tool_activity_older_than_days is not None:
        window_days = max(1, int(drop_tool_activity_older_than_days))
        drop_tool_cutoff = datetime.now().date() - timedelta(days=window_days - 1)

    drop_non_decision_cutoff = None
    if drop_non_decision_older_than_days is not None:
        window_days = max(1, int(drop_non_decision_older_than_days))
        drop_non_decision_cutoff = datetime.now().date() - timedelta(days=window_days - 1)

    tool_candidates = 0
    non_decision_candidates = 0
    by_file: dict[str, dict[str, int]] = {}

    for daily in daily_files:
        daily_date = _daily_file_date(daily)
        if daily_date is None:
            continue
        current_section = ""
        file_tool = 0
        file_non_decision = 0
        for line in daily.read_text(encoding="utf-8").splitlines():
            if line.startswith("## "):
                current_section = line[3:].strip()
                continue
            if not line.startswith("- "):
                continue
            if (
                drop_tool_cutoff is not None
                and daily_date < drop_tool_cutoff
                and current_section == "Tool Activity"
            ):
                tool_candidates += 1
                file_tool += 1
                continue
            if (
                drop_non_decision_cutoff is not None
                and daily_date < drop_non_decision_cutoff
                and current_section in {"Topics", "Open Questions"}
            ):
                non_decision_candidates += 1
                file_non_decision += 1
        if file_tool > 0 or file_non_decision > 0:
            by_file[daily.name] = {
                "drop_tool_activity": file_tool,
                "drop_non_decision": file_non_decision,
            }

    total_candidates = tool_candidates + non_decision_candidates
    if non_decision_candidates >= 50 or total_candidates >= 80:
        risk_level = "high"
    elif non_decision_candidates >= 20 or total_candidates >= 30:
        risk_level = "medium"
    else:
        risk_level = "low"

    if tool_candidates > non_decision_candidates:
        dominant_driver = "tool_activity"
    elif non_decision_candidates > tool_candidates:
        dominant_driver = "non_decision"
    elif tool_candidates == 0 and non_decision_candidates == 0:
        dominant_driver = "none"
    else:
        dominant_driver = "mixed"

    return CleanupDropPreviewSummary(
        scoped_daily_files=len(daily_files),
        skipped_daily_files=max(0, len(all_daily_files) - len(daily_files)),
        drop_tool_activity_candidates=tool_candidates,
        drop_non_decision_candidates=non_decision_candidates,
        risk_level=risk_level,
        dominant_driver=dominant_driver,
        by_file=dict(sorted(by_file.items())),
    )


def _top_candidate_file_pairs(by_file: dict[str, dict[str, int]], limit: int = 3) -> list[str]:
    items: list[tuple[str, int]] = []
    for name, counts in by_file.items():
        total = int(counts.get("drop_tool_activity", 0)) + int(counts.get("drop_non_decision", 0))
        if total > 0:
            items.append((name, total))
    items.sort(key=lambda x: (-x[1], x[0]))
    return [f"{name}:{total}" for name, total in items[:limit]]


def render_cleanup_drop_preview_markdown(summary: CleanupDropPreviewSummary, *, top_limit: int = 3) -> str:
    top_files = _top_candidate_file_pairs(summary.by_file, limit=max(1, int(top_limit)))
    lines = [
        "# Cleanup Drop Preview",
        "",
        f"- Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"- Scoped daily files: `{summary.scoped_daily_files}` (skipped=`{summary.skipped_daily_files}`)",
        f"- Risk level: `{summary.risk_level}`",
        f"- Dominant driver: `{summary.dominant_driver}`",
        f"- Top candidate files: `{', '.join(top_files) if top_files else 'none'}`",
        "",
        "## Candidate Counts",
        f"- drop_tool_activity_candidates: `{summary.drop_tool_activity_candidates}`",
        f"- drop_non_decision_candidates: `{summary.drop_non_decision_candidates}`",
        "",
        "## Candidate Files (Top 20)",
    ]
    if not summary.by_file:
        lines.append("- none")
    else:
        for name, counts in list(summary.by_file.items())[:20]:
            lines.append(
                f"- {name}: tool_activity=`{counts.get('drop_tool_activity', 0)}`, non_decision=`{counts.get('drop_non_decision', 0)}`"
            )
    lines.extend(["", "## Recommended Next Command"])
    if summary.risk_level == "high":
        lines.append(
            "- `nanobot memory-audit --apply-drop-preview --apply-recent-days 7 --drop-tool-activity-older-than-days 30 --drop-non-decision-older-than-days 30`"
        )
    elif summary.risk_level == "medium":
        lines.append(
            "- `nanobot memory-audit --apply --apply-recent-days 7 --drop-tool-activity-older-than-days 30 --drop-non-decision-older-than-days 30 --apply-abort-on-high-risk`"
        )
    else:
        lines.append(
            "- `nanobot memory-audit --apply --drop-tool-activity-older-than-days 30 --drop-non-decision-older-than-days 30 --apply-abort-on-high-risk`"
        )
    lines.append("")
    return "\n".join(lines)


def render_cleanup_effect_markdown(before: MemoryAudit, after: MemoryAudit, result: CleanupApplyResult) -> str:
    lines = [
        "# Memory Cleanup Effect",
        "",
        f"- Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"- Memory dir: `{result.memory_dir}`",
        f"- Scoped daily files: `{result.scoped_daily_files}` (skipped=`{result.skipped_daily_files}`)",
        f"- Touched files: `{len(result.touched_files)}` ({', '.join(result.touched_files) if result.touched_files else 'none'})",
        "",
        "## Operation Counters",
        f"- history_trimmed_entries: `{result.history_trimmed_entries}`",
        f"- history_deduplicated_entries: `{result.history_deduplicated_entries}`",
        f"- daily_trimmed_bullets: `{result.daily_trimmed_bullets}`",
        f"- daily_deduplicated_bullets: `{result.daily_deduplicated_bullets}`",
        f"- daily_dropped_tool_activity_bullets: `{result.daily_dropped_tool_activity_bullets}`",
        f"- daily_dropped_non_decision_bullets: `{result.daily_dropped_non_decision_bullets}`",
        f"- conversion_index_rows: `{result.conversion_index_rows}`",
        "",
        "## Audit Delta (Before -> After)",
        f"- HISTORY long(>600): `{before.history_long_entry_count}` -> `{after.history_long_entry_count}`",
        f"- HISTORY duplicates: `{before.history_duplicate_count}` -> `{after.history_duplicate_count}`",
        f"- DAILY long bullets(>240): `{before.daily_long_bullet_count}` -> `{after.daily_long_bullet_count}`",
        f"- DAILY duplicates: `{before.daily_duplicate_count}` -> `{after.daily_duplicate_count}`",
        "",
    ]
    return "\n".join(lines)


def summarize_daily_routing_metrics(memory_dir: Path) -> DailyRoutingMetricsSummary:
    metrics_file = _metrics_file_for_read(memory_dir, "daily-routing-metrics.jsonl")
    if not metrics_file.exists():
        return DailyRoutingMetricsSummary(
            metrics_file_exists=False,
            total_rows=0,
            parse_error_rows=0,
            structured_ok_count=0,
            fallback_count=0,
            sessions_with_routing_events=0,
            sessions_with_fallback_events=0,
            by_session={},
            structured_source_counts={},
            fallback_reason_counts={},
            fallback_sessions_by_reason={},
            by_date={},
        )

    total_rows = 0
    parse_error_rows = 0
    structured_ok_count = 0
    fallback_count = 0
    session_counter: Counter[str] = Counter()
    fallback_reason_counter: Counter[str] = Counter()
    fallback_session_counter: Counter[str] = Counter()
    fallback_sessions_by_reason: dict[str, set[str]] = {}
    structured_source_counter: Counter[str] = Counter()
    by_date: dict[str, dict[str, int]] = {}

    for raw_line in metrics_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        total_rows += 1
        try:
            item = json.loads(line)
        except Exception:
            parse_error_rows += 1
            continue
        if not isinstance(item, dict):
            parse_error_rows += 1
            continue
        session_key = str(item.get("session_key") or "unknown")
        session_counter[session_key] += 1
        structured_source = str(item.get("structured_source") or "")
        if structured_source:
            structured_source_counter[structured_source] += 1

        date = str(item.get("date") or "")
        if not date:
            date = "unknown"
        bucket = by_date.setdefault(date, {"total": 0, "structured_ok": 0, "fallback": 0})
        bucket["total"] += 1

        structured_ok = bool(item.get("structured_daily_ok", False))
        if structured_ok:
            structured_ok_count += 1
            bucket["structured_ok"] += 1
        else:
            fallback_count += 1
            bucket["fallback"] += 1
            reason = str(item.get("fallback_reason") or "unknown")
            fallback_reason_counter[reason] += 1
            fallback_session_counter[session_key] += 1
            fallback_sessions_by_reason.setdefault(reason, set()).add(session_key)

    return DailyRoutingMetricsSummary(
        metrics_file_exists=True,
        total_rows=total_rows,
        parse_error_rows=parse_error_rows,
        structured_ok_count=structured_ok_count,
        fallback_count=fallback_count,
        sessions_with_routing_events=len(session_counter),
        sessions_with_fallback_events=len(fallback_session_counter),
        by_session=dict(sorted(session_counter.items(), key=lambda kv: (-kv[1], kv[0]))),
        structured_source_counts=dict(sorted(structured_source_counter.items(), key=lambda kv: (-kv[1], kv[0]))),
        fallback_reason_counts=dict(sorted(fallback_reason_counter.items(), key=lambda kv: (-kv[1], kv[0]))),
        fallback_sessions_by_reason=dict(
            sorted(
                ((reason, len(sessions)) for reason, sessions in fallback_sessions_by_reason.items()),
                key=lambda kv: (-kv[1], kv[0]),
            )
        ),
        by_date=dict(sorted(by_date.items())),
    )


def summarize_cleanup_stage_metrics(memory_dir: Path) -> CleanupStageMetricsSummary:
    metrics_file = _metrics_file_for_read(memory_dir, "cleanup-stage-metrics.jsonl")
    if not metrics_file.exists():
        return CleanupStageMetricsSummary(
            metrics_file_exists=False,
            total_rows=0,
            parse_error_rows=0,
            total_stage_counts={},
            runs_with_stage={},
        )

    total_rows = 0
    parse_error_rows = 0
    total_stage_counter: Counter[str] = Counter()
    runs_with_stage_counter: Counter[str] = Counter()

    for raw_line in metrics_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        total_rows += 1
        try:
            item = json.loads(line)
        except Exception:
            parse_error_rows += 1
            continue
        if not isinstance(item, dict):
            parse_error_rows += 1
            continue

        stage_counts = item.get("stage_counts")
        if not isinstance(stage_counts, dict):
            parse_error_rows += 1
            continue

        for stage in ("trim", "dedupe", "drop_tool_activity", "drop_non_decision"):
            raw = stage_counts.get(stage, 0)
            if not isinstance(raw, int):
                parse_error_rows += 1
                continue
            value = max(0, int(raw))
            total_stage_counter[stage] += value
            if value > 0:
                runs_with_stage_counter[stage] += 1

    return CleanupStageMetricsSummary(
        metrics_file_exists=True,
        total_rows=total_rows,
        parse_error_rows=parse_error_rows,
        total_stage_counts=dict(sorted(total_stage_counter.items())),
        runs_with_stage=dict(sorted(runs_with_stage_counter.items())),
    )


def render_cleanup_stage_metrics_markdown(summary: CleanupStageMetricsSummary) -> str:
    lines = [
        "# Cleanup Stage Metrics Summary",
        "",
        f"- Generated at: {datetime.now().isoformat(timespec='seconds')}",
    ]
    if not summary.metrics_file_exists:
        lines.extend(["- Metrics file: not found (`cleanup-stage-metrics.jsonl`)", ""])
        return "\n".join(lines)

    valid = max(0, summary.total_rows - summary.parse_error_rows)
    lines.extend(
        [
            "- Metrics file: found (`cleanup-stage-metrics.jsonl`)",
            "",
            "## Overall",
            f"- Rows: `{summary.total_rows}` (valid=`{valid}`, parse_errors=`{summary.parse_error_rows}`)",
            "",
            "## Stage Distribution (Total Events)",
        ]
    )
    if not summary.total_stage_counts:
        lines.append("- none")
    else:
        total_events = sum(summary.total_stage_counts.values())
        for stage, count in summary.total_stage_counts.items():
            ratio = (count / total_events * 100.0) if total_events > 0 else 0.0
            lines.append(f"- {stage}: `{count}` ({ratio:.1f}%)")
    lines.extend(["", "## Stage Activation (Runs With Stage>0)"])
    if not summary.runs_with_stage:
        lines.append("- none")
    else:
        for stage, runs in summary.runs_with_stage.items():
            ratio = (runs / valid * 100.0) if valid > 0 else 0.0
            lines.append(f"- {stage}: `{runs}` runs ({ratio:.1f}% of valid runs)")
    lines.append("")
    return "\n".join(lines)


def summarize_cleanup_conversion_index(memory_dir: Path) -> CleanupConversionIndexSummary:
    index_file = _metrics_file_for_read(memory_dir, "cleanup-conversion-index.jsonl")
    if not index_file.exists():
        return CleanupConversionIndexSummary(
            index_file_exists=False,
            total_rows=0,
            parse_error_rows=0,
            action_counts={},
            source_file_counts={},
            latest_run_id="",
            latest_run_action_counts={},
        )

    total_rows = 0
    parse_error_rows = 0
    action_counter: Counter[str] = Counter()
    source_counter: Counter[str] = Counter()
    latest_run_id = ""
    latest_run_action_counter: Counter[str] = Counter()
    for raw_line in index_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        total_rows += 1
        try:
            item = json.loads(line)
        except Exception:
            parse_error_rows += 1
            continue
        if not isinstance(item, dict):
            parse_error_rows += 1
            continue
        action = str(item.get("action") or "unknown")
        source_file = str(item.get("source_file") or "unknown")
        run_id = str(item.get("run_id") or "")
        action_counter[action] += 1
        source_counter[source_file] += 1
        if run_id:
            if run_id != latest_run_id:
                latest_run_id = run_id
                latest_run_action_counter = Counter()
            latest_run_action_counter[action] += 1
    return CleanupConversionIndexSummary(
        index_file_exists=True,
        total_rows=total_rows,
        parse_error_rows=parse_error_rows,
        action_counts=dict(sorted(action_counter.items(), key=lambda kv: (-kv[1], kv[0]))),
        source_file_counts=dict(sorted(source_counter.items(), key=lambda kv: (-kv[1], kv[0]))),
        latest_run_id=latest_run_id,
        latest_run_action_counts=dict(sorted(latest_run_action_counter.items(), key=lambda kv: (-kv[1], kv[0]))),
    )


def render_cleanup_conversion_index_markdown(summary: CleanupConversionIndexSummary) -> str:
    lines = [
        "# Cleanup Conversion Index Summary",
        "",
        f"- Generated at: {datetime.now().isoformat(timespec='seconds')}",
    ]
    if not summary.index_file_exists:
        lines.extend(["- Index file: not found (`cleanup-conversion-index.jsonl`)", ""])
        return "\n".join(lines)

    valid = max(0, summary.total_rows - summary.parse_error_rows)
    lines.extend(
        [
            "- Index file: found (`cleanup-conversion-index.jsonl`)",
            "",
            "## Overall",
            f"- Rows: `{summary.total_rows}` (valid=`{valid}`, parse_errors=`{summary.parse_error_rows}`)",
            "",
            "## Actions",
        ]
    )
    if not summary.action_counts:
        lines.append("- none")
    else:
        for action, count in summary.action_counts.items():
            lines.append(f"- {action}: `{count}`")
    lines.extend(["", "## Top Source Files"])
    if not summary.source_file_counts:
        lines.append("- none")
    else:
        for source_file, count in list(summary.source_file_counts.items())[:10]:
            lines.append(f"- {source_file}: `{count}`")
    lines.extend(["", "## Latest Run"])
    if not summary.latest_run_id:
        lines.append("- run_id: `unknown`")
    else:
        lines.append(f"- run_id: `{summary.latest_run_id}`")
        if summary.latest_run_action_counts:
            for action, count in summary.latest_run_action_counts.items():
                lines.append(f"- {action}: `{count}`")
    lines.append("")
    return "\n".join(lines)


def render_daily_routing_metrics_markdown(summary: DailyRoutingMetricsSummary) -> str:
    lines = [
        "# Daily Routing Metrics Summary",
        "",
        f"- Generated at: {datetime.now().isoformat(timespec='seconds')}",
    ]
    if not summary.metrics_file_exists:
        lines.extend(["- Metrics file: not found (`daily-routing-metrics.jsonl`)", ""])
        return "\n".join(lines)

    total_valid = max(0, summary.total_rows - summary.parse_error_rows)
    ok_rate = (summary.structured_ok_count / total_valid * 100.0) if total_valid else 0.0
    fallback_rate = (summary.fallback_count / total_valid * 100.0) if total_valid else 0.0

    lines.extend(
        [
            "- Metrics file: found (`daily-routing-metrics.jsonl`)",
            "",
            "## Overall",
            f"- Rows: `{summary.total_rows}` (valid=`{total_valid}`, parse_errors=`{summary.parse_error_rows}`)",
            f"- sessions_with_routing_events: `{summary.sessions_with_routing_events}`",
            f"- sessions_with_fallback_events: `{summary.sessions_with_fallback_events}`",
            f"- Structured OK: `{summary.structured_ok_count}` ({ok_rate:.1f}%)",
            f"- Fallback: `{summary.fallback_count}` ({fallback_rate:.1f}%)",
            "",
            "## Fallback Reasons",
        ]
    )
    if not summary.fallback_reason_counts:
        lines.append("- none")
    else:
        for reason, count in summary.fallback_reason_counts.items():
            session_count = summary.fallback_sessions_by_reason.get(reason, 0)
            lines.append(f"- {reason}: `{count}` (sessions=`{session_count}`)")
    if summary.fallback_reason_counts:
        lines.extend(["", "## Suggested Fixes"])
        for reason in summary.fallback_reason_counts:
            hint = _FALLBACK_REASON_HINTS.get(reason)
            if hint:
                lines.append(f"- {reason}: {hint}")
    if total_valid > 0 and ok_rate < _STRUCTURED_DAILY_OK_RATE_WARN_THRESHOLD:
        if "## Suggested Fixes" not in lines:
            lines.extend(["", "## Suggested Fixes"])
        lines.append(
            "- structured_daily_ok rate is low; tighten consolidation prompt to prefer non-empty `daily_sections` and validate serializer shape."
        )

    lines.extend(["", "## Structured Source"])
    if not summary.structured_source_counts:
        lines.append("- none")
    else:
        for source, count in summary.structured_source_counts.items():
            lines.append(f"- {source}: `{count}`")

    lines.extend(["", "## By Date"])
    if not summary.by_date:
        lines.append("- none")
    else:
        for date, row in summary.by_date.items():
            valid = row["total"]
            ok_pct = (row["structured_ok"] / valid * 100.0) if valid else 0.0
            lines.append(
                f"- {date}: total=`{row['total']}`, structured_ok=`{row['structured_ok']}` ({ok_pct:.1f}%), fallback=`{row['fallback']}`"
            )
    lines.extend(["", "## Sessions (Top)"])
    if not summary.by_session:
        lines.append("- none")
    else:
        for idx, (session_key, count) in enumerate(summary.by_session.items()):
            if idx >= 10:
                break
            lines.append(f"- {session_key}: `{count}`")
    lines.append("")
    return "\n".join(lines)


def summarize_memory_update_guard_metrics(memory_dir: Path) -> MemoryUpdateGuardMetricsSummary:
    metrics_file = _metrics_file_for_read(memory_dir, "memory-update-guard-metrics.jsonl")
    if not metrics_file.exists():
        return MemoryUpdateGuardMetricsSummary(
            metrics_file_exists=False,
            total_rows=0,
            parse_error_rows=0,
            reason_counts={},
            sessions_with_hits_by_reason={},
            by_session={},
            preview_by_reason={},
            avg_current_memory_chars=0,
            avg_returned_memory_chars=0,
            sessions_with_guard_hits=0,
        )

    total_rows = 0
    parse_error_rows = 0
    reason_counter: Counter[str] = Counter()
    reason_session_counter: dict[str, set[str]] = {}
    session_counter: Counter[str] = Counter()
    preview_by_reason: dict[str, str] = {}
    sum_current_chars = 0
    sum_returned_chars = 0
    chars_count = 0

    for raw_line in metrics_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        total_rows += 1
        try:
            item = json.loads(line)
        except Exception:
            parse_error_rows += 1
            continue
        if not isinstance(item, dict):
            parse_error_rows += 1
            continue
        reason = str(item.get("reason") or "unknown")
        session_key = str(item.get("session_key") or "unknown")
        preview = str(item.get("candidate_preview") or "").strip()
        current_chars = item.get("current_memory_chars")
        returned_chars = item.get("returned_memory_chars")
        reason_counter[reason] += 1
        session_counter[session_key] += 1
        reason_session_counter.setdefault(reason, set()).add(session_key)
        if preview and reason not in preview_by_reason:
            preview_by_reason[reason] = preview
        if isinstance(current_chars, int) and isinstance(returned_chars, int):
            sum_current_chars += max(0, int(current_chars))
            sum_returned_chars += max(0, int(returned_chars))
            chars_count += 1

    return MemoryUpdateGuardMetricsSummary(
        metrics_file_exists=True,
        total_rows=total_rows,
        parse_error_rows=parse_error_rows,
        reason_counts=dict(sorted(reason_counter.items(), key=lambda kv: (-kv[1], kv[0]))),
        sessions_with_hits_by_reason=dict(
            sorted(
                ((reason, len(sessions)) for reason, sessions in reason_session_counter.items()),
                key=lambda kv: (-kv[1], kv[0]),
            )
        ),
        by_session=dict(sorted(session_counter.items(), key=lambda kv: (-kv[1], kv[0]))),
        preview_by_reason=preview_by_reason,
        avg_current_memory_chars=(int(sum_current_chars / chars_count) if chars_count > 0 else 0),
        avg_returned_memory_chars=(int(sum_returned_chars / chars_count) if chars_count > 0 else 0),
        sessions_with_guard_hits=len(session_counter),
    )


def render_memory_update_guard_metrics_markdown(summary: MemoryUpdateGuardMetricsSummary) -> str:
    lines = [
        "# Memory Update Guard Metrics Summary",
        "",
        f"- Generated at: {datetime.now().isoformat(timespec='seconds')}",
    ]
    if not summary.metrics_file_exists:
        lines.extend(["- Metrics file: not found (`memory-update-guard-metrics.jsonl`)", ""])
        return "\n".join(lines)

    total_valid = max(0, summary.total_rows - summary.parse_error_rows)
    lines.extend(
        [
            "- Metrics file: found (`memory-update-guard-metrics.jsonl`)",
            "",
            "## Overall",
            f"- Rows: `{summary.total_rows}` (valid=`{total_valid}`, parse_errors=`{summary.parse_error_rows}`)",
            f"- sessions_with_guard_hits: `{summary.sessions_with_guard_hits}`",
            f"- avg_current_memory_chars: `{summary.avg_current_memory_chars}`",
            f"- avg_returned_memory_chars: `{summary.avg_returned_memory_chars}`",
            f"- dominant_reason: `{next(iter(summary.reason_counts.keys())) if summary.reason_counts else 'none'}`",
            "",
            "## Guard Reasons",
        ]
    )
    if not summary.reason_counts:
        lines.append("- none")
    else:
        for reason, count in summary.reason_counts.items():
            lines.append(f"- {reason}: `{count}`")
    lines.extend(["", "## Reason Session Coverage"])
    if not summary.sessions_with_hits_by_reason:
        lines.append("- none")
    else:
        for reason, session_count in summary.sessions_with_hits_by_reason.items():
            lines.append(f"- {reason}: `{session_count}` sessions")
    lines.extend(["", "## Suggested Fixes"])
    if not summary.reason_counts:
        lines.append("- none")
    else:
        for reason, _ in summary.reason_counts.items():
            hint = _GUARD_REASON_HINTS.get(reason)
            if hint:
                lines.append(f"- {reason}: {hint}")
        too_long_count = int(summary.reason_counts.get("candidate_too_long", 0))
        if too_long_count >= 3 or (
            summary.avg_current_memory_chars > 0
            and summary.avg_returned_memory_chars > int(summary.avg_current_memory_chars * 1.8)
        ):
            lines.append(
                "- candidate_too_long trend: tighten consolidator output target (compact sections + concise bullets) before writing memory_update."
            )
    lines.extend(["", "## Priority Focus"])
    if not summary.reason_counts:
        lines.append("- none")
    else:
        for reason, count in list(summary.reason_counts.items())[:2]:
            hint = _GUARD_REASON_HINTS.get(reason)
            if hint:
                lines.append(f"- {reason}: `{count}` ({hint})")
            else:
                lines.append(f"- {reason}: `{count}`")
    lines.extend(["", "## Candidate Preview Samples"])
    if not summary.preview_by_reason:
        lines.append("- none")
    else:
        for reason, sample in summary.preview_by_reason.items():
            lines.append(f"- {reason}: `{sample}`")

    lines.extend(["", "## Sessions (Top)"])
    if not summary.by_session:
        lines.append("- none")
    else:
        for idx, (session_key, count) in enumerate(summary.by_session.items()):
            if idx >= 10:
                break
            lines.append(f"- {session_key}: `{count}`")
    lines.append("")
    return "\n".join(lines)


def summarize_memory_update_sanitize_metrics(memory_dir: Path) -> MemoryUpdateSanitizeMetricsSummary:
    metrics_file = _metrics_file_for_read(memory_dir, "memory-update-sanitize-metrics.jsonl")
    if not metrics_file.exists():
        return MemoryUpdateSanitizeMetricsSummary(
            metrics_file_exists=False,
            total_rows=0,
            parse_error_rows=0,
            total_recent_topic_sections_removed=0,
            total_transient_status_lines_removed=0,
            total_duplicate_bullets_removed=0,
            dominant_focus="none",
            sessions_with_sanitize_hits=0,
            sessions_with_effective_sanitize_hits=0,
            by_session={},
            top_recent_topic_sections={},
            top_transient_status_sections={},
            top_duplicate_bullet_sections={},
        )

    total_rows = 0
    parse_error_rows = 0
    recent_topic_removed = 0
    transient_lines_removed = 0
    duplicate_bullets_removed = 0
    session_counter: Counter[str] = Counter()
    effective_session_counter: Counter[str] = Counter()
    recent_section_counter: Counter[str] = Counter()
    transient_section_counter: Counter[str] = Counter()
    duplicate_section_counter: Counter[str] = Counter()

    for raw_line in metrics_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        total_rows += 1
        try:
            item = json.loads(line)
        except Exception:
            parse_error_rows += 1
            continue
        if not isinstance(item, dict):
            parse_error_rows += 1
            continue
        session_key = str(item.get("session_key") or "unknown")
        rcount = item.get("removed_recent_topic_section_count")
        tcount = item.get("removed_transient_status_line_count")
        dcount = item.get("removed_duplicate_bullet_count", 0)
        if not isinstance(rcount, int) or not isinstance(tcount, int) or not isinstance(dcount, int):
            parse_error_rows += 1
            continue
        recent_topic_removed += max(0, int(rcount))
        transient_lines_removed += max(0, int(tcount))
        duplicate_bullets_removed += max(0, int(dcount))
        raw_recent_sections = item.get("removed_recent_topic_sections")
        raw_transient_sections = item.get("removed_transient_status_sections")
        raw_duplicate_sections = item.get("removed_duplicate_bullet_sections")
        if isinstance(raw_recent_sections, list):
            for sec in raw_recent_sections:
                if isinstance(sec, str) and sec.strip():
                    recent_section_counter[sec.strip()] += 1
        if isinstance(raw_transient_sections, list):
            for sec in raw_transient_sections:
                if isinstance(sec, str) and sec.strip():
                    transient_section_counter[sec.strip()] += 1
        if isinstance(raw_duplicate_sections, list):
            for sec in raw_duplicate_sections:
                if isinstance(sec, str) and sec.strip():
                    duplicate_section_counter[sec.strip()] += 1
        session_counter[session_key] += 1
        if (max(0, int(rcount)) + max(0, int(tcount)) + max(0, int(dcount))) > 0:
            effective_session_counter[session_key] += 1

    if recent_topic_removed > transient_lines_removed and recent_topic_removed >= duplicate_bullets_removed:
        dominant_focus = "recent_topic"
    elif transient_lines_removed > recent_topic_removed and transient_lines_removed >= duplicate_bullets_removed:
        dominant_focus = "transient_status"
    elif duplicate_bullets_removed > 0:
        dominant_focus = "duplicate_bullets"
    elif recent_topic_removed == 0 and transient_lines_removed == 0 and duplicate_bullets_removed == 0:
        dominant_focus = "none"
    else:
        dominant_focus = "balanced"

    return MemoryUpdateSanitizeMetricsSummary(
        metrics_file_exists=True,
        total_rows=total_rows,
        parse_error_rows=parse_error_rows,
        total_recent_topic_sections_removed=recent_topic_removed,
        total_transient_status_lines_removed=transient_lines_removed,
        total_duplicate_bullets_removed=duplicate_bullets_removed,
        dominant_focus=dominant_focus,
        sessions_with_sanitize_hits=len(session_counter),
        sessions_with_effective_sanitize_hits=len(effective_session_counter),
        by_session=dict(sorted(session_counter.items(), key=lambda kv: (-kv[1], kv[0]))),
        top_recent_topic_sections=dict(sorted(recent_section_counter.items(), key=lambda kv: (-kv[1], kv[0]))),
        top_transient_status_sections=dict(sorted(transient_section_counter.items(), key=lambda kv: (-kv[1], kv[0]))),
        top_duplicate_bullet_sections=dict(sorted(duplicate_section_counter.items(), key=lambda kv: (-kv[1], kv[0]))),
    )


def render_memory_update_sanitize_metrics_markdown(summary: MemoryUpdateSanitizeMetricsSummary) -> str:
    lines = [
        "# Memory Update Sanitize Metrics Summary",
        "",
        f"- Generated at: {datetime.now().isoformat(timespec='seconds')}",
    ]
    if not summary.metrics_file_exists:
        lines.extend(["- Metrics file: not found (`memory-update-sanitize-metrics.jsonl`)", ""])
        return "\n".join(lines)

    total_valid = max(0, summary.total_rows - summary.parse_error_rows)
    lines.extend(
        [
            "- Metrics file: found (`memory-update-sanitize-metrics.jsonl`)",
            "",
            "## Overall",
            f"- Rows: `{summary.total_rows}` (valid=`{total_valid}`, parse_errors=`{summary.parse_error_rows}`)",
            f"- sessions_with_sanitize_hits: `{summary.sessions_with_sanitize_hits}`",
            f"- sessions_with_effective_sanitize_hits: `{summary.sessions_with_effective_sanitize_hits}`",
            f"- removed_recent_topic_sections(total): `{summary.total_recent_topic_sections_removed}`",
            f"- removed_transient_status_lines(total): `{summary.total_transient_status_lines_removed}`",
            f"- removed_duplicate_bullets(total): `{summary.total_duplicate_bullets_removed}`",
            f"- dominant_focus: `{summary.dominant_focus}`",
        ]
    )
    lines.extend(["", "## Suggested Fixes"])
    if summary.total_recent_topic_sections_removed > 0:
        lines.append(
            "- Recent-topic sanitize hits are non-zero: tighten consolidation instruction to keep ephemeral topics in `history_entry`/daily only."
        )
    if summary.total_transient_status_lines_removed > 0:
        lines.append(
            "- Transient-status sanitize hits are non-zero: avoid copying dated error/status lines into `memory_update`; keep durable constraints only."
        )
    if summary.total_duplicate_bullets_removed > 0:
        lines.append(
            "- Duplicate-bullet sanitize hits are non-zero: deduplicate repetitive bullets in `memory_update` and keep one durable statement per fact."
        )
    if (
        summary.total_recent_topic_sections_removed == 0
        and summary.total_transient_status_lines_removed == 0
        and summary.total_duplicate_bullets_removed == 0
    ):
        lines.append("- No sanitize-specific prompt adjustment needed based on current metrics.")
    else:
        lines.extend(["", "## Priority Focus"])
        focus_items = [
            ("recent_topic", summary.total_recent_topic_sections_removed),
            ("transient_status", summary.total_transient_status_lines_removed),
            ("duplicate_bullets", summary.total_duplicate_bullets_removed),
        ]
        focus_items.sort(key=lambda kv: (-kv[1], kv[0]))
        for key, count in focus_items:
            if count <= 0:
                continue
            if key == "recent_topic":
                lines.append(f"- {key}: `{count}` (prioritize reducing ephemeral topic sections in memory_update)")
            elif key == "duplicate_bullets":
                lines.append(f"- {key}: `{count}` (prioritize deduplicating repeated bullets in memory_update)")
            else:
                lines.append(f"- {key}: `{count}` (prioritize filtering dated status/error lines)")
    lines.extend(["", "## Top Sanitized Sections"])
    if not summary.top_recent_topic_sections and not summary.top_transient_status_sections:
        lines.append("- none")
    else:
        if summary.top_recent_topic_sections:
            lines.append(
                "- recent_topic: "
                + ", ".join([f"`{k}`:`{v}`" for k, v in list(summary.top_recent_topic_sections.items())[:3]])
            )
        if summary.top_transient_status_sections:
            lines.append(
                "- transient_status: "
                + ", ".join([f"`{k}`:`{v}`" for k, v in list(summary.top_transient_status_sections.items())[:3]])
            )
        if summary.top_duplicate_bullet_sections:
            lines.append(
                "- duplicate_bullets: "
                + ", ".join([f"`{k}`:`{v}`" for k, v in list(summary.top_duplicate_bullet_sections.items())[:3]])
            )
    lines.extend(["", "## Sessions (Top)"])
    if not summary.by_session:
        lines.append("- none")
    else:
        for idx, (session_key, count) in enumerate(summary.by_session.items()):
            if idx >= 10:
                break
            lines.append(f"- {session_key}: `{count}`")
    lines.append("")
    return "\n".join(lines)


def summarize_daily_archive_dry_run(memory_dir: Path, *, keep_days: int = 30) -> DailyArchiveDryRunSummary:
    window_days = max(1, int(keep_days))
    cutoff = datetime.now().date() - timedelta(days=window_days - 1)
    candidates: list[str] = []
    bullet_count = 0
    for p in _iter_daily_files(memory_dir):
        d = _daily_file_date(p)
        if d is None or d >= cutoff:
            continue
        candidates.append(p.name)
        text = p.read_text(encoding="utf-8")
        bullet_count += sum(1 for line in text.splitlines() if line.startswith("- "))
    return DailyArchiveDryRunSummary(
        keep_days=window_days,
        candidate_file_count=len(candidates),
        candidate_bullet_count=bullet_count,
        candidate_files=sorted(candidates),
    )


def render_daily_archive_dry_run_markdown(summary: DailyArchiveDryRunSummary) -> str:
    lines = [
        "# Daily Archive Dry-Run Summary",
        "",
        f"- Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"- Keep window: last `{summary.keep_days}` day(s)",
        f"- Candidate files: `{summary.candidate_file_count}`",
        f"- Candidate bullets: `{summary.candidate_bullet_count}`",
        "",
        "## Candidate Files",
    ]
    if not summary.candidate_files:
        lines.append("- none")
    else:
        for name in summary.candidate_files:
            lines.append(f"- {name}")
    lines.append("")
    return "\n".join(lines)


def summarize_memory_conflict_metrics(memory_dir: Path) -> MemoryConflictMetricsSummary:
    metrics_file = _metrics_file_for_read(memory_dir, "memory-conflict-metrics.jsonl")
    if not metrics_file.exists():
        return MemoryConflictMetricsSummary(
            metrics_file_exists=False,
            total_rows=0,
            parse_error_rows=0,
            key_counts={},
            by_session={},
        )

    total_rows = 0
    parse_error_rows = 0
    key_counter: Counter[str] = Counter()
    session_counter: Counter[str] = Counter()

    for raw_line in metrics_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        total_rows += 1
        try:
            item = json.loads(line)
        except Exception:
            parse_error_rows += 1
            continue
        if not isinstance(item, dict):
            parse_error_rows += 1
            continue
        key = str(item.get("conflict_key") or "unknown")
        session_key = str(item.get("session_key") or "unknown")
        key_counter[key] += 1
        session_counter[session_key] += 1

    return MemoryConflictMetricsSummary(
        metrics_file_exists=True,
        total_rows=total_rows,
        parse_error_rows=parse_error_rows,
        key_counts=dict(sorted(key_counter.items(), key=lambda kv: (-kv[1], kv[0]))),
        by_session=dict(sorted(session_counter.items(), key=lambda kv: (-kv[1], kv[0]))),
    )


def render_memory_conflict_metrics_markdown(summary: MemoryConflictMetricsSummary) -> str:
    lines = [
        "# Memory Conflict Metrics Summary",
        "",
        f"- Generated at: {datetime.now().isoformat(timespec='seconds')}",
    ]
    if not summary.metrics_file_exists:
        lines.extend(["- Metrics file: not found (`memory-conflict-metrics.jsonl`)", ""])
        return "\n".join(lines)
    total_valid = max(0, summary.total_rows - summary.parse_error_rows)
    lines.extend(
        [
            "- Metrics file: found (`memory-conflict-metrics.jsonl`)",
            "",
            "## Overall",
            f"- Rows: `{summary.total_rows}` (valid=`{total_valid}`, parse_errors=`{summary.parse_error_rows}`)",
            "",
            "## Conflict Keys",
        ]
    )
    if not summary.key_counts:
        lines.append("- none")
    else:
        for key, count in summary.key_counts.items():
            lines.append(f"- {key}: `{count}`")
    lines.extend(["", "## Sessions (Top)"])
    if not summary.by_session:
        lines.append("- none")
    else:
        for idx, (session_key, count) in enumerate(summary.by_session.items()):
            if idx >= 10:
                break
            lines.append(f"- {session_key}: `{count}`")
    lines.append("")
    return "\n".join(lines)


def summarize_context_trace(memory_dir: Path) -> ContextTraceSummary:
    trace_file = _metrics_file_for_read(memory_dir, "context-trace.jsonl")
    if not trace_file.exists():
        return ContextTraceSummary(
            trace_file_exists=False,
            total_rows=0,
            parse_error_rows=0,
            by_stage={},
            avg_tokens_by_stage={},
            prefix_stability_ratio=0.0,
        )

    total_rows = 0
    parse_error_rows = 0
    stage_counter: Counter[str] = Counter()
    stage_tokens_sum: Counter[str] = Counter()
    stage_tokens_count: Counter[str] = Counter()
    before_send_prefixes: list[str] = []

    for raw_line in trace_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        total_rows += 1
        try:
            item = json.loads(line)
        except Exception:
            parse_error_rows += 1
            continue
        if not isinstance(item, dict):
            parse_error_rows += 1
            continue
        stage = str(item.get("stage") or "unknown")
        stage_counter[stage] += 1
        est = item.get("estimated_tokens")
        if isinstance(est, int):
            stage_tokens_sum[stage] += est
            stage_tokens_count[stage] += 1
        if stage == "before_send":
            prefix = str(item.get("prefix_hash") or "")
            if prefix:
                before_send_prefixes.append(prefix)

    avg_tokens_by_stage: dict[str, int] = {}
    for stage, count in stage_tokens_count.items():
        if count > 0:
            avg_tokens_by_stage[stage] = int(stage_tokens_sum[stage] / count)

    stable_pairs = 0
    total_pairs = max(0, len(before_send_prefixes) - 1)
    for i in range(total_pairs):
        if before_send_prefixes[i] == before_send_prefixes[i + 1]:
            stable_pairs += 1
    ratio = (stable_pairs / total_pairs) if total_pairs > 0 else 0.0

    return ContextTraceSummary(
        trace_file_exists=True,
        total_rows=total_rows,
        parse_error_rows=parse_error_rows,
        by_stage=dict(sorted(stage_counter.items())),
        avg_tokens_by_stage=dict(sorted(avg_tokens_by_stage.items())),
        prefix_stability_ratio=ratio,
    )


def render_context_trace_markdown(summary: ContextTraceSummary) -> str:
    lines = [
        "# Context Trace Summary",
        "",
        f"- Generated at: {datetime.now().isoformat(timespec='seconds')}",
    ]
    if not summary.trace_file_exists:
        lines.extend(["- Trace file: not found (`context-trace.jsonl`)", ""])
        return "\n".join(lines)

    valid = max(0, summary.total_rows - summary.parse_error_rows)
    lines.extend(
        [
            "- Trace file: found (`context-trace.jsonl`)",
            "",
            "## Overall",
            f"- Rows: `{summary.total_rows}` (valid=`{valid}`, parse_errors=`{summary.parse_error_rows}`)",
            f"- Prefix stability ratio (before_send): `{summary.prefix_stability_ratio:.2f}`",
            "",
            "## Stage Counts",
        ]
    )
    if not summary.by_stage:
        lines.append("- none")
    else:
        for stage, count in summary.by_stage.items():
            lines.append(f"- {stage}: `{count}`")
    lines.extend(["", "## Avg Tokens By Stage"])
    if not summary.avg_tokens_by_stage:
        lines.append("- none")
    else:
        for stage, avg in summary.avg_tokens_by_stage.items():
            lines.append(f"- {stage}: `{avg}`")
    lines.append("")
    return "\n".join(lines)


def render_memory_observability_dashboard(memory_dir: Path) -> str:
    audit = run_memory_audit(memory_dir)
    routing = summarize_daily_routing_metrics(memory_dir)
    guard = summarize_memory_update_guard_metrics(memory_dir)
    sanitize = summarize_memory_update_sanitize_metrics(memory_dir)
    conflict = summarize_memory_conflict_metrics(memory_dir)
    trace = summarize_context_trace(memory_dir)
    cleanup_stage = summarize_cleanup_stage_metrics(memory_dir)
    cleanup_conv = summarize_cleanup_conversion_index(memory_dir)
    cleanup_preview = summarize_cleanup_drop_preview(
        memory_dir,
        drop_tool_activity_older_than_days=30,
        drop_non_decision_older_than_days=30,
    )
    preview_top_files = _top_candidate_file_pairs(cleanup_preview.by_file, limit=3)

    routing_valid = max(0, routing.total_rows - routing.parse_error_rows)
    routing_ok_rate = (routing.structured_ok_count / routing_valid * 100.0) if routing_valid else 0.0
    cleanup_valid = max(0, cleanup_stage.total_rows - cleanup_stage.parse_error_rows)
    cleanup_total_events = sum(cleanup_stage.total_stage_counts.values())

    lines = [
        "# Memory Observability Dashboard",
        "",
        f"- Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"- Memory dir: `{memory_dir}`",
        "",
        "## Quality Snapshot",
        f"- HISTORY long(>600): `{audit.history_long_entry_count}`",
        f"- DAILY long bullets(>240): `{audit.daily_long_bullet_count}`",
        f"- DAILY duplicates: `{audit.daily_duplicate_count}`",
        "",
        "## Routing",
        f"- structured_daily_ok rate: `{routing_ok_rate:.1f}%` (valid=`{routing_valid}`)",
        f"- sessions_with_routing_events: `{routing.sessions_with_routing_events}`",
        f"- sessions_with_fallback_events: `{routing.sessions_with_fallback_events}`",
        f"- top fallback reasons: `{', '.join(list(routing.fallback_reason_counts.keys())[:3]) if routing.fallback_reason_counts else 'none'}`",
        "",
        "## Guard / Conflict",
        f"- memory_update guard events: `{max(0, guard.total_rows - guard.parse_error_rows)}`",
        f"- guard avg_current_memory_chars: `{guard.avg_current_memory_chars}`",
        f"- guard avg_returned_memory_chars: `{guard.avg_returned_memory_chars}`",
        f"- memory_update sanitize events: `{max(0, sanitize.total_rows - sanitize.parse_error_rows)}`",
        f"- sessions_with_sanitize_hits: `{sanitize.sessions_with_sanitize_hits}`",
        f"- sessions_with_effective_sanitize_hits: `{sanitize.sessions_with_effective_sanitize_hits}`",
        f"- sanitize removed_recent_topic_sections(total): `{sanitize.total_recent_topic_sections_removed}`",
        f"- sanitize removed_transient_status_lines(total): `{sanitize.total_transient_status_lines_removed}`",
        f"- sanitize removed_duplicate_bullets(total): `{sanitize.total_duplicate_bullets_removed}`",
        f"- sanitize dominant_focus: `{sanitize.dominant_focus}`",
        f"- memory conflict events: `{max(0, conflict.total_rows - conflict.parse_error_rows)}`",
        "",
        "## Context Trace",
        f"- prefix stability ratio: `{trace.prefix_stability_ratio:.2f}`",
        f"- trace rows(valid): `{max(0, trace.total_rows - trace.parse_error_rows)}`",
        "",
        "## Pruning Stage Distribution",
        f"- cleanup rows(valid): `{cleanup_valid}`",
        f"- cleanup total stage events: `{cleanup_total_events}`",
        (
            f"- top stage mix: "
            f"`{', '.join([f'{k}:{v}' for k, v in sorted(cleanup_stage.total_stage_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:3]]) if cleanup_stage.total_stage_counts else 'none'}`"
        ),
        "",
        "## Cleanup Conversion Traceability",
        f"- conversion rows(valid): `{max(0, cleanup_conv.total_rows - cleanup_conv.parse_error_rows)}`",
        f"- latest cleanup run: `{cleanup_conv.latest_run_id or 'unknown'}`",
        (
            f"- top conversion actions: "
            f"`{', '.join([f'{k}:{v}' for k, v in list(cleanup_conv.action_counts.items())[:3]]) if cleanup_conv.action_counts else 'none'}`"
        ),
        "",
        "## Half-Life Drop Preview (30d)",
        f"- tool_activity candidates: `{cleanup_preview.drop_tool_activity_candidates}`",
        f"- non_decision candidates: `{cleanup_preview.drop_non_decision_candidates}`",
        f"- preview risk level: `{cleanup_preview.risk_level}`",
        f"- preview dominant driver: `{cleanup_preview.dominant_driver}`",
        f"- preview top candidate files: `{', '.join(preview_top_files) if preview_top_files else 'none'}`",
        "",
        "## Suggested Next Actions",
    ]

    if audit.daily_long_bullet_count > 0 or audit.daily_duplicate_count > 0:
        lines.append("- Run controlled cleanup: `nanobot memory-audit --apply --apply-recent-days 7 --apply-skip-history`")
    if routing.fallback_reason_counts:
        lines.append("- Inspect fallback fix hints via: `nanobot memory-audit --metrics-summary`")
    if routing_valid > 0 and routing_ok_rate < _STRUCTURED_DAILY_OK_RATE_WARN_THRESHOLD:
        lines.append(
            "- structured_daily_ok rate is below target; prioritize consolidation prompt tuning for stable `daily_sections` output."
        )
    if routing.sessions_with_fallback_events >= 5:
        lines.append(
            "- Fallback has spread across many sessions; prioritize serializer/schema fix before per-session prompt tuning."
        )
    if max(0, guard.total_rows - guard.parse_error_rows) > 0:
        lines.append("- Review guard reasons: `nanobot memory-audit --guard-metrics-summary`")
    unstructured_count = int(guard.reason_counts.get("unstructured_candidate", 0))
    date_overflow_count = int(guard.reason_counts.get("date_line_overflow", 0))
    url_overflow_count = int(guard.reason_counts.get("url_line_overflow", 0))
    duplicate_overflow_count = int(guard.reason_counts.get("duplicate_line_overflow", 0))
    if unstructured_count >= 3:
        lines.append("- Guard shows repeated unstructured candidates; enforce markdown section/bullet structure in consolidation output.")
    if date_overflow_count >= 3:
        lines.append("- Guard shows repeated dated-line overflow; keep timeline-like entries in daily/history rather than long-term memory.")
    if url_overflow_count >= 3:
        lines.append("- Guard shows repeated URL-line overflow; keep source link lists in daily/history and retain only durable conclusions.")
    if duplicate_overflow_count >= 3:
        lines.append("- Guard shows repeated duplicate-line overflow; deduplicate repetitive bullets before writing long-term memory.")
    if guard.avg_current_memory_chars > 0 and guard.avg_returned_memory_chars > int(guard.avg_current_memory_chars * 1.6):
        lines.append("- Guard shows oversized candidate trend; tighten consolidation prompt to return concise long-term memory updates.")
    if max(0, sanitize.total_rows - sanitize.parse_error_rows) > 0:
        lines.append("- Review sanitize hits: `nanobot memory-audit --sanitize-metrics-summary`")
    if sanitize.total_recent_topic_sections_removed >= 10:
        lines.append("- Recent-topic sanitize volume is high; reduce transient topic sections in memory_update output.")
    if sanitize.total_transient_status_lines_removed >= 10:
        lines.append("- Transient-status sanitize volume is high; keep dated status/error lines in daily/history only.")
    if sanitize.total_duplicate_bullets_removed >= 10:
        lines.append("- Duplicate-bullets sanitize volume is high; deduplicate repeated bullets in memory_update before write.")
    if sanitize.total_duplicate_bullets_removed >= 20:
        lines.append("- Duplicate-bullets sanitize volume is very high; tighten consolidator prompt to avoid repetitive bullet generation.")
    if sanitize.total_transient_status_lines_removed >= 20:
        lines.append("- Transient-status sanitize volume is high; tighten consolidation prompt to reduce noisy memory_update output.")
    if max(0, conflict.total_rows - conflict.parse_error_rows) > 0:
        lines.append("- Review preference conflicts: `nanobot memory-audit --conflict-metrics-summary`")
    if trace.trace_file_exists and trace.prefix_stability_ratio < 0.85:
        lines.append("- Prefix stability below target (0.85): inspect dynamic prompt mutations / tool catalog drift")
    if cleanup_stage.metrics_file_exists and cleanup_valid == 0:
        lines.append("- Cleanup stage metrics only has parse errors: inspect `cleanup-stage-metrics.jsonl` writer/format.")
    if cleanup_stage.total_stage_counts:
        tool_drop = cleanup_stage.total_stage_counts.get("drop_tool_activity", 0)
        non_decision_drop = cleanup_stage.total_stage_counts.get("drop_non_decision", 0)
        if cleanup_total_events > 0 and (tool_drop / cleanup_total_events) > 0.5:
            lines.append("- `drop_tool_activity` dominates cleanup events; verify retention window is not too aggressive.")
        if cleanup_total_events > 0 and (non_decision_drop / cleanup_total_events) > 0.35:
            lines.append(
                "- `drop_non_decision` ratio is high; review `--drop-non-decision-older-than-days` window to avoid over-pruning recall context."
            )
        if non_decision_drop >= 50:
            lines.append("- `drop_non_decision` absolute count is large; sample-check archived dailies before widening rollout.")
    total_preview_candidates = (
        cleanup_preview.drop_tool_activity_candidates + cleanup_preview.drop_non_decision_candidates
    )
    if total_preview_candidates > 0:
        if cleanup_preview.risk_level == "high":
            lines.append(
                "- High-risk preview: `nanobot memory-audit --apply-drop-preview --apply-recent-days 7 --drop-tool-activity-older-than-days 30 --drop-non-decision-older-than-days 30`"
            )
        elif cleanup_preview.risk_level == "medium":
            lines.append(
                "- Medium-risk rollout: `nanobot memory-audit --apply --apply-recent-days 7 --drop-tool-activity-older-than-days 30 --drop-non-decision-older-than-days 30 --apply-abort-on-high-risk`"
            )
        else:
            lines.append(
                "- Low-risk rollout: `nanobot memory-audit --apply --drop-tool-activity-older-than-days 30 --drop-non-decision-older-than-days 30 --apply-abort-on-high-risk`"
            )
    else:
        lines.append("- No half-life cleanup candidates in 30d preview window.")
    if max(0, cleanup_conv.total_rows - cleanup_conv.parse_error_rows) > 0 and not cleanup_conv.latest_run_id:
        lines.append("- Conversion index rows found without `run_id`; consider regenerating via latest `memory-audit --apply`.")
    if cleanup_preview.risk_level == "high":
        lines.append("- Half-life preview risk is high; run on recent-days scope first and sample-check before full apply.")
    if cleanup_preview.drop_non_decision_candidates >= 50:
        lines.append("- Preview shows high non-decision drops; consider narrowing `--drop-non-decision-older-than-days` first.")
    if lines[-1] == "## Suggested Next Actions":
        lines.append("- No immediate action required; continue observing daily snapshots.")

    lines.append("")
    return "\n".join(lines)


def build_cleanup_plan(memory_dir: Path) -> dict[str, object]:
    audit = run_memory_audit(memory_dir)
    plan: dict[str, object] = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "memory_dir": str(memory_dir),
        "safe_dry_run_only": True,
        "actions": [],
    }
    actions: list[dict[str, object]] = []

    if audit.history_long_entry_count > 0:
        actions.append(
            {
                "type": "history_trim_long_entries",
                "count": audit.history_long_entry_count,
                "target_max_chars": 600,
            }
        )
    if audit.daily_long_bullet_count > 0:
        actions.append(
            {
                "type": "daily_trim_long_bullets",
                "count": audit.daily_long_bullet_count,
                "target_max_chars": 240,
            }
        )
    if audit.daily_duplicate_count > 0:
        actions.append(
            {
                "type": "daily_deduplicate_exact_bullets",
                "count": audit.daily_duplicate_count,
            }
        )
    if audit.memory_timestamp_line_count > 0:
        actions.append(
            {
                "type": "memory_remove_timestamp_like_lines",
                "count": audit.memory_timestamp_line_count,
            }
        )
    if audit.daily_orphan_files:
        actions.append(
            {
                "type": "review_orphan_daily_files",
                "files": audit.daily_orphan_files,
            }
        )

    plan["actions"] = actions
    return plan


def render_audit_markdown(audit: MemoryAudit) -> str:
    lines = [
        "# Memory Audit Report",
        "",
        f"- Generated at: {datetime.now().isoformat(timespec='seconds')}",
        f"- Memory dir: `{audit.memory_dir}`",
        "",
        "## Snapshot",
        f"- MEMORY.md exists: `{audit.memory_file_exists}`",
        f"- HISTORY.md exists: `{audit.history_file_exists}`",
        f"- Daily files: `{len(audit.daily_files)}` ({', '.join(audit.daily_files) if audit.daily_files else 'none'})",
        "",
        "## Findings",
        f"- MEMORY: timestamp-like lines = `{audit.memory_timestamp_line_count}`, urls = `{audit.memory_url_line_count}`, code fences = `{audit.memory_code_fence_count}`",
        f"- HISTORY: entries = `{audit.history_entry_count}`, long(>600) = `{audit.history_long_entry_count}`, duplicates = `{audit.history_duplicate_count}`, code fences = `{audit.history_code_fence_count}`",
        f"- DAILY: long bullets(>240) = `{audit.daily_long_bullet_count}`, duplicates = `{audit.daily_duplicate_count}`, timestamp-style bullets = `{audit.daily_timestamp_bullet_count}`",
        f"- DAILY orphan files (date not seen in HISTORY): `{len(audit.daily_orphan_files)}` ({', '.join(audit.daily_orphan_files) if audit.daily_orphan_files else 'none'})",
        "",
        "## Assessment",
    ]
    if (
        audit.memory_timestamp_line_count == 0
        and audit.history_long_entry_count == 0
        and audit.daily_long_bullet_count == 0
        and audit.daily_duplicate_count == 0
    ):
        lines.append("- Memory quality is currently healthy for the checked rules.")
    else:
        lines.append("- Memory quality has drift in at least one dimension; keep dry-run cleanup enabled and observe for 24-72h.")
    lines.append("")
    return "\n".join(lines)


def write_cleanup_plan_json(plan: dict[str, object], output_file: Path) -> None:
    output_file.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
