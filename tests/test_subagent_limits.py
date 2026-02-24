"""Tests for subagent resource limits (1.7).

Covers:
1. max_concurrent_subagents blocks new spawns when limit reached
2. subagent_max_iterations is used instead of hardcoded 15
3. subagent_timeout cancels long-running subagents
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.subagent import SubagentManager
from nanobot.bus.queue import MessageBus


def _make_manager(tmp_path: Path, **kwargs) -> SubagentManager:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    bus = MessageBus()
    return SubagentManager(
        provider=provider,
        workspace=tmp_path,
        bus=bus,
        **kwargs,
    )


class TestMaxConcurrentSubagents:
    @pytest.mark.asyncio
    async def test_spawn_blocked_when_limit_reached(self, tmp_path: Path) -> None:
        """spawn() returns error message when concurrent limit is reached."""
        manager = _make_manager(tmp_path, max_concurrent_subagents=2)

        # Inject fake running tasks to simulate limit reached
        manager._running_tasks["task1"] = MagicMock()
        manager._running_tasks["task2"] = MagicMock()

        result = await manager.spawn("do something", origin_channel="cli", origin_chat_id="test")

        assert "limit" in result.lower() or "cannot spawn" in result.lower()
        assert len(manager._running_tasks) == 2  # no new task added

    @pytest.mark.asyncio
    async def test_spawn_allowed_below_limit(self, tmp_path: Path) -> None:
        """spawn() succeeds when under the concurrent limit."""
        manager = _make_manager(tmp_path, max_concurrent_subagents=3)
        manager._running_tasks["task1"] = MagicMock()

        # Patch _run_subagent to avoid real execution
        async def _noop(*args, **kwargs):
            await asyncio.sleep(0)

        manager._run_subagent = _noop

        result = await manager.spawn("do something", origin_channel="cli", origin_chat_id="test")

        assert "started" in result.lower()


class TestSubagentMaxIterations:
    def test_default_max_iterations(self, tmp_path: Path) -> None:
        """subagent_max_iterations defaults to 15."""
        manager = _make_manager(tmp_path)
        assert manager.subagent_max_iterations == 15

    def test_custom_max_iterations(self, tmp_path: Path) -> None:
        """subagent_max_iterations can be overridden."""
        manager = _make_manager(tmp_path, subagent_max_iterations=5)
        assert manager.subagent_max_iterations == 5


class TestSubagentTimeout:
    def test_default_timeout(self, tmp_path: Path) -> None:
        """subagent_timeout defaults to 300 seconds."""
        manager = _make_manager(tmp_path)
        assert manager.subagent_timeout == 300.0

    @pytest.mark.asyncio
    async def test_timeout_cancels_subagent(self, tmp_path: Path) -> None:
        """A subagent that exceeds timeout is cancelled."""
        manager = _make_manager(tmp_path, subagent_timeout=0.05)

        cancelled = asyncio.Event()

        async def _slow_run(*args, **kwargs):
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        manager._run_subagent = _slow_run

        await manager.spawn("slow task", origin_channel="cli", origin_chat_id="test")

        # Wait for timeout to fire
        await asyncio.sleep(0.2)

        assert cancelled.is_set(), "Subagent was not cancelled after timeout"
        assert manager.get_running_count() == 0
