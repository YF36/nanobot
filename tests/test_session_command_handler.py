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

