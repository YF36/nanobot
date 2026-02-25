from pathlib import Path

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

