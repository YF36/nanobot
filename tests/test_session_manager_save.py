from pathlib import Path

import nanobot.session.manager as session_manager_module
from nanobot.session.manager import SessionManager


def test_save_skips_unchanged_session(monkeypatch, tmp_path: Path) -> None:
    manager = SessionManager(Path(tmp_path))
    session = manager.get_or_create("cli:test")
    session.add_message("user", "hello")

    writes: list[int] = []
    original_write = manager._write_session_file

    def _counting_write(path, s):
        writes.append(len(s.messages))
        return original_write(path, s)

    monkeypatch.setattr(manager, "_write_session_file", _counting_write)

    manager.save(session)
    manager.save(session)

    assert writes == [1]


def test_save_writes_when_last_consolidated_changes(monkeypatch, tmp_path: Path) -> None:
    manager = SessionManager(Path(tmp_path))
    session = manager.get_or_create("cli:test")
    session.add_message("user", "hello")
    manager.save(session)

    writes: list[int] = []
    original_write = manager._write_session_file

    def _counting_write(path, s):
        writes.append(s.last_consolidated)
        return original_write(path, s)

    monkeypatch.setattr(manager, "_write_session_file", _counting_write)

    manager.save(session)  # no-op
    session.last_consolidated = 1
    manager.save(session)

    assert writes == [1]


def test_save_writes_atomically_and_reloads(tmp_path: Path) -> None:
    manager = SessionManager(Path(tmp_path))
    session = manager.get_or_create("cli:test")
    session.add_message("user", "hello")
    session.add_message("assistant", "world")
    session.last_consolidated = 1

    manager.save(session)

    path = manager._get_session_path("cli:test")
    assert path.exists()
    assert not any(p.name.startswith(f".{path.name}.tmp-") for p in path.parent.iterdir())

    manager.invalidate("cli:test")
    loaded = manager.get_or_create("cli:test")
    assert len(loaded.messages) == 2
    assert loaded.messages[0]["content"] == "hello"
    assert loaded.messages[1]["content"] == "world"
    assert loaded.last_consolidated == 1


def test_save_logs_periodic_summary(monkeypatch, tmp_path: Path) -> None:
    manager = SessionManager(Path(tmp_path))
    manager._save_summary_every = 2
    session = manager.get_or_create("cli:test")
    session.add_message("user", "hello")

    debug_calls: list[tuple[str, dict]] = []
    original_debug = session_manager_module.logger.debug

    def _capture_debug(event, **kwargs):
        debug_calls.append((event, kwargs))
        return original_debug(event, **kwargs)

    monkeypatch.setattr(session_manager_module.logger, "debug", _capture_debug)

    manager.save(session)   # write
    manager.save(session)   # skip -> should trigger summary

    summaries = [kwargs for event, kwargs in debug_calls if event == "session_save_summary"]
    assert len(summaries) == 1
    summary = summaries[0]
    assert summary["interval_ops"] == 2
    assert summary["interval_writes"] == 1
    assert summary["interval_skips"] == 1
