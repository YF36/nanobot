"""Read-only memory audit and optional cleanup planning utilities."""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


_DATE_FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.md$")
_HISTORY_ENTRY_RE = re.compile(r"^\[(20\d{2}-\d{2}-\d{2})(?:\s+\d{2}:\d{2})?\]\s*(.*)$")


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


def _iter_daily_files(memory_dir: Path) -> list[Path]:
    items: list[Path] = []
    for p in sorted(memory_dir.glob("*.md")):
        if p.name in {"MEMORY.md", "HISTORY.md"}:
            continue
        if _DATE_FILE_RE.match(p.name):
            items.append(p)
    return items


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
