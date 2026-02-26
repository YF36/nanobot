import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.consolidation_coordinator import ConsolidationCoordinator
from nanobot.agent.session_command_handler import SessionCommandHandler
from nanobot.bus.events import InboundMessage
from nanobot.session.manager import Session


def _make_handler(*, cancel_session_tasks=None) -> SessionCommandHandler:
    return SessionCommandHandler(
        sessions=MagicMock(),
        consolidation=ConsolidationCoordinator(),
        consolidate_memory=AsyncMock(return_value=True),
        cancel_session_tasks=cancel_session_tasks,
    )


def _msg(content: str) -> InboundMessage:
    return InboundMessage(channel="cli", sender_id="u1", chat_id="c1", content=content)


@pytest.mark.asyncio
async def test_help_includes_stop_command() -> None:
    handler = _make_handler()
    out = await handler.handle(_msg("/help"), Session(key="cli:c1"))
    assert out is not None
    assert "/stop" in out.content


@pytest.mark.asyncio
async def test_stop_reports_no_active_task_when_none() -> None:
    cancel = AsyncMock(return_value=0)
    handler = _make_handler(cancel_session_tasks=cancel)
    session = Session(key="cli:c1")

    out = await handler.handle(_msg("/stop"), session)

    assert out is not None
    assert out.content == "No active task to stop."
    cancel.assert_awaited_once_with("cli:c1")


@pytest.mark.asyncio
async def test_stop_reports_cancelled_count() -> None:
    cancel = AsyncMock(return_value=2)
    handler = _make_handler(cancel_session_tasks=cancel)

    out = await handler.handle(_msg("/stop"), Session(key="cli:c1"))

    assert out is not None
    assert out.content == "⏹ Stopped 2 task(s)."


@pytest.mark.asyncio
async def test_new_schedules_background_archive_and_returns_immediately() -> None:
    sessions = MagicMock()
    consolidation = ConsolidationCoordinator()
    started = asyncio.Event()
    release = asyncio.Event()

    async def _slow_consolidate(_session: Session, archive_all: bool) -> bool:
        assert archive_all is True
        started.set()
        await release.wait()
        return True

    handler = SessionCommandHandler(
        sessions=sessions,
        consolidation=consolidation,
        consolidate_memory=_slow_consolidate,
    )
    session = Session(key="cli:c1")
    session.add_message("user", "hello")
    session.add_message("assistant", "hi")

    out = await handler.handle(_msg("/new"), session)

    assert out is not None
    assert "new session started" in out.content.lower()
    assert session.messages == []
    sessions.save.assert_called_once()
    sessions.invalidate.assert_called_once_with("cli:c1")
    assert "cli:c1" in consolidation.tasks

    await started.wait()
    release.set()
    await asyncio.gather(*consolidation.tasks.values(), return_exceptions=True)
