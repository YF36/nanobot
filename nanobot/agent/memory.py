"""Memory system for persistent agent memory."""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from nanobot.logging import get_logger

from nanobot.utils.helpers import ensure_dir

if TYPE_CHECKING:
    from nanobot.providers.base import LLMProvider
    from nanobot.session.manager import Session

logger = get_logger(__name__)


_SAVE_MEMORY_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "save_memory",
            "description": "Save the memory consolidation result to persistent storage.",
            "parameters": {
                "type": "object",
                "properties": {
                    "history_entry": {
                        "type": "string",
                        "description": "A paragraph (2-5 sentences) summarizing key events/decisions/topics. "
                        "Start with [YYYY-MM-DD HH:MM]. Include detail useful for grep search.",
                    },
                    "memory_update": {
                        "type": "string",
                        "description": "Full updated long-term memory as markdown. Include all existing "
                        "facts plus new ones. Return unchanged if nothing new.",
                    },
                    "daily_sections": {
                        "type": "object",
                        "description": "Optional structured daily memory bullets for daily log sections.",
                        "properties": {
                            "topics": {"type": "array", "items": {"type": "string"}},
                            "decisions": {"type": "array", "items": {"type": "string"}},
                            "tool_activity": {"type": "array", "items": {"type": "string"}},
                            "open_questions": {"type": "array", "items": {"type": "string"}},
                        },
                    },
                },
                "required": ["history_entry", "memory_update"],
            },
        },
    }
]


class MemoryStore:
    """Two-layer memory: MEMORY.md (long-term facts) + HISTORY.md (grep-searchable log)."""

    _CHARS_PER_TOKEN = 4
    _CONSOLIDATION_REPLY_RESERVE_TOKENS = 4096
    _CONSOLIDATION_SOFT_INPUT_TOKENS = 24_000
    _CONSOLIDATION_TOOLCALL_RETRIES = 1
    _MEMORY_TRUNCATION_NOTICE = "\n\n[... long-term memory truncated for consolidation ...]\n\n"
    _MEMORY_SECTION_REJECT_PATTERNS = (
        re.compile(r"(今天|今日|近期).*(讨论|主题)"),
        re.compile(r"today.*(discussion|topics?)", re.IGNORECASE),
        re.compile(r"recent.*(discussion|topics?)", re.IGNORECASE),
        re.compile(r"\b20\d{2}-\d{2}-\d{2}\b"),
    )
    _MEMORY_TRANSIENT_STATUS_SECTION_PATTERNS = (
        re.compile(r"(system|technical).*(issues?|status)", re.IGNORECASE),
        re.compile(r"(api|service).*(issues?|status|errors?)", re.IGNORECASE),
        re.compile(r"(系统|技术).*(问题|状态)"),
        re.compile(r"(接口|服务).*(问题|状态|报错)"),
    )
    _MEMORY_TRANSIENT_STATUS_LINE_PATTERNS = (
        re.compile(r"\b20\d{2}-\d{2}-\d{2}\b"),
        re.compile(r"\b(today|yesterday|recently|currently|temporary|temporarily)\b", re.IGNORECASE),
        re.compile(r"\b(error|failed|failure|timeout|timed out|unavailable)\b", re.IGNORECASE),
        re.compile(r"\b(4\d{2}|5\d{2})\b"),
        re.compile(r"(报错|错误|失败|超时|不可用|临时)"),
    )
    _MEMORY_SANITIZE_LOG_SAMPLE_LIMIT = 3
    _MEMORY_SANITIZE_LOG_SAMPLE_CHARS = 120
    _MEMORY_UPDATE_SHRINK_GUARD_RATIO = 0.4
    _MEMORY_UPDATE_MIN_HEADING_RETAIN_RATIO = 0.5
    _MEMORY_UPDATE_MIN_STRUCTURED_CHARS = 120
    _MEMORY_UPDATE_MAX_CHARS = 12_000
    _MEMORY_UPDATE_CODE_FENCE_MAX = 0
    _MEMORY_UPDATE_URL_LINE_MIN_COUNT = 3
    _MEMORY_UPDATE_URL_LINE_RATIO_GUARD = 0.2
    _MEMORY_UPDATE_DATE_LINE_RATIO_GUARD = 0.2
    _MEMORY_UPDATE_DATE_LINE_MIN_COUNT = 3
    _MEMORY_UPDATE_DUPLICATE_LINE_MIN_COUNT = 4
    _MEMORY_UPDATE_DUPLICATE_LINE_RATIO_GUARD = 0.4
    _DATE_TOKEN_RE = re.compile(r"\b20\d{2}-\d{2}-\d{2}\b")
    _HISTORY_ENTRY_DATE_RE = re.compile(r"^\[(20\d{2}-\d{2}-\d{2})(?:\s+\d{2}:\d{2})?\]")
    _DAILY_FILE_DATE_RE = re.compile(r"^(20\d{2}-\d{2}-\d{2})\.md$")
    _RECENT_DAILY_DEFAULT_SECTIONS = frozenset({"Topics", "Decisions", "Open Questions", "Entries"})
    _DAILY_MEMORY_SECTIONS = (
        "Topics",
        "Decisions",
        "Tool Activity",
        "Open Questions",
    )
    _DAILY_SECTIONS_SCHEMA_MAP = {
        "topics": "Topics",
        "decisions": "Decisions",
        "tool_activity": "Tool Activity",
        "open_questions": "Open Questions",
    }
    _HISTORY_ENTRY_MAX_CHARS = 600
    _DAILY_BULLET_MAX_CHARS = 240
    _FALLBACK_PREFIX_PATTERNS = (
        re.compile(
            r"^(?:User|Assistant|System)\s+(?:asked|requested|shared|sent|provided|explained|confirmed|discussed)\s+",
            re.IGNORECASE,
        ),
        re.compile(r"^(?:用户|助手|系统)(?:询问|请求|分享|发送|提供|解释|确认|讨论)(?:了)?"),
    )
    _FALLBACK_META_CLAUSE_PATTERNS = (
        re.compile(r"\bThis interaction indicates\b.*$", re.IGNORECASE),
        re.compile(r"\baligns with user's established interest\b.*$", re.IGNORECASE),
        re.compile(r"\bNo new information added\b.*$", re.IGNORECASE),
    )
    _PREFERENCE_KEY_PATTERNS = (
        ("language", re.compile(r"(语言|language)", re.IGNORECASE)),
        ("communication_style", re.compile(r"(沟通风格|communication style)", re.IGNORECASE)),
    )

    def __init__(self, workspace: Path):
        self.memory_dir = ensure_dir(workspace / "memory")
        self.memory_file = self.memory_dir / "MEMORY.md"
        self.history_file = self.memory_dir / "HISTORY.md"
        self.daily_routing_metrics_file = self.memory_dir / "daily-routing-metrics.jsonl"
        self.memory_update_guard_metrics_file = self.memory_dir / "memory-update-guard-metrics.jsonl"
        self.memory_update_sanitize_metrics_file = self.memory_dir / "memory-update-sanitize-metrics.jsonl"
        self.memory_conflict_metrics_file = self.memory_dir / "memory-conflict-metrics.jsonl"

    def read_long_term(self) -> str:
        if self.memory_file.exists():
            return self.memory_file.read_text(encoding="utf-8")
        return ""

    def write_long_term(self, content: str) -> None:
        self.memory_file.write_text(content, encoding="utf-8")

    def append_history(self, entry: str) -> None:
        with open(self.history_file, "a", encoding="utf-8") as f:
            f.write(entry.rstrip() + "\n\n")

    def _append_daily_routing_metric(
        self,
        *,
        session_key: str,
        date_str: str,
        structured_daily_ok: bool,
        fallback_reason: str,
        structured_keys: list[str],
        structured_bullet_count: int,
    ) -> None:
        row = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "session_key": session_key,
            "date": date_str,
            "structured_daily_ok": structured_daily_ok,
            "fallback_used": (not structured_daily_ok),
            "fallback_reason": fallback_reason,
            "structured_keys": structured_keys,
            "structured_bullet_count": structured_bullet_count,
        }
        try:
            with open(self.daily_routing_metrics_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception:
            logger.warning(
                "Failed to append daily routing metric",
                file=str(self.daily_routing_metrics_file),
            )

    def _append_memory_update_guard_metric(
        self,
        *,
        session_key: str,
        reason: str,
        current_memory_chars: int,
        returned_memory_chars: int,
        candidate_preview: str,
    ) -> None:
        row = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "session_key": session_key,
            "reason": reason,
            "current_memory_chars": current_memory_chars,
            "returned_memory_chars": returned_memory_chars,
            "candidate_preview": candidate_preview,
        }
        try:
            with open(self.memory_update_guard_metrics_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception:
            logger.warning(
                "Failed to append memory_update guard metric",
                file=str(self.memory_update_guard_metrics_file),
            )

    def _append_memory_conflict_metric(
        self,
        *,
        session_key: str,
        conflict_key: str,
        old_value: str,
        new_value: str,
    ) -> None:
        row = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "session_key": session_key,
            "conflict_key": conflict_key,
            "old_value": old_value,
            "new_value": new_value,
        }
        try:
            with open(self.memory_conflict_metrics_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception:
            logger.warning(
                "Failed to append memory conflict metric",
                file=str(self.memory_conflict_metrics_file),
            )

    def _append_memory_update_sanitize_metric(
        self,
        *,
        session_key: str,
        removed_recent_topic_section_count: int,
        removed_transient_status_line_count: int,
        removed_duplicate_bullet_count: int,
        removed_recent_topic_sections: list[str],
        removed_transient_status_sections: list[str],
        removed_duplicate_bullet_sections: list[str],
    ) -> None:
        row = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "session_key": session_key,
            "removed_recent_topic_section_count": removed_recent_topic_section_count,
            "removed_transient_status_line_count": removed_transient_status_line_count,
            "removed_duplicate_bullet_count": removed_duplicate_bullet_count,
            "removed_recent_topic_sections": removed_recent_topic_sections[:3],
            "removed_transient_status_sections": removed_transient_status_sections[:3],
            "removed_duplicate_bullet_sections": removed_duplicate_bullet_sections[:3],
        }
        try:
            with open(self.memory_update_sanitize_metrics_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception:
            logger.warning(
                "Failed to append memory_update sanitize metric",
                file=str(self.memory_update_sanitize_metrics_file),
            )

    @classmethod
    def _extract_preference_values(cls, text: str) -> dict[str, str]:
        values: dict[str, str] = {}
        in_preferences = False
        for raw in text.splitlines():
            line = raw.strip()
            if line.startswith("## "):
                in_preferences = line[3:].strip().lower() in {"preferences", "偏好", "用户偏好"}
                continue
            if not in_preferences or not line.startswith("-"):
                continue
            item = line.lstrip("-").strip()
            for key, pat in cls._PREFERENCE_KEY_PATTERNS:
                if pat.search(item):
                    if ":" in item:
                        values[key] = item.split(":", 1)[1].strip()
                    elif "：" in item:
                        values[key] = item.split("：", 1)[1].strip()
                    else:
                        values[key] = item
        return values

    @classmethod
    def _detect_preference_conflicts(cls, current_memory: str, candidate_update: str) -> list[dict[str, str]]:
        current_vals = cls._extract_preference_values(current_memory)
        candidate_vals = cls._extract_preference_values(candidate_update)
        conflicts: list[dict[str, str]] = []
        for key, old_value in current_vals.items():
            new_value = candidate_vals.get(key)
            if not new_value:
                continue
            if old_value != new_value:
                conflicts.append(
                    {
                        "conflict_key": key,
                        "old_value": old_value,
                        "new_value": new_value,
                    }
                )
        return conflicts

    @classmethod
    def _history_entry_date(cls, entry: str) -> str:
        if m := cls._HISTORY_ENTRY_DATE_RE.match(entry.strip()):
            return m.group(1)
        return datetime.now().strftime("%Y-%m-%d")

    def _daily_memory_file(self, date_str: str) -> Path:
        return self.memory_dir / f"{date_str}.md"

    @classmethod
    def _daily_memory_template(cls, date_str: str) -> str:
        sections = "".join(f"## {name}\n\n" for name in cls._DAILY_MEMORY_SECTIONS)
        return f"# {date_str}\n\n{sections}"

    @staticmethod
    def _history_entry_body(entry: str) -> str:
        text = entry.strip()
        if text.startswith("[") and "]" in text:
            return text.split("]", 1)[1].strip()
        return text

    @classmethod
    def _daily_section_for_history_entry(cls, entry: str) -> str:
        body = cls._history_entry_body(entry).lower()
        if any(k in body for k in ("decid", "decision", "选择", "决定", "方案")):
            return "Decisions"
        if any(k in body for k in ("open item", "follow-up", "follow up", "todo", "next step", "待办", "后续", "未完成")):
            return "Open Questions"
        if any(k in body for k in ("tool", "command", "exec", "edited", "modified", "created", "read_file", "write_file", "edit_file", "bash")):
            return "Tool Activity"
        return "Topics"

    @staticmethod
    def _append_bullet_to_daily_section(daily_file: Path, section: str, bullet: str) -> bool:
        text = daily_file.read_text(encoding="utf-8")
        target = f"## {section}"
        idx = text.find(target)
        if idx == -1:
            target = "## Entries"
            idx = text.find(target)
        if idx == -1:
            with open(daily_file, "a", encoding="utf-8") as f:
                f.write(f"\n## Entries\n\n- {bullet}\n")
            return True
        insert_at = text.find("\n## ", idx + len(target))
        if insert_at == -1:
            insert_at = len(text)
        prefix = text[:insert_at]
        suffix = text[insert_at:]
        section_body = text[idx:insert_at]
        if f"\n- {bullet}\n" in section_body or section_body.endswith(f"\n- {bullet}"):
            return False
        if not prefix.endswith("\n"):
            prefix += "\n"
        new_text = prefix + f"- {bullet}\n" + suffix
        daily_file.write_text(new_text, encoding="utf-8")
        return True

    @classmethod
    def _normalize_history_entry(cls, entry: object) -> tuple[str | None, str]:
        if entry is None:
            return None, "missing"
        if not isinstance(entry, str):
            return None, "invalid_type"
        text = " ".join(entry.split()).strip()
        if not text:
            return None, "empty"
        if "```" in text:
            return None, "contains_code_block"
        if len(text) > cls._HISTORY_ENTRY_MAX_CHARS:
            text = text[: cls._HISTORY_ENTRY_MAX_CHARS - 3].rstrip() + "..."
        if not cls._HISTORY_ENTRY_DATE_RE.match(text):
            text = f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] {text}"
        return text, "ok"

    @classmethod
    def _sanitize_daily_bullet(cls, item: object) -> tuple[str | None, str]:
        if not isinstance(item, str):
            return None, "invalid_item"
        text = " ".join(item.split()).strip()
        if not text:
            return None, "empty_item"
        if "```" in text:
            return None, "contains_code_block"
        if len(text) > cls._DAILY_BULLET_MAX_CHARS:
            text = text[: cls._DAILY_BULLET_MAX_CHARS - 3].rstrip() + "..."
        return text, "ok"

    @classmethod
    def _compact_fallback_daily_bullet(cls, text: str) -> str:
        """Conservative de-noise for fallback daily bullets.

        Keeps core meaning while removing common templated phrasing.
        """
        compact = " ".join(text.split()).strip()
        if not compact:
            return ""
        for pat in cls._FALLBACK_PREFIX_PATTERNS:
            compact = pat.sub("", compact).strip()
        for pat in cls._FALLBACK_META_CLAUSE_PATTERNS:
            compact = pat.sub("", compact).strip()
        compact = compact.strip()
        compact = re.sub(r"^[-:;\s]+", "", compact)
        return compact

    @classmethod
    def _normalize_daily_sections_detailed(cls, value: object) -> tuple[dict[str, list[str]] | None, str]:
        if value is None:
            return None, "missing"
        if not isinstance(value, dict):
            return None, "not_object"
        normalized: dict[str, list[str]] = {}
        for key in cls._DAILY_SECTIONS_SCHEMA_MAP:
            raw = value.get(key)
            if raw is None:
                continue
            if not isinstance(raw, list):
                return None, f"invalid_type:{key}"
            items: list[str] = []
            for item in raw:
                text, sanitize_reason = cls._sanitize_daily_bullet(item)
                if sanitize_reason == "invalid_item":
                    return None, f"invalid_item:{key}"
                if text:
                    items.append(text)
            if items:
                normalized[key] = items
        if not normalized:
            return None, "empty"
        return normalized, "ok"

    @classmethod
    def _normalize_daily_sections(cls, value: object) -> dict[str, list[str]] | None:
        normalized, reason = cls._normalize_daily_sections_detailed(value)
        return normalized if reason == "ok" else None

    def append_daily_sections_detailed(self, date_str: str, sections: object) -> tuple[Path, bool, dict[str, object]]:
        daily_file = self._daily_memory_file(date_str)
        normalized, reason = self._normalize_daily_sections_detailed(sections)
        if normalized is None:
            return daily_file, False, {
                "reason": reason,
                "keys": [],
                "bullet_count": 0,
                "created": False,
            }
        created = False
        if not daily_file.exists():
            daily_file.write_text(self._daily_memory_template(date_str), encoding="utf-8")
            created = True
        wrote = 0
        for schema_key, section_name in self._DAILY_SECTIONS_SCHEMA_MAP.items():
            for bullet in normalized.get(schema_key, []):
                if self._append_bullet_to_daily_section(daily_file, section_name, bullet):
                    wrote += 1
        details = {
            "reason": "ok",
            "keys": sorted(normalized.keys()),
            "bullet_count": wrote,
            "created": created,
        }
        logger.debug(
            "Memory daily structured sections appended",
            date=date_str,
            created=created,
            file=str(daily_file),
            keys=details["keys"],
            bullet_count=wrote,
        )
        return daily_file, True, details

    def append_daily_sections(self, date_str: str, sections: object) -> tuple[Path, bool]:
        daily_file, ok, _ = self.append_daily_sections_detailed(date_str, sections)
        return daily_file, ok

    def append_daily_history_entry(self, entry: str) -> Path:
        date_str = self._history_entry_date(entry)
        daily_file = self._daily_memory_file(date_str)
        created = False
        if not daily_file.exists():
            daily_file.write_text(self._daily_memory_template(date_str), encoding="utf-8")
            created = True
        section = self._daily_section_for_history_entry(entry)
        fallback_bullet = self._compact_fallback_daily_bullet(self._history_entry_body(entry))
        fallback_bullet, _ = self._sanitize_daily_bullet(fallback_bullet)
        wrote = False
        if fallback_bullet:
            wrote = self._append_bullet_to_daily_section(daily_file, section, fallback_bullet)
        logger.debug(
            "Memory daily entry appended",
            date=date_str,
            section=section,
            created=created,
            wrote=wrote,
            file=str(daily_file),
            sample=self._truncate_log_sample(self._history_entry_body(entry)),
        )
        return daily_file

    def get_memory_context(self) -> str:
        long_term = self.read_long_term()
        return f"## Long-term Memory\n{long_term}" if long_term else ""

    def get_recent_daily_context(
        self,
        *,
        days: int = 7,
        max_bullets: int = 12,
        max_chars: int = 1200,
        include_tool_activity: bool = False,
    ) -> str:
        """Return a compact recent-daily snippet for recall-style queries."""
        window_days = max(1, days)
        bullet_budget = max(1, max_bullets)
        char_budget = max(200, max_chars)
        allowed_sections = set(self._RECENT_DAILY_DEFAULT_SECTIONS)
        if include_tool_activity:
            allowed_sections.add("Tool Activity")
        cutoff = datetime.now().date() - timedelta(days=window_days - 1)

        dated_files: list[tuple[str, Path]] = []
        for p in self.memory_dir.glob("*.md"):
            m = self._DAILY_FILE_DATE_RE.match(p.name)
            if not m:
                continue
            date_str = m.group(1)
            try:
                d = datetime.strptime(date_str, "%Y-%m-%d").date()
            except ValueError:
                continue
            if d >= cutoff:
                dated_files.append((date_str, p))
        dated_files.sort(key=lambda x: x[0], reverse=True)

        lines: list[str] = []
        total_chars = 0
        for date_str, path in dated_files:
            text = path.read_text(encoding="utf-8")
            current_section = ""
            for raw in text.splitlines():
                if raw.startswith("## "):
                    current_section = raw[3:].strip()
                    continue
                if not raw.startswith("- "):
                    continue
                if current_section and current_section not in allowed_sections:
                    continue
                bullet = raw[2:].strip()
                if not bullet:
                    continue
                section_label = f" [{current_section}]" if current_section else ""
                line = f"- {date_str}{section_label}: {bullet}"
                projected = total_chars + len(line) + 1
                if len(lines) >= bullet_budget or projected > char_budget:
                    return "\n".join(lines)
                lines.append(line)
                total_chars = projected
        return "\n".join(lines)

    @classmethod
    def _estimate_tokens(cls, text: str) -> int:
        """Coarse token estimate for memory consolidation budgeting."""
        return max(1, len(text) // cls._CHARS_PER_TOKEN) if text else 0

    @staticmethod
    def _is_context_length_error(text: str | None) -> bool:
        if not text:
            return False
        lower = text.lower()
        return (
            "maximum context length" in lower
            or "exceeds the model's maximum context length" in lower
            or "input tokens exceeds" in lower
            or "context length" in lower
        )

    def _format_consolidation_lines(self, messages: list[dict]) -> list[str]:
        lines: list[str] = []
        for m in messages:
            if not m.get("content"):
                continue
            tools = f" [tools: {', '.join(m['tools_used'])}]" if m.get("tools_used") else ""
            lines.append(f"[{m.get('timestamp', '?')[:16]}] {m['role'].upper()}{tools}: {m['content']}")
        return lines

    def _build_consolidation_prompt(self, current_memory: str, lines: list[str]) -> str:
        return f"""Process this conversation and call the save_memory tool with your consolidation.

## Current Long-term Memory
{current_memory or "(empty)"}

## Conversation to Process
{chr(10).join(lines)}"""

    @staticmethod
    def _consolidation_system_prompt(strict_tool_call: bool = False) -> str:
        base = (
            "You are a memory consolidation agent. Call the save_memory tool with your consolidation of the conversation. "
            "Treat MEMORY.md as long-term stable facts only (user preferences, durable project context, stable environment constraints). "
            "Do NOT copy recent discussion topics, knowledge-answer content, long summaries, tables, or tool outputs into memory_update; "
            "those belong in history_entry only. Temporary system/API error statuses, one-off incidents, and dated operational notes "
            "should usually stay out of memory_update (or be reduced to a durable configuration fact only). "
            "Optionally include daily_sections with concise bullets for Topics/Decisions/Tool Activity/Open Questions."
        )
        if not strict_tool_call:
            return base
        return (
            base
            + " Do not reply with plain text. You MUST call save_memory exactly once "
              "with both history_entry and memory_update."
        )

    @classmethod
    def _truncate_log_sample(cls, text: str) -> str:
        text = " ".join(text.split())
        if len(text) <= cls._MEMORY_SANITIZE_LOG_SAMPLE_CHARS:
            return text
        return text[: cls._MEMORY_SANITIZE_LOG_SAMPLE_CHARS - 3].rstrip() + "..."

    @classmethod
    def _sanitize_memory_update_detailed(
        cls,
        update: str,
        current_memory: str,
    ) -> tuple[str, dict[str, object]]:
        """Remove obviously short-lived/topic-dump content and return classification stats."""
        if not update.strip():
            return update, {
                "removed_sections": [],
                "removed_recent_topic_sections": [],
                "removed_transient_status_sections": [],
                "removed_transient_status_line_count": 0,
                "removed_duplicate_bullet_count": 0,
                "recent_topic_section_samples": [],
                "transient_status_line_samples": [],
                "duplicate_bullet_section_samples": [],
            }

        lines = update.splitlines()
        kept: list[str] = []
        removed_headings: list[str] = []
        removed_recent_sections: list[str] = []
        removed_transient_status_sections: list[str] = []
        removed_transient_status_line_count = 0
        recent_topic_section_samples: list[str] = []
        transient_status_line_samples: list[str] = []
        i = 0
        while i < len(lines):
            line = lines[i]
            if line.startswith("## "):
                heading = line[3:].strip()
                if any(p.search(heading) for p in cls._MEMORY_SECTION_REJECT_PATTERNS):
                    removed_headings.append(heading)
                    removed_recent_sections.append(heading)
                    if len(recent_topic_section_samples) < cls._MEMORY_SANITIZE_LOG_SAMPLE_LIMIT:
                        recent_topic_section_samples.append(cls._truncate_log_sample(heading))
                    i += 1
                    while i < len(lines) and not lines[i].startswith("## "):
                        i += 1
                    continue
                if any(p.search(heading) for p in cls._MEMORY_TRANSIENT_STATUS_SECTION_PATTERNS):
                    section_lines = [line]
                    removed_lines_in_section = 0
                    i += 1
                    while i < len(lines) and not lines[i].startswith("## "):
                        candidate = lines[i]
                        stripped = candidate.strip()
                        if stripped and any(p.search(candidate) for p in cls._MEMORY_TRANSIENT_STATUS_LINE_PATTERNS):
                            removed_lines_in_section += 1
                            removed_transient_status_line_count += 1
                            if len(transient_status_line_samples) < cls._MEMORY_SANITIZE_LOG_SAMPLE_LIMIT:
                                transient_status_line_samples.append(cls._truncate_log_sample(candidate))
                            i += 1
                            continue
                        section_lines.append(candidate)
                        i += 1
                    if any(s.strip() for s in section_lines[1:]):
                        kept.extend(section_lines)
                    else:
                        removed_heading = f"{heading} (transient status only)"
                        removed_headings.append(removed_heading)
                        removed_transient_status_sections.append(heading)
                    if removed_lines_in_section:
                        removed_transient_status_sections.append(heading)
                    continue
            kept.append(line)
            i += 1

        deduped_kept, removed_duplicate_bullet_count, duplicate_bullet_section_samples = (
            cls._dedupe_markdown_bullets_by_section(kept)
        )
        kept = deduped_kept
        sanitized = "\n".join(kept).strip()
        if not sanitized:
            sanitized = current_memory
        else:
            sanitized = sanitized + ("\n" if update.endswith("\n") else "")
        details = {
            "removed_sections": removed_headings,
            "removed_recent_topic_sections": removed_recent_sections,
            "removed_transient_status_sections": sorted(set(removed_transient_status_sections)),
            "removed_transient_status_line_count": removed_transient_status_line_count,
            "removed_duplicate_bullet_count": removed_duplicate_bullet_count,
            "recent_topic_section_samples": recent_topic_section_samples,
            "transient_status_line_samples": transient_status_line_samples,
            "duplicate_bullet_section_samples": duplicate_bullet_section_samples,
        }
        return sanitized, details

    @classmethod
    def _dedupe_markdown_bullets_by_section(cls, lines: list[str]) -> tuple[list[str], int, list[str]]:
        kept: list[str] = []
        seen: set[tuple[str, str]] = set()
        current_heading = "(root)"
        removed = 0
        section_samples: list[str] = []
        for line in lines:
            if line.startswith("## "):
                heading = line[3:].strip()
                current_heading = heading or "(untitled)"
                kept.append(line)
                continue
            stripped = line.strip()
            if not stripped.startswith("- "):
                kept.append(line)
                continue
            normalized = re.sub(r"\s+", " ", stripped[2:].strip()).lower()
            if not normalized:
                kept.append(line)
                continue
            key = (current_heading, normalized)
            if key in seen:
                removed += 1
                if (
                    current_heading not in section_samples
                    and len(section_samples) < cls._MEMORY_SANITIZE_LOG_SAMPLE_LIMIT
                ):
                    section_samples.append(current_heading)
                continue
            seen.add(key)
            kept.append(line)
        return kept, removed, section_samples

    @classmethod
    def _sanitize_memory_update(cls, update: str, current_memory: str) -> tuple[str, list[str]]:
        """Backward-compatible wrapper returning removed section headings only."""
        sanitized, details = cls._sanitize_memory_update_detailed(update, current_memory)
        return sanitized, list(details["removed_sections"])

    @classmethod
    def _extract_h2_headings(cls, text: str) -> list[str]:
        headings: list[str] = []
        for line in text.splitlines():
            if line.startswith("## "):
                heading = line[3:].strip()
                if heading:
                    headings.append(heading)
        return headings

    @classmethod
    def _has_structured_markers(cls, text: str) -> bool:
        for raw in text.splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith("## ") or line.startswith("- "):
                return True
        return False

    @classmethod
    def _memory_update_guard_reason(cls, current_memory: str, candidate_update: str) -> str | None:
        """Return reason string when memory_update looks suspicious and should be skipped."""
        current = current_memory.strip()
        candidate = candidate_update.strip()
        if not candidate:
            return "empty_candidate"
        if not current:
            return None

        current_len = len(current)
        candidate_len = len(candidate)
        if candidate_len > cls._MEMORY_UPDATE_MAX_CHARS:
            return "candidate_too_long"
        if candidate.count("```") > cls._MEMORY_UPDATE_CODE_FENCE_MAX:
            return "contains_code_block"
        if current_len >= 200 and candidate_len < int(current_len * cls._MEMORY_UPDATE_SHRINK_GUARD_RATIO):
            return "excessive_shrink"
        if candidate_len >= cls._MEMORY_UPDATE_MIN_STRUCTURED_CHARS and not cls._has_structured_markers(candidate):
            return "unstructured_candidate"
        non_empty_lines = [ln.strip() for ln in candidate.splitlines() if ln.strip()]
        if non_empty_lines:
            date_lines = sum(1 for ln in non_empty_lines if cls._DATE_TOKEN_RE.search(ln))
            if (
                date_lines >= cls._MEMORY_UPDATE_DATE_LINE_MIN_COUNT
                and (date_lines / len(non_empty_lines)) >= cls._MEMORY_UPDATE_DATE_LINE_RATIO_GUARD
            ):
                return "date_line_overflow"
            url_lines = sum(1 for ln in non_empty_lines if "http://" in ln or "https://" in ln)
            if (
                url_lines >= cls._MEMORY_UPDATE_URL_LINE_MIN_COUNT
                and (url_lines / len(non_empty_lines)) >= cls._MEMORY_UPDATE_URL_LINE_RATIO_GUARD
            ):
                return "url_line_overflow"
            content_lines: list[str] = []
            for line in non_empty_lines:
                normalized = line
                if normalized.startswith("## "):
                    continue
                if normalized.startswith("- "):
                    normalized = normalized[2:].strip()
                normalized = re.sub(r"\s+", " ", normalized).strip().lower()
                if normalized:
                    content_lines.append(normalized)
            if content_lines:
                duplicate_max = max(Counter(content_lines).values())
                if (
                    duplicate_max >= cls._MEMORY_UPDATE_DUPLICATE_LINE_MIN_COUNT
                    and (duplicate_max / len(content_lines)) >= cls._MEMORY_UPDATE_DUPLICATE_LINE_RATIO_GUARD
                ):
                    return "duplicate_line_overflow"

        current_h2 = cls._extract_h2_headings(current)
        if current_h2:
            candidate_h2 = set(cls._extract_h2_headings(candidate))
            kept = sum(1 for h in current_h2 if h in candidate_h2)
            keep_ratio = kept / len(current_h2)
            if keep_ratio < cls._MEMORY_UPDATE_MIN_HEADING_RETAIN_RATIO:
                return "heading_retention_too_low"
        return None

    def _fit_chunk_by_soft_budget(
        self,
        messages: list[dict],
        current_memory: str,
    ) -> list[dict]:
        """Select a prefix chunk that fits a conservative input budget."""
        if not messages:
            return []
        # Prompt scaffolding estimate plus current memory
        budget_memory, _ = self._fit_memory_context_by_soft_budget(current_memory, [])
        scaffold = self._build_consolidation_prompt(budget_memory, [])
        budget = max(
            1,
            self._CONSOLIDATION_SOFT_INPUT_TOKENS - self._estimate_tokens(scaffold) - self._CONSOLIDATION_REPLY_RESERVE_TOKENS,
        )
        total = 0
        chunk: list[dict] = []
        for m in messages:
            lines = self._format_consolidation_lines([m])
            delta = self._estimate_tokens("\n".join(lines)) if lines else 1
            if chunk and total + delta > budget:
                break
            chunk.append(m)
            total += delta
        return chunk or messages[:1]

    def _fit_memory_context_by_soft_budget(
        self,
        current_memory: str,
        lines: list[str],
    ) -> tuple[str, bool]:
        """Trim long-term memory context to fit the consolidation input budget.

        Returns (memory_for_prompt, was_truncated).
        """
        if not current_memory:
            return current_memory, False

        # Available input budget after accounting for scaffold, conversation chunk, and reply reserve.
        prompt_without_memory = self._build_consolidation_prompt("", lines)
        available_tokens = (
            self._CONSOLIDATION_SOFT_INPUT_TOKENS
            - self._CONSOLIDATION_REPLY_RESERVE_TOKENS
            - self._estimate_tokens(prompt_without_memory)
        )
        if available_tokens <= 0:
            return "", True

        if self._estimate_tokens(current_memory) <= available_tokens:
            return current_memory, False

        # Keep a head+tail slice so stable section headers and recent facts both survive.
        max_chars = max(64, available_tokens * self._CHARS_PER_TOKEN)
        notice = self._MEMORY_TRUNCATION_NOTICE
        room = max_chars - len(notice)
        if room <= 0:
            return notice.strip(), True

        head_chars = max(1, room // 2)
        tail_chars = max(1, room - head_chars)
        trimmed = current_memory[:head_chars] + notice + current_memory[-tail_chars:]

        # Tighten if coarse char->token estimate still overshoots.
        while self._estimate_tokens(trimmed) > available_tokens and (head_chars > 16 or tail_chars > 16):
            head_chars = max(16, int(head_chars * 0.85))
            tail_chars = max(16, int(tail_chars * 0.85))
            trimmed = current_memory[:head_chars] + notice + current_memory[-tail_chars:]

        return trimmed, True

    async def consolidate(
        self,
        session: Session,
        provider: LLMProvider,
        model: str,
        *,
        archive_all: bool = False,
        memory_window: int = 50,
    ) -> bool:
        """Consolidate old messages into MEMORY.md + HISTORY.md via LLM tool call.

        Returns True on success (including no-op), False on failure.
        """
        # Snapshot length before the LLM call so new messages appended
        # concurrently don't shift the last_consolidated boundary.
        snapshot_len = len(session.messages)

        if archive_all:
            old_messages = session.messages[:snapshot_len]
            keep_count = 0
            logger.info("Memory consolidation (archive_all)", message_count=snapshot_len)
        else:
            keep_count = memory_window // 2
            if snapshot_len <= keep_count:
                return True
            if snapshot_len - session.last_consolidated <= 0:
                return True
            old_messages = session.messages[session.last_consolidated:snapshot_len - keep_count]
            if not old_messages:
                return True
            logger.info("Memory consolidation", to_consolidate=len(old_messages), keep=keep_count)

        start_index = session.last_consolidated
        target_last = 0 if archive_all else snapshot_len - keep_count
        pending = list(old_messages)
        processed_count = 0

        try:
            while pending:
                current_memory = self.read_long_term()
                chunk = self._fit_chunk_by_soft_budget(pending, current_memory)
                if not chunk:
                    break

                # Retry with smaller prefixes if provider still reports context overflow.
                while True:
                    lines = self._format_consolidation_lines(chunk)
                    if not lines:
                        # Nothing useful to summarize; just mark the messages as processed.
                        processed_count += len(chunk)
                        pending = pending[len(chunk):]
                        if not archive_all:
                            session.last_consolidated = min(target_last, start_index + processed_count)
                        break

                    prompt_memory, memory_truncated = self._fit_memory_context_by_soft_budget(current_memory, lines)
                    if memory_truncated:
                        logger.warning(
                            "Memory consolidation prompt truncating long-term memory context",
                            memory_chars=len(current_memory),
                            prompt_memory_chars=len(prompt_memory),
                            chunk_messages=len(chunk),
                        )
                    prompt = self._build_consolidation_prompt(prompt_memory, lines)
                    response = None
                    for attempt in range(self._CONSOLIDATION_TOOLCALL_RETRIES + 1):
                        strict_retry = attempt > 0
                        response = await provider.chat(
                            messages=[
                                {"role": "system", "content": self._consolidation_system_prompt(strict_tool_call=strict_retry)},
                                {"role": "user", "content": prompt},
                            ],
                            tools=_SAVE_MEMORY_TOOL,
                            model=model,
                            temperature=0.0,
                        )

                        if response.has_tool_calls:
                            break
                        if getattr(response, "finish_reason", "") == "error":
                            break
                        if attempt < self._CONSOLIDATION_TOOLCALL_RETRIES:
                            logger.warning(
                                "Memory consolidation response missing save_memory tool call, retrying",
                                retry=attempt + 1,
                            )

                    assert response is not None

                    if getattr(response, "finish_reason", "") == "error" and self._is_context_length_error(response.content):
                        if len(chunk) <= 1:
                            logger.warning("Memory consolidation failed: prompt exceeds context even for single message")
                            return False
                        chunk = chunk[: max(1, len(chunk) // 2)]
                        continue

                    if not response.has_tool_calls:
                        if getattr(response, "finish_reason", "") == "error":
                            logger.warning("Memory consolidation LLM call failed", error=response.content or "(empty)")
                        else:
                            logger.warning("Memory consolidation: LLM did not call save_memory, skipping")
                        return False

                    args = response.tool_calls[0].arguments
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except Exception:
                            logger.warning("Memory consolidation: failed to parse tool call arguments JSON")
                            return False
                    if not isinstance(args, dict):
                        logger.warning(
                            "Memory consolidation: unexpected arguments type",
                            arguments_type=type(args).__name__,
                        )
                        return False
                    entry = args.get("history_entry")
                    entry_text, entry_reason = self._normalize_history_entry(entry)
                    if entry_text is not None:
                        self.append_history(entry_text)
                        date_str = self._history_entry_date(entry_text)
                        raw_daily_sections = args.get("daily_sections")
                        _, structured_daily_ok, structured_daily_details = self.append_daily_sections_detailed(
                            date_str,
                            raw_daily_sections,
                        )
                        if not structured_daily_ok:
                            self.append_daily_history_entry(entry_text)
                        logger.debug(
                            "Memory daily routing decision",
                            date=date_str,
                            structured_daily_ok=structured_daily_ok,
                            fallback_used=(not structured_daily_ok),
                            fallback_reason=structured_daily_details["reason"],
                            structured_keys=structured_daily_details["keys"],
                            structured_bullet_count=structured_daily_details["bullet_count"],
                        )
                        self._append_daily_routing_metric(
                            session_key=session.key,
                            date_str=date_str,
                            structured_daily_ok=structured_daily_ok,
                            fallback_reason=str(structured_daily_details["reason"]),
                            structured_keys=list(structured_daily_details["keys"]),
                            structured_bullet_count=int(structured_daily_details["bullet_count"]),
                        )
                    else:
                        logger.warning(
                            "Memory consolidation skipped history_entry due to quality gate",
                            reason=entry_reason,
                        )
                    if update := args.get("memory_update"):
                        if not isinstance(update, str):
                            update = json.dumps(update, ensure_ascii=False)
                        update, sanitize_details = self._sanitize_memory_update_detailed(update, current_memory)
                        if (
                            sanitize_details["removed_sections"]
                            or sanitize_details["removed_transient_status_line_count"]
                            or sanitize_details["removed_duplicate_bullet_count"]
                        ):
                            logger.warning(
                                "Memory consolidation sanitized long-term memory update",
                                removed_sections=sanitize_details["removed_sections"],
                                removed_recent_topic_sections=sanitize_details["removed_recent_topic_sections"],
                                removed_transient_status_sections=sanitize_details["removed_transient_status_sections"],
                                removed_transient_status_line_count=sanitize_details["removed_transient_status_line_count"],
                                removed_duplicate_bullet_count=sanitize_details["removed_duplicate_bullet_count"],
                                recent_topic_section_samples=sanitize_details["recent_topic_section_samples"],
                                transient_status_line_samples=sanitize_details["transient_status_line_samples"],
                                duplicate_bullet_section_samples=sanitize_details["duplicate_bullet_section_samples"],
                            )
                            self._append_memory_update_sanitize_metric(
                                session_key=session.key,
                                removed_recent_topic_section_count=len(
                                    list(sanitize_details["removed_recent_topic_sections"])
                                ),
                                removed_transient_status_line_count=int(
                                    sanitize_details["removed_transient_status_line_count"]
                                ),
                                removed_duplicate_bullet_count=int(
                                    sanitize_details["removed_duplicate_bullet_count"]
                                ),
                                removed_recent_topic_sections=list(sanitize_details["removed_recent_topic_sections"]),
                                removed_transient_status_sections=list(
                                    sanitize_details["removed_transient_status_sections"]
                                ),
                                removed_duplicate_bullet_sections=list(
                                    sanitize_details["duplicate_bullet_section_samples"]
                                ),
                            )
                        if memory_truncated:
                            logger.warning(
                                "Skipping memory_update write because long-term memory context was truncated",
                                current_memory_chars=len(current_memory),
                                returned_memory_chars=len(update),
                            )
                        elif update != current_memory:
                            guard_reason = self._memory_update_guard_reason(current_memory, update)
                            if guard_reason:
                                logger.warning(
                                    "Skipping memory_update write due to guard",
                                    reason=guard_reason,
                                    current_memory_chars=len(current_memory),
                                    returned_memory_chars=len(update),
                                )
                                self._append_memory_update_guard_metric(
                                    session_key=session.key,
                                    reason=guard_reason,
                                    current_memory_chars=len(current_memory),
                                    returned_memory_chars=len(update),
                                    candidate_preview=self._truncate_log_sample(update),
                                )
                            else:
                                conflicts = self._detect_preference_conflicts(current_memory, update)
                                for conflict in conflicts:
                                    logger.warning(
                                        "Memory preference conflict detected",
                                        key=conflict["conflict_key"],
                                        old_value=conflict["old_value"],
                                        new_value=conflict["new_value"],
                                    )
                                    self._append_memory_conflict_metric(
                                        session_key=session.key,
                                        conflict_key=conflict["conflict_key"],
                                        old_value=conflict["old_value"],
                                        new_value=conflict["new_value"],
                                    )
                                self.write_long_term(update)

                    processed_count += len(chunk)
                    pending = pending[len(chunk):]
                    if not archive_all:
                        # Process at most one chunk per normal pass to keep latency bounded.
                        session.last_consolidated = min(target_last, start_index + processed_count)
                        logger.info(
                            "Memory consolidation done",
                            snapshot_len=snapshot_len,
                            last_consolidated=session.last_consolidated,
                            processed_messages=processed_count,
                            partial=(session.last_consolidated < target_last),
                        )
                        return True
                    break

            if archive_all:
                session.last_consolidated = 0
            else:
                session.last_consolidated = min(target_last, start_index + processed_count)
            logger.info(
                "Memory consolidation done",
                snapshot_len=snapshot_len,
                last_consolidated=session.last_consolidated,
                processed_messages=processed_count,
                partial=(not archive_all and session.last_consolidated < target_last),
            )
            return True
        except Exception:
            logger.exception("Memory consolidation failed")
            return False
