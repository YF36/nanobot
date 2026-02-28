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


def test_memory_audit_archive_apply_moves_old_daily_files(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "ws"
    memory_dir = workspace / "memory"
    memory_dir.mkdir(parents=True)
    _write(memory_dir / "MEMORY.md", "# Long-term Memory\n")
    _write(memory_dir / "HISTORY.md", "")
    _write(memory_dir / "2020-01-01.md", "# 2020-01-01\n\n## Topics\n\n- old\n")

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
            "--archive-apply",
            "--archive-keep-days",
            "30",
        ],
    )
    assert result.exit_code == 0
    assert "Applied daily archive:" in result.output
    assert not (memory_dir / "2020-01-01.md").exists()
    assert (memory_dir / "archive" / "2020-01-01.md").exists()


def test_memory_audit_archive_compact_apply_writes_history_and_moves(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "ws"
    memory_dir = workspace / "memory"
    memory_dir.mkdir(parents=True)
    _write(memory_dir / "MEMORY.md", "# Long-term Memory\n")
    _write(memory_dir / "HISTORY.md", "")
    _write(
        memory_dir / "2020-01-01.md",
        (
            "# 2020-01-01\n\n"
            "## Topics\n\n- old topic\n\n"
            "## Decisions\n\n- old decision\n"
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
            "--archive-compact-apply",
            "--archive-keep-days",
            "30",
            "--archive-compact-max-bullets-per-file",
            "2",
        ],
    )
    assert result.exit_code == 0
    assert "Applied daily archive compact:" in result.output
    assert not (memory_dir / "2020-01-01.md").exists()
    assert (memory_dir / "archive" / "2020-01-01.md").exists()
    history_text = (memory_dir / "HISTORY.md").read_text(encoding="utf-8")
    assert "Archived daily summary" in history_text


def test_memory_audit_daily_ttl_apply_deletes_expired_archive_files(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "ws"
    memory_dir = workspace / "memory"
    archive_dir = memory_dir / "archive"
    archive_dir.mkdir(parents=True)
    _write(memory_dir / "MEMORY.md", "# Long-term Memory\n")
    _write(memory_dir / "HISTORY.md", "")
    _write(archive_dir / "2020-01-01.md", "# 2020-01-01\n\n## Topics\n\n- old\n")

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
            "--daily-ttl-apply",
            "--daily-ttl-days",
            "30",
        ],
    )
    assert result.exit_code == 0
    assert "Applied daily TTL janitor:" in result.output
    assert not (archive_dir / "2020-01-01.md").exists()


def test_memory_audit_purge_rejected_sections_apply(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "ws"
    memory_dir = workspace / "memory"
    memory_dir.mkdir(parents=True)
    _write(
        memory_dir / "MEMORY.md",
        (
            "# Long-term Memory\n\n"
            "## Preferences\n- 中文沟通\n\n"
            "## External Reference Information\n- 某条目\n"
        ),
    )
    _write(memory_dir / "HISTORY.md", "")

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
            "--purge-rejected-sections",
            "--apply",
        ],
    )
    assert result.exit_code == 0
    assert "Rejected Memory Section Scan" in result.output
    assert "Removed sections" in result.output
    text = (memory_dir / "MEMORY.md").read_text(encoding="utf-8")
    assert "External Reference Information" not in text


def test_memory_audit_insights_ttl_apply_deletes_expired_lines(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "ws"
    memory_dir = workspace / "memory"
    memory_dir.mkdir(parents=True)
    _write(memory_dir / "MEMORY.md", "# Long-term Memory\n")
    _write(memory_dir / "HISTORY.md", "")
    _write(
        memory_dir / "INSIGHTS.md",
        (
            "# Insights\n\n"
            "## Lessons Learned\n"
            "- [2020-01-01] stale insight\n"
            f"- [{datetime.now().strftime('%Y-%m-%d')}] recent insight\n"
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
            "--insights-ttl-apply",
            "--insights-ttl-days",
            "30",
        ],
    )
    assert result.exit_code == 0
    assert "Applied insights TTL janitor:" in result.output
    text = (memory_dir / "INSIGHTS.md").read_text(encoding="utf-8")
    assert "stale insight" not in text
    assert "recent insight" in text
