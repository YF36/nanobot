"""Tests for mtime-based skill caching (5.3).

Covers:
1. Skill content is cached after first read
2. Cache is invalidated when file mtime changes
3. Missing skill returns None
"""

import time
from pathlib import Path

import pytest

from nanobot.agent.skills import SkillsLoader


def _make_skill(skills_dir: Path, name: str, content: str) -> Path:
    skill_dir = skills_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(content, encoding="utf-8")
    return skill_file


class TestSkillCache:
    def test_first_read_returns_content(self, tmp_path: Path) -> None:
        """load_skill returns correct content on first call."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        _make_skill(workspace / "skills", "my_skill", "# My Skill\nDo stuff.")

        loader = SkillsLoader(workspace=workspace)
        result = loader.load_skill("my_skill")

        assert result == "# My Skill\nDo stuff."

    def test_second_read_uses_cache(self, tmp_path: Path) -> None:
        """load_skill returns cached content without re-reading disk."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        skill_file = _make_skill(workspace / "skills", "cached_skill", "v1")

        loader = SkillsLoader(workspace=workspace)
        loader.load_skill("cached_skill")

        # Overwrite file content but keep same mtime
        mtime = skill_file.stat().st_mtime
        skill_file.write_text("v2", encoding="utf-8")
        import os
        os.utime(skill_file, (mtime, mtime))

        result = loader.load_skill("cached_skill")
        assert result == "v1", "Cache should have returned v1, not v2"

    def test_cache_invalidated_on_mtime_change(self, tmp_path: Path) -> None:
        """load_skill re-reads file when mtime changes."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        skill_file = _make_skill(workspace / "skills", "updated_skill", "v1")

        loader = SkillsLoader(workspace=workspace)
        loader.load_skill("updated_skill")

        # Update file with a newer mtime
        time.sleep(0.01)
        skill_file.write_text("v2", encoding="utf-8")

        result = loader.load_skill("updated_skill")
        assert result == "v2", "Should have re-read file after mtime change"

    def test_missing_skill_returns_none(self, tmp_path: Path) -> None:
        """load_skill returns None for non-existent skill."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        loader = SkillsLoader(workspace=workspace)
        result = loader.load_skill("nonexistent")

        assert result is None

    def test_builtin_skill_cached(self, tmp_path: Path) -> None:
        """Builtin skills are also cached by mtime."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        builtin = tmp_path / "builtin"
        skill_file = _make_skill(builtin, "builtin_skill", "builtin content")

        loader = SkillsLoader(workspace=workspace, builtin_skills_dir=builtin)
        result = loader.load_skill("builtin_skill")

        assert result == "builtin content"
        assert str(skill_file) in loader._cache
