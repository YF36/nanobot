from pathlib import Path

from typer.testing import CliRunner

from nanobot.cli.commands import app


def test_memory_observe_generates_three_reports(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "ws"
    memory_dir = workspace / "memory"
    obs_dir = workspace / "improvement-notes" / "memory-observations"
    memory_dir.mkdir(parents=True)
    obs_dir.mkdir(parents=True)

    (memory_dir / "MEMORY.md").write_text("# Long-term Memory\n", encoding="utf-8")
    (memory_dir / "HISTORY.md").write_text("", encoding="utf-8")
    (memory_dir / "2026-02-27.md").write_text("# 2026-02-27\n\n## Topics\n\n- hello\n", encoding="utf-8")

    class _Cfg:
        workspace_path = workspace

    import nanobot.cli.commands as commands_module

    monkeypatch.setattr(commands_module, "load_config", lambda: _Cfg(), raising=False)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "memory-observe",
            "--memory-dir",
            str(memory_dir),
            "--out-dir",
            str(obs_dir),
            "--tag",
            "smoke",
        ],
    )
    assert result.exit_code == 0
    files = sorted(p.name for p in obs_dir.glob("*-smoke.md"))
    assert len(files) == 10
    assert any(name.endswith("audit-smoke.md") for name in files)
    assert any(name.endswith("metrics-summary-smoke.md") for name in files)
    assert any(name.endswith("guard-metrics-summary-smoke.md") for name in files)
    assert any(name.endswith("sanitize-metrics-summary-smoke.md") for name in files)
    assert any(name.endswith("conflict-metrics-summary-smoke.md") for name in files)
    assert any(name.endswith("context-trace-summary-smoke.md") for name in files)
    assert any(name.endswith("cleanup-stage-summary-smoke.md") for name in files)
    assert any(name.endswith("cleanup-conversion-summary-smoke.md") for name in files)
    assert any(name.endswith("cleanup-drop-preview-summary-smoke.md") for name in files)
    assert any(name.endswith("observability-dashboard-smoke.md") for name in files)
