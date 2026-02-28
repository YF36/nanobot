"""Memory system for persistent agent memory."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from nanobot.memory.consolidation import ConsolidationPipeline
from nanobot.memory.guard_policy import MemoryGuardPolicy
from nanobot.memory.io import MemoryIO
from nanobot.memory.routing_policy import DailyRoutingPlan, DailyRoutingPolicy
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
    _DAILY_SECTIONS_REVERSE_SCHEMA_MAP = {
        "Topics": "topics",
        "Decisions": "decisions",
        "Tool Activity": "tool_activity",
        "Open Questions": "open_questions",
    }
    _HISTORY_ENTRY_MAX_CHARS = 600
    _DAILY_BULLET_MAX_CHARS = 240
    _SYNTH_DAILY_MAX_BULLETS = 4
    _SYNTH_DAILY_MIN_BULLET_CHARS = 8
    _SYNTH_DAILY_EXCLUDE_PATTERNS = (
        re.compile(r"\b(4\d{2}|5\d{2})\b"),
        re.compile(r"\b(error|failed|failure|timeout|timed out|unavailable|temporary)\b", re.IGNORECASE),
        re.compile(r"(报错|错误|失败|超时|不可用|临时)"),
    )
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
        ("timezone", re.compile(r"(时区|time ?zone)", re.IGNORECASE)),
        ("output_format", re.compile(r"(输出格式|格式|output format)", re.IGNORECASE)),
        ("tone", re.compile(r"(语气|tone)", re.IGNORECASE)),
    )
    _PREFERENCE_CONFLICT_STRATEGIES = {"keep_new", "keep_old", "ask_user", "merge"}
    _H2_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$")

    @dataclass
    class _ChunkProcessResult:
        status: str  # processed | retry_smaller | fatal
        processed_count: int = 0
        next_chunk: list[dict] | None = None

    @dataclass
    class _ConsolidationScope:
        snapshot_len: int
        keep_count: int
        old_messages: list[dict]
        start_index: int
        target_last: int
        archive_all: bool

    @dataclass
    class _ChunkPromptData:
        prompt: str
        memory_truncated: bool

    @dataclass
    class _ConsolidationCallMeta:
        preferred_retry_used: bool
        tool_call_has_daily_sections: bool

    @dataclass
    class _ConsolidationProgress:
        session_key: str
        start_index: int
        target_last: int
        archive_all: bool
        keep_count: int
        snapshot_len: int
        processed_count: int

    def __init__(
        self,
        workspace: Path,
        *,
        daily_sections_mode: str = "compatible",
        preference_conflict_strategy: str = "keep_new",
        preference_conflict_keys: tuple[str, ...] | None = None,
    ):
        self.memory_dir = ensure_dir(workspace / "memory")
        self.observability_dir = ensure_dir(self.memory_dir / "observability")
        self.memory_file = self.memory_dir / "MEMORY.md"
        self.history_file = self.memory_dir / "HISTORY.md"
        self.consolidation_progress_file = self.memory_dir / "consolidation-in-progress.json"
        self.daily_routing_metrics_file = self.observability_dir / "daily-routing-metrics.jsonl"
        self.memory_update_guard_metrics_file = self.observability_dir / "memory-update-guard-metrics.jsonl"
        self.memory_update_sanitize_metrics_file = self.observability_dir / "memory-update-sanitize-metrics.jsonl"
        self.memory_conflict_metrics_file = self.observability_dir / "memory-conflict-metrics.jsonl"
        self.memory_update_outcome_metrics_file = self.observability_dir / "memory-update-outcome.jsonl"
        mode = (daily_sections_mode or "compatible").strip().lower()
        if mode not in {"compatible", "preferred", "required"}:
            mode = "compatible"
        self.daily_sections_mode = mode
        strategy = (preference_conflict_strategy or "keep_new").strip().lower()
        if strategy not in self._PREFERENCE_CONFLICT_STRATEGIES:
            strategy = "keep_new"
        self.preference_conflict_strategy = strategy
        configured_keys = tuple(
            k.strip().lower() for k in (preference_conflict_keys or ("language", "communication_style")) if k.strip()
        )
        self.preference_conflict_keys = configured_keys or ("language", "communication_style")
        self._io = MemoryIO()
        self._routing_policy = DailyRoutingPolicy(
            normalize_daily_sections_detailed=self._normalize_daily_sections_detailed,
            coerce_partial_daily_sections=self._coerce_partial_daily_sections,
            synthesize_daily_sections_from_entry=self._synthesize_daily_sections_from_entry,
        )
        self._pipeline = ConsolidationPipeline(self)

    def read_long_term(self) -> str:
        if self.memory_file.exists():
            return self.memory_file.read_text(encoding="utf-8")
        return ""

    def write_long_term(self, content: str) -> None:
        self._io.write_text(self.memory_file, content, encoding="utf-8")

    def append_history(self, entry: str) -> None:
        self._io.append_text(self.history_file, entry.rstrip() + "\n\n", encoding="utf-8")

    def _append_daily_routing_metric(
        self,
        *,
        session_key: str,
        date_str: str,
        structured_daily_ok: bool,
        fallback_reason: str,
        structured_keys: list[str],
        structured_bullet_count: int,
        structured_source: str,
        model_daily_sections_ok: bool,
        model_daily_sections_reason: str,
        preferred_retry_used: bool = False,
        tool_call_has_daily_sections: bool = False,
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
            "structured_source": structured_source,
            "model_daily_sections_ok": model_daily_sections_ok,
            "model_daily_sections_reason": model_daily_sections_reason,
            "preferred_retry_used": preferred_retry_used,
            "tool_call_has_daily_sections": tool_call_has_daily_sections,
        }
        try:
            self._io.append_text(
                self.daily_routing_metrics_file,
                json.dumps(row, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
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
            self._io.append_text(
                self.memory_update_guard_metrics_file,
                json.dumps(row, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
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
        resolution: str = "keep_new",
    ) -> None:
        row = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "session_key": session_key,
            "conflict_key": conflict_key,
            "old_value": old_value,
            "new_value": new_value,
            "resolution": resolution,
        }
        try:
            self._io.append_text(
                self.memory_conflict_metrics_file,
                json.dumps(row, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
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
            self._io.append_text(
                self.memory_update_sanitize_metrics_file,
                json.dumps(row, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        except Exception:
            logger.warning(
                "Failed to append memory_update sanitize metric",
                file=str(self.memory_update_sanitize_metrics_file),
            )

    def _append_memory_update_outcome_metric(
        self,
        *,
        session_key: str,
        outcome: str,
        guard_reason: str | None = None,
        sanitize_changes: int = 0,
        merge_applied: bool = False,
        conflict_count: int = 0,
    ) -> None:
        row = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "session_key": session_key,
            "outcome": outcome,
            "guard_reason": guard_reason,
            "sanitize_changes": int(max(0, sanitize_changes)),
            "merge_applied": bool(merge_applied),
            "conflict_count": int(max(0, conflict_count)),
        }
        try:
            self._io.append_text(
                self.memory_update_outcome_metrics_file,
                json.dumps(row, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        except Exception:
            logger.warning(
                "Failed to append memory_update outcome metric",
                file=str(self.memory_update_outcome_metrics_file),
            )

    def _write_consolidation_progress(
        self,
        *,
        progress: _ConsolidationProgress,
    ) -> None:
        payload = {
            "version": 1,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "session_key": progress.session_key,
            "start_index": int(progress.start_index),
            "target_last": int(progress.target_last),
            "archive_all": bool(progress.archive_all),
            "keep_count": int(progress.keep_count),
            "snapshot_len": int(progress.snapshot_len),
            "processed_count": int(progress.processed_count),
        }
        self._io.write_text(
            self.consolidation_progress_file,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )

    def _read_consolidation_progress(self) -> _ConsolidationProgress | None:
        if not self.consolidation_progress_file.exists():
            return None
        try:
            payload = json.loads(self.consolidation_progress_file.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        try:
            return self._ConsolidationProgress(
                session_key=str(payload.get("session_key") or ""),
                start_index=int(payload.get("start_index") or 0),
                target_last=int(payload.get("target_last") or 0),
                archive_all=bool(payload.get("archive_all", False)),
                keep_count=int(payload.get("keep_count") or 0),
                snapshot_len=int(payload.get("snapshot_len") or 0),
                processed_count=int(payload.get("processed_count") or 0),
            )
        except Exception:
            return None

    def _clear_consolidation_progress(self) -> None:
        if self.consolidation_progress_file.exists():
            self.consolidation_progress_file.unlink()

    def _build_recovery_scope(
        self,
        *,
        session: Session,
        archive_all: bool,
    ) -> _ConsolidationScope | None:
        progress = self._read_consolidation_progress()
        if progress is None:
            return None
        if not progress.session_key or progress.session_key != session.key:
            return None
        if archive_all != progress.archive_all:
            # Caller changed mode; ignore stale progress for safety.
            return None

        start_index = max(0, progress.start_index + progress.processed_count)
        target_last = max(0, progress.target_last)
        snapshot_len = len(session.messages)
        if snapshot_len <= start_index or target_last <= start_index:
            self._clear_consolidation_progress()
            return None

        end_index = min(snapshot_len, target_last)
        old_messages = session.messages[start_index:end_index]
        if not old_messages:
            self._clear_consolidation_progress()
            return None

        logger.info(
            "Memory consolidation recovery scope loaded",
            start_index=start_index,
            target_last=target_last,
            recover_count=len(old_messages),
            archive_all=archive_all,
        )
        return self._ConsolidationScope(
            snapshot_len=snapshot_len,
            keep_count=max(0, progress.keep_count),
            old_messages=list(old_messages),
            start_index=start_index,
            target_last=target_last,
            archive_all=archive_all,
        )

    @classmethod
    def _extract_preference_values(cls, text: str) -> dict[str, str]:
        return MemoryGuardPolicy.extract_preference_values(
            text,
            key_patterns=list(cls._PREFERENCE_KEY_PATTERNS),
        )

    def _detect_preference_conflicts(self, current_memory: str, candidate_update: str) -> list[dict[str, str]]:
        active_keys = set(self.preference_conflict_keys)
        key_patterns = [(k, p) for k, p in self._PREFERENCE_KEY_PATTERNS if k in active_keys]
        if not key_patterns:
            key_patterns = list(self._PREFERENCE_KEY_PATTERNS)
        return MemoryGuardPolicy.detect_preference_conflicts(
            current_memory,
            candidate_update,
            key_patterns=key_patterns,
        )

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

    def _append_bullet_to_daily_section(self, daily_file: Path, section: str, bullet: str) -> bool:
        text = daily_file.read_text(encoding="utf-8")
        target = f"## {section}"
        idx = text.find(target)
        if idx == -1:
            target = "## Entries"
            idx = text.find(target)
        if idx == -1:
            self._io.append_text(daily_file, f"\n## Entries\n\n- {bullet}\n", encoding="utf-8")
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
        self._io.write_text(daily_file, new_text, encoding="utf-8")
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

    @classmethod
    def _coerce_partial_daily_sections(cls, value: object) -> dict[str, list[str]] | None:
        """Best-effort salvage for partially invalid model payloads.

        Keeps valid list[string] sections and drops invalid keys/items.
        """
        if not isinstance(value, dict):
            return None
        normalized: dict[str, list[str]] = {}
        for key in cls._DAILY_SECTIONS_SCHEMA_MAP:
            raw = value.get(key)
            if raw is None or not isinstance(raw, list):
                continue
            items: list[str] = []
            for item in raw:
                text, _ = cls._sanitize_daily_bullet(item)
                if text:
                    items.append(text)
            if items:
                normalized[key] = items
        return normalized or None

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
            self._io.write_text(daily_file, self._daily_memory_template(date_str), encoding="utf-8")
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
            self._io.write_text(daily_file, self._daily_memory_template(date_str), encoding="utf-8")
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

    @classmethod
    def _synthesize_daily_sections_from_entry(cls, entry: str) -> dict[str, list[str]] | None:
        body = cls._history_entry_body(entry)
        compact = cls._compact_fallback_daily_bullet(body)
        if not compact:
            return None
        raw_parts = re.split(r"[。！？!?;；]\s*", compact)
        candidates = [p.strip() for p in raw_parts if p and p.strip()]
        if not candidates:
            candidates = [compact]

        sections: dict[str, list[str]] = {}
        seen: set[str] = set()
        used = 0
        for part in candidates:
            bullet, _ = cls._sanitize_daily_bullet(part)
            if not bullet:
                continue
            if len(bullet) < cls._SYNTH_DAILY_MIN_BULLET_CHARS:
                continue
            if any(p.search(bullet) for p in cls._SYNTH_DAILY_EXCLUDE_PATTERNS):
                continue
            normalized = " ".join(bullet.split()).lower()
            if normalized in seen:
                continue
            seen.add(normalized)
            section = cls._daily_section_for_history_entry(bullet)
            schema_key = cls._DAILY_SECTIONS_REVERSE_SCHEMA_MAP.get(section)
            if not schema_key:
                continue
            sections.setdefault(schema_key, []).append(bullet)
            used += 1
            if used >= cls._SYNTH_DAILY_MAX_BULLETS:
                break
        return sections or None

    def _resolve_daily_routing_plan(
        self,
        *,
        entry_text: str,
        raw_daily_sections: object,
    ) -> DailyRoutingPlan:
        """Resolve best payload for structured daily write with stable source semantics."""
        return self._routing_policy.resolve(
            entry_text=entry_text,
            raw_daily_sections=raw_daily_sections,
            mode=self.daily_sections_mode,
        )

    @classmethod
    def _parse_memory_sections(cls, text: str) -> tuple[list[str], list[tuple[str, list[str]]]]:
        preamble: list[str] = []
        section_map: dict[str, list[str]] = {}
        section_order: list[str] = []
        current_heading: str | None = None
        for raw_line in text.splitlines():
            m = cls._H2_HEADING_RE.match(raw_line)
            if m:
                current_heading = m.group(1).strip()
                if current_heading not in section_map:
                    section_map[current_heading] = []
                    section_order.append(current_heading)
                continue
            if current_heading is None:
                preamble.append(raw_line)
            else:
                section_map[current_heading].append(raw_line)
        sections = [(heading, section_map[heading]) for heading in section_order]
        return preamble, sections

    @classmethod
    def _render_memory_sections(
        cls,
        preamble: list[str],
        sections: list[tuple[str, list[str]]],
    ) -> str:
        parts: list[str] = []
        preamble_text = "\n".join(preamble).strip("\n")
        if preamble_text:
            parts.append(preamble_text)
        for heading, lines in sections:
            body = "\n".join(lines).strip("\n")
            if body:
                parts.append(f"## {heading}\n{body}")
            else:
                parts.append(f"## {heading}")
        rendered = "\n\n".join(parts).rstrip()
        return rendered + ("\n" if rendered else "")

    @staticmethod
    def _normalize_bullet_line(line: str) -> str:
        if not line.startswith("- "):
            return ""
        return re.sub(r"\s+", " ", line[2:].strip()).lower()

    @classmethod
    def _merge_section_lines(cls, current: list[str], candidate: list[str]) -> list[str]:
        merged = list(current)
        seen_line_keys: set[str] = {line.strip() for line in current if line.strip()}
        seen_bullets: set[str] = set()
        for line in current:
            normalized = cls._normalize_bullet_line(line)
            if normalized:
                seen_bullets.add(normalized)

        for line in candidate:
            stripped = line.strip()
            if not stripped:
                continue
            normalized_bullet = cls._normalize_bullet_line(line)
            if normalized_bullet:
                if normalized_bullet in seen_bullets:
                    continue
                seen_bullets.add(normalized_bullet)
            elif stripped in seen_line_keys:
                continue
            seen_line_keys.add(stripped)
            merged.append(line)
        return merged

    @classmethod
    def _merge_memory_update_with_current(
        cls,
        current_memory: str,
        candidate_update: str,
    ) -> tuple[str, dict[str, object]]:
        if not current_memory.strip() or not candidate_update.strip():
            return candidate_update, {
                "applied": False,
                "reason": "empty_input",
                "added_sections": [],
                "merged_sections": [],
            }

        cur_preamble, cur_sections = cls._parse_memory_sections(current_memory)
        cand_preamble, cand_sections = cls._parse_memory_sections(candidate_update)
        if not cur_sections or not cand_sections:
            return candidate_update, {
                "applied": False,
                "reason": "unstructured",
                "added_sections": [],
                "merged_sections": [],
            }

        merged_map: dict[str, list[str]] = {heading: list(lines) for heading, lines in cur_sections}
        merged_order: list[str] = [heading for heading, _ in cur_sections]
        added_sections: list[str] = []
        merged_sections: list[str] = []

        for heading, cand_lines in cand_sections:
            if heading in merged_map:
                before = list(merged_map[heading])
                merged_map[heading] = cls._merge_section_lines(before, cand_lines)
                if merged_map[heading] != before:
                    merged_sections.append(heading)
            else:
                merged_map[heading] = list(cand_lines)
                merged_order.append(heading)
                added_sections.append(heading)

        out_preamble = cur_preamble if any(line.strip() for line in cur_preamble) else cand_preamble
        merged_sections_list = [(heading, merged_map[heading]) for heading in merged_order]
        merged_text = cls._render_memory_sections(out_preamble, merged_sections_list)
        if not merged_text:
            return candidate_update, {
                "applied": False,
                "reason": "render_empty",
                "added_sections": [],
                "merged_sections": [],
            }
        return merged_text, {
            "applied": True,
            "reason": "ok",
            "added_sections": added_sections,
            "merged_sections": merged_sections,
        }

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
            "Prefer including daily_sections with concise bullets for Topics/Decisions/Tool Activity/Open Questions "
            "whenever history_entry has meaningful content; only omit a section when there is truly no relevant bullet."
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
        return MemoryGuardPolicy.truncate_log_sample(text)

    @classmethod
    def _sanitize_memory_update_detailed(
        cls,
        update: str,
        current_memory: str,
    ) -> tuple[str, dict[str, object]]:
        """Remove obviously short-lived/topic-dump content and return classification stats."""
        return MemoryGuardPolicy.sanitize_memory_update_detailed(update, current_memory)

    @classmethod
    def _dedupe_markdown_bullets_by_section(cls, lines: list[str]) -> tuple[list[str], int, list[str]]:
        return MemoryGuardPolicy.dedupe_markdown_bullets_by_section(lines)

    @classmethod
    def _sanitize_memory_update(cls, update: str, current_memory: str) -> tuple[str, list[str]]:
        """Backward-compatible wrapper returning removed section headings only."""
        return MemoryGuardPolicy.sanitize_memory_update(update, current_memory)

    @classmethod
    def _extract_h2_headings(cls, text: str) -> list[str]:
        return MemoryGuardPolicy.extract_h2_headings(text)

    @classmethod
    def _has_structured_markers(cls, text: str) -> bool:
        return MemoryGuardPolicy.has_structured_markers(text)

    @classmethod
    def _memory_update_guard_reason(cls, current_memory: str, candidate_update: str) -> str | None:
        """Return reason string when memory_update looks suspicious and should be skipped."""
        return MemoryGuardPolicy.memory_update_guard_reason(current_memory, candidate_update)

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
        scope = self._build_recovery_scope(session=session, archive_all=archive_all)
        if scope is None:
            scope = self._build_consolidation_scope(
                session=session,
                archive_all=archive_all,
                memory_window=memory_window,
            )
        if scope is None:
            self._clear_consolidation_progress()
            return True

        progress = self._ConsolidationProgress(
            session_key=session.key,
            start_index=scope.start_index,
            target_last=scope.target_last,
            archive_all=scope.archive_all,
            keep_count=scope.keep_count,
            snapshot_len=scope.snapshot_len,
            processed_count=0,
        )
        self._write_consolidation_progress(progress=progress)

        pending = list(scope.old_messages)
        processed_count = 0

        try:
            while pending:
                current_memory = self.read_long_term()
                chunk = self._fit_chunk_by_soft_budget(pending, current_memory)
                if not chunk:
                    break

                # Retry with smaller prefixes if provider still reports context overflow.
                while True:
                    chunk_result = await self._process_chunk(
                        session=session,
                        provider=provider,
                        model=model,
                        chunk=chunk,
                        current_memory=current_memory,
                    )
                    if chunk_result.status == "retry_smaller":
                        chunk = chunk_result.next_chunk or chunk[: max(1, len(chunk) // 2)]
                        continue
                    if chunk_result.status == "fatal":
                        return False

                    processed_count += chunk_result.processed_count
                    progress.processed_count = processed_count
                    self._write_consolidation_progress(progress=progress)
                    pending = pending[chunk_result.processed_count:]
                    if not scope.archive_all:
                        # Process at most one chunk per normal pass to keep latency bounded.
                        self._update_last_consolidated(
                            session=session,
                            target_last=scope.target_last,
                            start_index=scope.start_index,
                            processed_count=processed_count,
                        )
                        self._log_consolidation_done(
                            snapshot_len=scope.snapshot_len,
                            last_consolidated=session.last_consolidated,
                            processed_messages=processed_count,
                            partial=(session.last_consolidated < scope.target_last),
                        )
                        self._clear_consolidation_progress()
                        return True
                    break

            if scope.archive_all:
                session.last_consolidated = 0
            else:
                self._update_last_consolidated(
                    session=session,
                    target_last=scope.target_last,
                    start_index=scope.start_index,
                    processed_count=processed_count,
                )
            self._log_consolidation_done(
                snapshot_len=scope.snapshot_len,
                last_consolidated=session.last_consolidated,
                processed_messages=processed_count,
                partial=(not scope.archive_all and session.last_consolidated < scope.target_last),
            )
            self._clear_consolidation_progress()
            return True
        except Exception:
            logger.exception("Memory consolidation failed")
            return False

    def _build_consolidation_scope(
        self,
        *,
        session: Session,
        archive_all: bool,
        memory_window: int,
    ) -> _ConsolidationScope | None:
        # Snapshot length before the LLM call so concurrent appends don't shift boundaries.
        snapshot_len = len(session.messages)
        if archive_all:
            old_messages = session.messages[:snapshot_len]
            logger.info("Memory consolidation (archive_all)", message_count=snapshot_len)
            return self._ConsolidationScope(
                snapshot_len=snapshot_len,
                keep_count=0,
                old_messages=list(old_messages),
                start_index=session.last_consolidated,
                target_last=0,
                archive_all=True,
            )

        keep_count = memory_window // 2
        if snapshot_len <= keep_count:
            return None
        if snapshot_len - session.last_consolidated <= 0:
            return None
        old_messages = session.messages[session.last_consolidated:snapshot_len - keep_count]
        if not old_messages:
            return None
        logger.info("Memory consolidation", to_consolidate=len(old_messages), keep=keep_count)
        return self._ConsolidationScope(
            snapshot_len=snapshot_len,
            keep_count=keep_count,
            old_messages=list(old_messages),
            start_index=session.last_consolidated,
            target_last=snapshot_len - keep_count,
            archive_all=False,
        )

    @staticmethod
    def _update_last_consolidated(
        *,
        session: Session,
        target_last: int,
        start_index: int,
        processed_count: int,
    ) -> None:
        session.last_consolidated = min(target_last, start_index + processed_count)

    @staticmethod
    def _log_consolidation_done(
        *,
        snapshot_len: int,
        last_consolidated: int,
        processed_messages: int,
        partial: bool,
    ) -> None:
        logger.info(
            "Memory consolidation done",
            snapshot_len=snapshot_len,
            last_consolidated=last_consolidated,
            processed_messages=processed_messages,
            partial=partial,
        )

    async def _call_consolidation_llm(
        self,
        *,
        provider: LLMProvider,
        model: str,
        prompt: str,
    ) -> tuple[object, _ConsolidationCallMeta]:
        response = None
        preferred_retry_used = False
        has_daily_sections = False
        require_daily_sections = self.daily_sections_mode in {"preferred", "required"}
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

            has_daily_sections = self._tool_call_has_daily_sections(response)
            if response.has_tool_calls and (not require_daily_sections or has_daily_sections):
                break
            if getattr(response, "finish_reason", "") == "error":
                break
            if attempt < self._CONSOLIDATION_TOOLCALL_RETRIES:
                logger.warning(
                    "Memory consolidation response missing required tool payload, retrying",
                    retry=attempt + 1,
                    has_tool_calls=bool(response.has_tool_calls),
                    has_daily_sections=has_daily_sections,
                    daily_sections_mode=self.daily_sections_mode,
                )
                preferred_retry_used = True

        return response, self._ConsolidationCallMeta(
            preferred_retry_used=preferred_retry_used,
            tool_call_has_daily_sections=has_daily_sections,
        )

    @staticmethod
    def _tool_call_has_daily_sections(response) -> bool:
        if not getattr(response, "has_tool_calls", False):
            return False
        tool_calls = getattr(response, "tool_calls", None)
        if not tool_calls:
            return False
        args = tool_calls[0].arguments
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                return False
        if not isinstance(args, dict):
            return False
        return args.get("daily_sections") is not None

    @staticmethod
    def _extract_save_memory_args(response) -> dict | None:
        args = response.tool_calls[0].arguments
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except Exception:
                logger.warning("Memory consolidation: failed to parse tool call arguments JSON")
                return None
        if not isinstance(args, dict):
            logger.warning(
                "Memory consolidation: unexpected arguments type",
                arguments_type=type(args).__name__,
            )
            return None
        return args

    def _apply_save_memory_tool_call(
        self,
        *,
        session: Session,
        args: dict,
        current_memory: str,
        memory_truncated: bool,
        call_meta: _ConsolidationCallMeta | None = None,
    ) -> None:
        self._pipeline.run(
            session=session,
            args=args,
            current_memory=current_memory,
            memory_truncated=memory_truncated,
            call_meta=call_meta,
        )

    async def _process_chunk(
        self,
        *,
        session: Session,
        provider: LLMProvider,
        model: str,
        chunk: list[dict],
        current_memory: str,
    ) -> _ChunkProcessResult:
        prompt_data = self._build_chunk_prompt(
            chunk=chunk,
            current_memory=current_memory,
        )
        if prompt_data is None:
            # Nothing useful to summarize; just mark the messages as processed.
            return self._ChunkProcessResult(status="processed", processed_count=len(chunk))
        response, call_meta = await self._call_consolidation_llm(
            provider=provider,
            model=model,
            prompt=prompt_data.prompt,
        )
        assert response is not None
        return self._handle_chunk_response(
            session=session,
            response=response,
            chunk=chunk,
            current_memory=current_memory,
            memory_truncated=prompt_data.memory_truncated,
            call_meta=call_meta,
        )

    def _build_chunk_prompt(
        self,
        *,
        chunk: list[dict],
        current_memory: str,
    ) -> _ChunkPromptData | None:
        lines = self._format_consolidation_lines(chunk)
        if not lines:
            return None
        prompt_memory, memory_truncated = self._fit_memory_context_by_soft_budget(current_memory, lines)
        if memory_truncated:
            logger.warning(
                "Memory consolidation prompt truncating long-term memory context",
                memory_chars=len(current_memory),
                prompt_memory_chars=len(prompt_memory),
                chunk_messages=len(chunk),
            )
        return self._ChunkPromptData(
            prompt=self._build_consolidation_prompt(prompt_memory, lines),
            memory_truncated=memory_truncated,
        )

    def _handle_chunk_response(
        self,
        *,
        session: Session,
        response,
        chunk: list[dict],
        current_memory: str,
        memory_truncated: bool,
        call_meta: _ConsolidationCallMeta | None = None,
    ) -> _ChunkProcessResult:
        if getattr(response, "finish_reason", "") == "error" and self._is_context_length_error(response.content):
            if len(chunk) <= 1:
                logger.warning("Memory consolidation failed: prompt exceeds context even for single message")
                return self._ChunkProcessResult(status="fatal")
            return self._ChunkProcessResult(
                status="retry_smaller",
                next_chunk=chunk[: max(1, len(chunk) // 2)],
            )

        if not response.has_tool_calls:
            if getattr(response, "finish_reason", "") == "error":
                logger.warning("Memory consolidation LLM call failed", error=response.content or "(empty)")
            else:
                logger.warning("Memory consolidation: LLM did not call save_memory, skipping")
            return self._ChunkProcessResult(status="fatal")

        args = self._extract_save_memory_args(response)
        if args is None:
            return self._ChunkProcessResult(status="fatal")
        self._apply_save_memory_tool_call(
            session=session,
            args=args,
            current_memory=current_memory,
            memory_truncated=memory_truncated,
            call_meta=call_meta,
        )
        return self._ChunkProcessResult(status="processed", processed_count=len(chunk))
