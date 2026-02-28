"""Memory sanitize/guard/conflict policy extracted from MemoryStore."""

from __future__ import annotations

import re
from collections import Counter


class MemoryGuardPolicy:
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
    _PREFERENCE_KEY_PATTERNS = (
        ("language", re.compile(r"\b(language|语言)\b", re.IGNORECASE)),
        ("communication_style", re.compile(r"\b(communication style|沟通风格)\b", re.IGNORECASE)),
    )

    @classmethod
    def truncate_log_sample(cls, text: str) -> str:
        text = " ".join(text.split())
        if len(text) <= cls._MEMORY_SANITIZE_LOG_SAMPLE_CHARS:
            return text
        return text[: cls._MEMORY_SANITIZE_LOG_SAMPLE_CHARS - 3].rstrip() + "..."

    @classmethod
    def sanitize_memory_update_detailed(
        cls,
        update: str,
        current_memory: str,
    ) -> tuple[str, dict[str, object]]:
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
                        recent_topic_section_samples.append(cls.truncate_log_sample(heading))
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
                                transient_status_line_samples.append(cls.truncate_log_sample(candidate))
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

        deduped_kept, removed_duplicate_bullet_count, duplicate_bullet_section_samples = cls.dedupe_markdown_bullets_by_section(
            kept
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
    def dedupe_markdown_bullets_by_section(cls, lines: list[str]) -> tuple[list[str], int, list[str]]:
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
    def sanitize_memory_update(cls, update: str, current_memory: str) -> tuple[str, list[str]]:
        sanitized, details = cls.sanitize_memory_update_detailed(update, current_memory)
        return sanitized, list(details["removed_sections"])

    @classmethod
    def extract_h2_headings(cls, text: str) -> list[str]:
        headings: list[str] = []
        for line in text.splitlines():
            if line.startswith("## "):
                heading = line[3:].strip()
                if heading:
                    headings.append(heading)
        return headings

    @classmethod
    def has_structured_markers(cls, text: str) -> bool:
        for raw in text.splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith("## ") or line.startswith("- "):
                return True
        return False

    @classmethod
    def memory_update_guard_reason(cls, current_memory: str, candidate_update: str) -> str | None:
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
        if candidate_len >= cls._MEMORY_UPDATE_MIN_STRUCTURED_CHARS and not cls.has_structured_markers(candidate):
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
        current_h2 = cls.extract_h2_headings(current)
        if current_h2:
            candidate_h2 = set(cls.extract_h2_headings(candidate))
            kept = sum(1 for h in current_h2 if h in candidate_h2)
            keep_ratio = kept / len(current_h2)
            if keep_ratio < cls._MEMORY_UPDATE_MIN_HEADING_RETAIN_RATIO:
                return "heading_retention_too_low"
        return None

    @classmethod
    def extract_preference_values(cls, text: str) -> dict[str, str]:
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
    def detect_preference_conflicts(cls, current_memory: str, candidate_update: str) -> list[dict[str, str]]:
        current_vals = cls.extract_preference_values(current_memory)
        candidate_vals = cls.extract_preference_values(candidate_update)
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
