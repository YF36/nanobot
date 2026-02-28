"""Tests for insights context injection token budget."""

from pathlib import Path

from nanobot.memory import MemoryStore


def _make_insights(n: int, section: str = "Lessons Learned") -> str:
    """Generate an INSIGHTS.md with *n* dated bullets across a date range."""
    lines = [f"## {section}"]
    for i in range(n):
        day = f"2026-01-{(i + 1):02d}"
        lines.append(f"- [{day}] Insight number {i + 1} about something important.")
    return "\n".join(lines) + "\n"


def _make_multi_section_insights(n_per_section: int) -> str:
    """Generate insights with two sections."""
    parts: list[str] = []
    for section in ("Lessons Learned", "Workflow Patterns"):
        lines = [f"## {section}"]
        for i in range(n_per_section):
            day = f"2026-01-{(i + 1):02d}"
            lines.append(f"- [{day}] {section} insight {i + 1}.")
        parts.append("\n".join(lines))
    return "\n\n".join(parts) + "\n"


def test_truncates_bullet_count(tmp_path: Path) -> None:
    mm = MemoryStore(workspace=tmp_path)
    mm.insights_file.parent.mkdir(parents=True, exist_ok=True)
    mm.insights_file.write_text(_make_insights(25), encoding="utf-8")

    ctx = mm.get_memory_context()
    bullet_lines = [l for l in ctx.splitlines() if l.startswith("- [")]
    assert len(bullet_lines) <= mm._INSIGHTS_CONTEXT_MAX_BULLETS
    assert "(... truncated)" in ctx


def test_truncates_char_budget(tmp_path: Path) -> None:
    mm = MemoryStore(workspace=tmp_path)
    mm.insights_file.parent.mkdir(parents=True, exist_ok=True)
    # Use long bullets to hit char limit before bullet limit.
    lines = ["## Lessons Learned"]
    for i in range(10):
        day = f"2026-02-{(i + 1):02d}"
        lines.append(f"- [{day}] {'x' * 200} end.")
    mm.insights_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    ctx = mm.get_memory_context()
    # Extract just the insights portion.
    idx = ctx.index("## L1 Insights")
    insights_part = ctx[idx + len("## L1 Insights\n"):]
    bullet_lines = [l for l in insights_part.splitlines() if l.startswith("- [")]
    total_chars = sum(len(l) + 1 for l in bullet_lines)
    assert total_chars <= mm._INSIGHTS_CONTEXT_MAX_CHARS + 1
    assert "(... truncated)" in ctx


def test_keeps_most_recent_bullets(tmp_path: Path) -> None:
    mm = MemoryStore(workspace=tmp_path)
    mm.insights_file.parent.mkdir(parents=True, exist_ok=True)
    mm.insights_file.write_text(_make_insights(25), encoding="utf-8")

    ctx = mm.get_memory_context()
    bullet_lines = [l for l in ctx.splitlines() if l.startswith("- [")]
    # Most recent date should be 2026-01-25 (the last generated).
    assert bullet_lines[0].startswith("- [2026-01-25]")
    # Oldest kept should be 2026-01-11 (25 - 15 + 1).
    assert bullet_lines[-1].startswith("- [2026-01-11]")


def test_multi_section_respects_budget(tmp_path: Path) -> None:
    mm = MemoryStore(workspace=tmp_path)
    mm.insights_file.parent.mkdir(parents=True, exist_ok=True)
    mm.insights_file.write_text(
        _make_multi_section_insights(12), encoding="utf-8"
    )

    ctx = mm.get_memory_context()
    bullet_lines = [l for l in ctx.splitlines() if l.startswith("- [")]
    assert len(bullet_lines) <= mm._INSIGHTS_CONTEXT_MAX_BULLETS


def test_no_truncation_when_within_budget(tmp_path: Path) -> None:
    mm = MemoryStore(workspace=tmp_path)
    mm.insights_file.parent.mkdir(parents=True, exist_ok=True)
    mm.insights_file.write_text(_make_insights(3), encoding="utf-8")

    ctx = mm.get_memory_context()
    assert "(... truncated)" not in ctx
    bullet_lines = [l for l in ctx.splitlines() if l.startswith("- [")]
    assert len(bullet_lines) == 3


def test_empty_insights_returns_no_section(tmp_path: Path) -> None:
    mm = MemoryStore(workspace=tmp_path)
    ctx = mm.get_memory_context()
    assert "L1 Insights" not in ctx


def test_undated_bullets_are_retained_in_context(tmp_path: Path) -> None:
    mm = MemoryStore(workspace=tmp_path)
    mm.insights_file.parent.mkdir(parents=True, exist_ok=True)
    mm.insights_file.write_text(
        (
            "## Lessons Learned\n"
            "- Keep responses concise and structured.\n"
            "- Prefer deterministic tool retry policy.\n"
        ),
        encoding="utf-8",
    )

    ctx = mm.get_memory_context()
    assert "Keep responses concise and structured." in ctx
    assert "Prefer deterministic tool retry policy." in ctx
