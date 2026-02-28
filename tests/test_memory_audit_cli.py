from datetime import datetime, timedelta
from pathlib import Path

from typer.testing import CliRunner

from nanobot.cli.commands import app


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_memory_audit_apply_safe_auto_enables_apply_and_uses_defaults(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "ws"
    memory_dir = workspace / "memory"
    memory_dir.mkdir(parents=True)
    _write(memory_dir / "MEMORY.md", "# Long-term Memory\n")
    _write(memory_dir / "HISTORY.md", "[2026-02-27 10:00] keep\n\n")
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

    class _Cfg:
        workspace_path = workspace

    import nanobot.cli.commands as commands_module

    monkeypatch.setattr(commands_module, "load_config", lambda: _Cfg(), raising=False)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "memory-audit",
            "--memory-dir",
            str(memory_dir),
            "--apply-safe",
            "--apply-recent-days",
            "100",
        ],
    )
    assert result.exit_code == 0
    assert "Apply safe preset:" in result.output
    assert "drop_tool_activity_older_than_days=30" in result.output
    assert "drop_non_decision_older_than_days=30" in result.output
    assert "abort_on_high_risk=True" in result.output

    daily = (memory_dir / f"{old_day}.md").read_text(encoding="utf-8")
    assert "keep decision" in daily
    assert "old topic" not in daily
    assert "old question" not in daily
    assert "old cmd" not in daily
