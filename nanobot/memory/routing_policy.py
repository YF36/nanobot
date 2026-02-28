"""Daily routing policy for consolidation outputs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass
class DailyRoutingPlan:
    sections_payload: object
    structured_source: str
    model_daily_sections_ok: bool
    model_daily_sections_reason: str


class DailyRoutingPolicy:
    """Resolve best daily_sections payload under mode + data quality constraints."""

    def __init__(
        self,
        *,
        normalize_daily_sections_detailed: Callable[[object], tuple[dict[str, list[str]] | None, str]],
        coerce_partial_daily_sections: Callable[[object], dict[str, list[str]] | None],
        synthesize_daily_sections_from_entry: Callable[[str], dict[str, list[str]] | None],
    ) -> None:
        self._normalize_daily_sections_detailed = normalize_daily_sections_detailed
        self._coerce_partial_daily_sections = coerce_partial_daily_sections
        self._synthesize_daily_sections_from_entry = synthesize_daily_sections_from_entry

    def resolve(
        self,
        *,
        entry_text: str,
        raw_daily_sections: object,
        mode: str,
    ) -> DailyRoutingPlan:
        _, model_daily_sections_reason = self._normalize_daily_sections_detailed(raw_daily_sections)
        model_daily_sections_ok = model_daily_sections_reason == "ok"
        if model_daily_sections_ok:
            return DailyRoutingPlan(
                sections_payload=raw_daily_sections,
                structured_source="model",
                model_daily_sections_ok=True,
                model_daily_sections_reason="ok",
            )

        salvaged_sections = self._coerce_partial_daily_sections(raw_daily_sections)
        if salvaged_sections is not None and salvaged_sections != raw_daily_sections:
            return DailyRoutingPlan(
                sections_payload=salvaged_sections,
                structured_source="salvaged_model_partial",
                model_daily_sections_ok=False,
                model_daily_sections_reason=model_daily_sections_reason,
            )

        synthesized_sections = self._synthesize_daily_sections_from_entry(entry_text)
        if synthesized_sections is not None:
            synthesized_source = (
                "synthesized_missing" if raw_daily_sections is None else "synthesized_after_invalid"
            )
            return DailyRoutingPlan(
                sections_payload=synthesized_sections,
                structured_source=synthesized_source,
                model_daily_sections_ok=False,
                model_daily_sections_reason=model_daily_sections_reason,
            )

        if mode == "required":
            return DailyRoutingPlan(
                sections_payload=None,
                structured_source="required_missing",
                model_daily_sections_ok=False,
                model_daily_sections_reason=model_daily_sections_reason,
            )
        return DailyRoutingPlan(
            sections_payload=raw_daily_sections,
            structured_source="fallback_unstructured",
            model_daily_sections_ok=False,
            model_daily_sections_reason=model_daily_sections_reason,
        )

