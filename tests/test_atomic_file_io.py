from pathlib import Path

from nanobot.utils.helpers import atomic_append_text, atomic_write_text


def test_atomic_write_text_creates_and_overwrites_file(tmp_path: Path) -> None:
    path = tmp_path / "sample.txt"
    atomic_write_text(path, "v1\n", encoding="utf-8")
    assert path.read_text(encoding="utf-8") == "v1\n"

    atomic_write_text(path, "v2\n", encoding="utf-8")
    assert path.read_text(encoding="utf-8") == "v2\n"


def test_atomic_append_text_appends_content(tmp_path: Path) -> None:
    path = tmp_path / "append.txt"
    atomic_append_text(path, "line1\n", encoding="utf-8")
    atomic_append_text(path, "line2\n", encoding="utf-8")
    assert path.read_text(encoding="utf-8") == "line1\nline2\n"

