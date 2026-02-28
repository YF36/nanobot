"""Consolidation pipeline abstraction (behavior-preserving)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from nanobot.logging import get_logger

if TYPE_CHECKING:
    from nanobot.memory.store import MemoryStore
    from nanobot.session.manager import Session

logger = get_logger(__name__)


@dataclass
class PipelineContext:
    session: Session
    args: dict[str, Any]
    current_memory: str
    memory_truncated: bool
    call_meta: Any | None
    entry_text: str | None = None
    update: str | None = None


class ConsolidationPipeline:
    """Small step runner to reduce monolithic apply-save-memory logic."""

    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    def run(
        self,
        *,
        session: Session,
        args: dict[str, Any],
        current_memory: str,
        memory_truncated: bool,
        call_meta: Any | None = None,
    ) -> None:
        ctx = PipelineContext(
            session=session,
            args=args,
            current_memory=current_memory,
            memory_truncated=memory_truncated,
            call_meta=call_meta,
        )
        self._step_history_and_daily(ctx)
        self._step_memory_update(ctx)

    def _step_history_and_daily(self, ctx: PipelineContext) -> None:
        entry = ctx.args.get("history_entry")
        entry_text, entry_reason = self.store._normalize_history_entry(entry)
        if entry_text is None:
            logger.warning(
                "Memory consolidation skipped history_entry due to quality gate",
                reason=entry_reason,
            )
            return

        self.store.append_history(entry_text)
        ctx.entry_text = entry_text
        date_str = self.store._history_entry_date(entry_text)
        raw_daily_sections = ctx.args.get("daily_sections")
        routing_plan = self.store._resolve_daily_routing_plan(
            entry_text=entry_text,
            raw_daily_sections=raw_daily_sections,
        )
        _, structured_daily_ok, structured_daily_details = self.store.append_daily_sections_detailed(
            date_str,
            routing_plan.sections_payload,
        )
        if not structured_daily_ok:
            if self.store.daily_sections_mode == "required":
                logger.warning(
                    "Memory daily structured write required; skipping unstructured fallback",
                    date=date_str,
                    reason=structured_daily_details["reason"],
                )
            else:
                routing_plan.structured_source = "fallback_unstructured"
                self.store.append_daily_history_entry(entry_text)

        logger.debug(
            "Memory daily routing decision",
            date=date_str,
            structured_daily_ok=structured_daily_ok,
            structured_source=routing_plan.structured_source,
            fallback_used=(not structured_daily_ok),
            fallback_reason=structured_daily_details["reason"],
            structured_keys=structured_daily_details["keys"],
            structured_bullet_count=structured_daily_details["bullet_count"],
        )
        self.store._append_daily_routing_metric(
            session_key=ctx.session.key,
            date_str=date_str,
            structured_daily_ok=structured_daily_ok,
            fallback_reason=str(structured_daily_details["reason"]),
            structured_keys=list(structured_daily_details["keys"]),
            structured_bullet_count=int(structured_daily_details["bullet_count"]),
            structured_source=routing_plan.structured_source,
            model_daily_sections_ok=routing_plan.model_daily_sections_ok,
            model_daily_sections_reason=routing_plan.model_daily_sections_reason,
            preferred_retry_used=bool(ctx.call_meta and ctx.call_meta.preferred_retry_used),
            tool_call_has_daily_sections=bool(ctx.call_meta and ctx.call_meta.tool_call_has_daily_sections),
        )

    def _step_memory_update(self, ctx: PipelineContext) -> None:
        update = ctx.args.get("memory_update")
        if not update:
            return
        if not isinstance(update, str):
            update = json.dumps(update, ensure_ascii=False)
        update, sanitize_details = self.store._sanitize_memory_update_detailed(update, ctx.current_memory)
        sanitize_changes = int(
            len(list(sanitize_details["removed_sections"]))
            + int(sanitize_details["removed_transient_status_line_count"])
            + int(sanitize_details["removed_duplicate_bullet_count"])
        )

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
            self.store._append_memory_update_sanitize_metric(
                session_key=ctx.session.key,
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

        if ctx.memory_truncated:
            logger.warning(
                "Skipping memory_update write because long-term memory context was truncated",
                current_memory_chars=len(ctx.current_memory),
                returned_memory_chars=len(update),
            )
            self.store._append_memory_update_outcome_metric(
                session_key=ctx.session.key,
                outcome="truncated_skip",
                sanitize_changes=sanitize_changes,
                merge_applied=False,
                conflict_count=0,
            )
            return

        update, merge_details = self.store._merge_memory_update_with_current(ctx.current_memory, update)
        merge_applied = bool(merge_details.get("applied", False))
        if merge_details.get("applied"):
            logger.debug(
                "Memory section merge applied before guard",
                merged_sections=merge_details.get("merged_sections", []),
                added_sections=merge_details.get("added_sections", []),
            )

        if update == ctx.current_memory:
            self.store._append_memory_update_outcome_metric(
                session_key=ctx.session.key,
                outcome="no_change",
                sanitize_changes=sanitize_changes,
                merge_applied=merge_applied,
                conflict_count=0,
            )
            return
        guard_reason = self.store._memory_update_guard_reason(ctx.current_memory, update)
        if guard_reason:
            logger.warning(
                "Skipping memory_update write due to guard",
                reason=guard_reason,
                current_memory_chars=len(ctx.current_memory),
                returned_memory_chars=len(update),
            )
            self.store._append_memory_update_guard_metric(
                session_key=ctx.session.key,
                reason=guard_reason,
                current_memory_chars=len(ctx.current_memory),
                returned_memory_chars=len(update),
                candidate_preview=self.store._truncate_log_sample(update),
            )
            self.store._append_memory_update_outcome_metric(
                session_key=ctx.session.key,
                outcome="guard_rejected",
                guard_reason=guard_reason,
                sanitize_changes=sanitize_changes,
                merge_applied=merge_applied,
                conflict_count=0,
            )
            return

        conflicts = self.store._detect_preference_conflicts(ctx.current_memory, update)
        resolution = getattr(self.store, "preference_conflict_strategy", "keep_new")
        for conflict in conflicts:
            logger.warning(
                "Memory preference conflict detected",
                key=conflict["conflict_key"],
                old_value=conflict["old_value"],
                new_value=conflict["new_value"],
                resolution=resolution,
            )
            self.store._append_memory_conflict_metric(
                session_key=ctx.session.key,
                conflict_key=conflict["conflict_key"],
                old_value=conflict["old_value"],
                new_value=conflict["new_value"],
                resolution=resolution,
            )
        if conflicts and resolution in {"keep_old", "ask_user"}:
            self.store._append_memory_update_outcome_metric(
                session_key=ctx.session.key,
                outcome="guard_rejected",
                guard_reason=f"preference_conflict_{resolution}",
                sanitize_changes=sanitize_changes,
                merge_applied=merge_applied,
                conflict_count=len(conflicts),
            )
            return
        self.store.write_long_term(update)
        self.store._append_memory_update_outcome_metric(
            session_key=ctx.session.key,
            outcome=("sanitize_modified" if sanitize_changes > 0 else "written"),
            sanitize_changes=sanitize_changes,
            merge_applied=merge_applied,
            conflict_count=len(conflicts),
        )
