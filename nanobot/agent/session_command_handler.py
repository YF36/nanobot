"""Session-scoped command handling for AgentLoop."""

from __future__ import annotations

from typing import TYPE_CHECKING, Awaitable, Callable

from nanobot.logging import get_logger

from nanobot.agent.consolidation_coordinator import ConsolidationCoordinator
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.session.manager import Session

if TYPE_CHECKING:
    from nanobot.session.manager import SessionManager

logger = get_logger(__name__)


class SessionCommandHandler:
    """Handle slash commands that operate on conversation sessions."""

    def __init__(
        self,
        *,
        sessions: "SessionManager",
        consolidation: ConsolidationCoordinator,
        consolidate_memory: Callable[[Session, bool], Awaitable[bool]],
        cancel_session_tasks: Callable[[str], Awaitable[int]] | None = None,
    ) -> None:
        self.sessions = sessions
        self.consolidation = consolidation
        self.consolidate_memory = consolidate_memory
        self.cancel_session_tasks = cancel_session_tasks

    async def handle(self, msg: InboundMessage, session: Session) -> OutboundMessage | None:
        """Return command response if handled, else None."""
        cmd_raw = msg.content.strip()
        cmd = cmd_raw.lower()
        force_new = cmd in {"/new!", "/new --force", "/new -f"}

        if cmd == "/new" or force_new:
            return await self._handle_new(msg, session, force_new=force_new)

        if cmd == "/help":
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content=(
                    "🐈 nanobot commands:\n"
                    "/new — Archive and start a new conversation\n"
                    "/new! — Force new conversation (clear even if archival fails)\n"
                    "/stop — Stop running background tasks for this conversation\n"
                    "/help — Show available commands"
                ),
            )

        if cmd == "/stop":
            return await self._handle_stop(msg, session)

        return None

    async def _handle_stop(self, msg: InboundMessage, session: Session) -> OutboundMessage:
        try:
            cancelled = 0
            if self.cancel_session_tasks is not None:
                cancelled = await self.cancel_session_tasks(session.key)
            if cancelled > 0:
                content = f"⏹ Stopped {cancelled} task(s)."
            else:
                content = "No active task to stop."
        except Exception:
            logger.exception("/stop failed", session_key=session.key)
            content = "Failed to stop active tasks. Please try again."
        return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content=content)

    async def _handle_new(self, msg: InboundMessage, session: Session, *, force_new: bool) -> OutboundMessage:
        await self.consolidation.cancel_inflight(session.key)

        snapshot = list(session.messages[session.last_consolidated:])

        if snapshot:
            temp = Session(key=session.key)
            temp.messages = snapshot

            async def _archive_snapshot_background() -> None:
                try:
                    ok = await self.consolidate_memory(temp, True)
                    if not ok:
                        logger.warning(
                            "/new background archival failed",
                            session_key=session.key,
                            force_new=force_new,
                        )
                except Exception:
                    logger.exception("/new background archival errored", session_key=session.key)

            task = self.consolidation.start_background(session.key, _archive_snapshot_background)
            if task is None:
                logger.debug("/new background archival skipped (already in progress)", session_key=session.key)
            else:
                logger.debug(
                    "/new background archival scheduled",
                    session_key=session.key,
                    deferred=True,
                    force_new=force_new,
                    snapshot_len=len(snapshot),
                )

        session.clear()
        self.sessions.save(session)
        self.sessions.invalidate(session.key)
        if force_new:
            return OutboundMessage(
                channel=msg.channel,
                chat_id=msg.chat_id,
                content="New session started (forced). Memory archival may have failed.",
            )
        return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id, content="New session started.")
