"""Tests for consolidation race condition fixes.

Covers:
1. snapshot_len accuracy during concurrent message appends
2. Lock dict batch cleanup when exceeding 100 entries
3. /new cancels in-flight consolidation tasks
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock
from pathlib import Path

import pytest

from nanobot.session.manager import Session


class TestSnapshotLen:
    """Verify consolidation uses snapshot_len, not live len(session.messages)."""

    @pytest.mark.asyncio
    async def test_last_consolidated_uses_snapshot_not_live_len(self, tmp_path: Path) -> None:
        """Messages appended during LLM call must not shift last_consolidated."""
        from nanobot.agent.memory import MemoryStore
        from nanobot.providers.base import LLMResponse, ToolCallRequest

        mm = MemoryStore(workspace=tmp_path)
        session = Session(key="test:snapshot")
        for i in range(60):
            session.add_message("user", f"msg{i}")

        snapshot_before = len(session.messages)  # 60
        memory_window = 50
        keep_count = memory_window // 2  # 25
        expected_last = snapshot_before - keep_count  # 35

        provider = MagicMock()

        async def _fake_chat(**kwargs):
            # Simulate messages arriving while LLM is thinking
            for j in range(10):
                session.add_message("user", f"concurrent_{j}")
            return LLMResponse(
                content="",
                tool_calls=[ToolCallRequest(
                    id="t1", name="save_memory",
                    arguments={"history_entry": "summary", "memory_update": "updated"},
                )],
            )

        provider.chat = _fake_chat

        result = await mm.consolidate(
            session=session, provider=provider, model="test", memory_window=memory_window
        )

        assert result is True
        # last_consolidated should be based on snapshot (60), not live (70)
        assert session.last_consolidated == expected_last, (
            f"Expected {expected_last}, got {session.last_consolidated}. "
            f"Live len is {len(session.messages)}, snapshot was {snapshot_before}"
        )

    @pytest.mark.asyncio
    async def test_archive_all_uses_snapshot_len(self, tmp_path: Path) -> None:
        """archive_all=True should snapshot messages before LLM call."""
        from nanobot.agent.memory import MemoryStore
        from nanobot.providers.base import LLMResponse, ToolCallRequest

        mm = MemoryStore(workspace=tmp_path)
        session = Session(key="test:archive_snap")
        for i in range(20):
            session.add_message("user", f"msg{i}")

        provider = MagicMock()

        async def _fake_chat(**kwargs):
            for j in range(5):
                session.add_message("user", f"extra_{j}")
            return LLMResponse(
                content="",
                tool_calls=[ToolCallRequest(
                    id="t1", name="save_memory",
                    arguments={"history_entry": "archived", "memory_update": "mem"},
                )],
            )

        provider.chat = _fake_chat

        result = await mm.consolidate(
            session=session, provider=provider, model="test",
            memory_window=50, archive_all=True,
        )

        assert result is True
        assert session.last_consolidated == 0


class TestLockBatchCleanup:
    """Verify _prune_consolidation_lock batch cleanup when dict > 100."""

    @pytest.mark.asyncio
    async def test_batch_cleanup_over_100(self, tmp_path: Path) -> None:
        """Lock dict entries exceeding 100 should be batch-purged."""
        from nanobot.agent.loop import AgentLoop
        from nanobot.bus.queue import MessageBus

        bus = MessageBus()
        provider = MagicMock()
        provider.get_default_model.return_value = "test-model"
        loop = AgentLoop(
            bus=bus, provider=provider, workspace=tmp_path,
            model="test-model", memory_window=10,
        )

        # Populate 110 unlocked entries
        for i in range(110):
            loop._consolidation_locks[f"session:{i}"] = asyncio.Lock()

        assert len(loop._consolidation_locks) == 110

        # Trigger prune on an arbitrary key
        dummy_lock = loop._consolidation_locks["session:0"]
        loop._prune_consolidation_lock("session:0", dummy_lock)

        # All unlocked entries should be purged
        assert len(loop._consolidation_locks) == 0

    @pytest.mark.asyncio
    async def test_batch_cleanup_preserves_locked(self, tmp_path: Path) -> None:
        """Locked entries must survive batch cleanup."""
        from nanobot.agent.loop import AgentLoop
        from nanobot.bus.queue import MessageBus

        bus = MessageBus()
        provider = MagicMock()
        provider.get_default_model.return_value = "test-model"
        loop = AgentLoop(
            bus=bus, provider=provider, workspace=tmp_path,
            model="test-model", memory_window=10,
        )

        for i in range(105):
            loop._consolidation_locks[f"session:{i}"] = asyncio.Lock()

        # Lock two entries
        await loop._consolidation_locks["session:50"].acquire()
        await loop._consolidation_locks["session:99"].acquire()

        dummy_lock = loop._consolidation_locks["session:0"]
        loop._prune_consolidation_lock("session:0", dummy_lock)

        assert "session:50" in loop._consolidation_locks
        assert "session:99" in loop._consolidation_locks
        assert len(loop._consolidation_locks) == 2

        # Release for cleanup
        loop._consolidation_locks["session:50"].release()
        loop._consolidation_locks["session:99"].release()


class TestNewCancelsConsolidation:
    """/new should cancel in-flight consolidation instead of blocking."""

    @pytest.mark.asyncio
    async def test_new_cancels_inflight_task(self, tmp_path: Path) -> None:
        """/new cancels a running consolidation task for the same session."""
        from nanobot.agent.loop import AgentLoop
        from nanobot.bus.events import InboundMessage
        from nanobot.bus.queue import MessageBus
        from nanobot.providers.base import LLMResponse

        bus = MessageBus()
        provider = MagicMock()
        provider.get_default_model.return_value = "test-model"
        loop = AgentLoop(
            bus=bus, provider=provider, workspace=tmp_path,
            model="test-model", memory_window=10,
        )
        loop.provider.chat = AsyncMock(
            return_value=LLMResponse(content="ok", tool_calls=[]),
        )
        loop.tools.get_definitions = MagicMock(return_value=[])

        session = loop.sessions.get_or_create("cli:test")
        for i in range(15):
            session.add_message("user", f"msg{i}")
            session.add_message("assistant", f"resp{i}")
        loop.sessions.save(session)

        consolidation_cancelled = asyncio.Event()
        started = asyncio.Event()

        async def _slow_consolidate(_session, archive_all=False):
            if not archive_all:
                started.set()
                try:
                    await asyncio.sleep(10)  # simulate stuck LLM
                except asyncio.CancelledError:
                    consolidation_cancelled.set()
                    raise
            return True

        loop._consolidate_memory = _slow_consolidate

        # Trigger background consolidation
        msg = InboundMessage(
            channel="cli", sender_id="user", chat_id="test", content="hello",
        )
        await loop._process_message(msg)
        await started.wait()

        # /new should cancel the stuck task
        new_msg = InboundMessage(
            channel="cli", sender_id="user", chat_id="test", content="/new",
        )
        response = await loop._process_message(new_msg)

        assert consolidation_cancelled.is_set(), "Consolidation task was not cancelled"
        assert response is not None
        assert "cli:test" not in loop._consolidation_tasks

    @pytest.mark.asyncio
    async def test_new_without_inflight_task_works(self, tmp_path: Path) -> None:
        """/new works normally when no consolidation is in flight."""
        from nanobot.agent.loop import AgentLoop
        from nanobot.bus.events import InboundMessage
        from nanobot.bus.queue import MessageBus
        from nanobot.providers.base import LLMResponse

        bus = MessageBus()
        provider = MagicMock()
        provider.get_default_model.return_value = "test-model"
        loop = AgentLoop(
            bus=bus, provider=provider, workspace=tmp_path,
            model="test-model", memory_window=10,
        )
        loop.provider.chat = AsyncMock(
            return_value=LLMResponse(content="ok", tool_calls=[]),
        )
        loop.tools.get_definitions = MagicMock(return_value=[])

        session = loop.sessions.get_or_create("cli:test")
        for i in range(5):
            session.add_message("user", f"msg{i}")
        loop.sessions.save(session)

        async def _ok_consolidate(sess, archive_all=False):
            return True

        loop._consolidate_memory = _ok_consolidate

        new_msg = InboundMessage(
            channel="cli", sender_id="user", chat_id="test", content="/new",
        )
        response = await loop._process_message(new_msg)

        assert response is not None
        assert "new session started" in response.content.lower()
