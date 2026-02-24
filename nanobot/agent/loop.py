"""Agent loop: the core processing engine."""

from __future__ import annotations

import asyncio
import json
import re
from contextlib import AsyncExitStack
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

import structlog

from nanobot.logging import get_logger

from nanobot.agent.context import ContextBuilder
from nanobot.agent.memory import MemoryStore
from nanobot.agent.subagent import SubagentManager
from nanobot.agent.tools.cron import CronTool
from nanobot.agent.tools.filesystem import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from nanobot.agent.tools.message import MessageTool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.shell import ExecTool
from nanobot.agent.tools.spawn import SpawnTool
from nanobot.agent.tools.web import WebFetchTool, WebSearchTool
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMProvider
from nanobot.session.manager import Session, SessionManager

if TYPE_CHECKING:
    from nanobot.config.schema import ChannelsConfig, ExecToolConfig, FilesystemToolConfig
    from nanobot.cron.service import CronService

logger = get_logger(__name__)


class AgentLoop:
    """
    The agent loop is the core processing engine.

    It:
    1. Receives messages from the bus
    2. Builds context with history, memory, skills
    3. Calls the LLM
    4. Executes tool calls
    5. Sends responses back
    """

    def __init__(
        self,
        bus: MessageBus,
        provider: LLMProvider,
        workspace: Path,
        model: str | None = None,
        max_iterations: int = 40,
        temperature: float = 0.1,
        max_tokens: int = 4096,
        memory_window: int = 100,
        brave_api_key: str | None = None,
        exec_config: ExecToolConfig | None = None,
        filesystem_config: FilesystemToolConfig | None = None,
        cron_service: CronService | None = None,
        restrict_to_workspace: bool = False,
        session_manager: SessionManager | None = None,
        mcp_servers: dict | None = None,
        audit_tool_calls: bool = True,
        channels_config: ChannelsConfig | None = None,
    ):
        from nanobot.config.schema import ExecToolConfig, FilesystemToolConfig
        self.bus = bus
        self.channels_config = channels_config
        self.provider = provider
        self.workspace = workspace
        self.model = model or provider.get_default_model()
        self.max_iterations = max_iterations
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.memory_window = memory_window
        self.brave_api_key = brave_api_key
        self.exec_config = exec_config or ExecToolConfig()
        self.filesystem_config = filesystem_config or FilesystemToolConfig()
        self.cron_service = cron_service
        self.restrict_to_workspace = restrict_to_workspace

        self.context = ContextBuilder(workspace)
        self.sessions = session_manager or SessionManager(workspace)
        self.tools = ToolRegistry(audit=audit_tool_calls)
        self.subagents = SubagentManager(
            provider=provider,
            workspace=workspace,
            bus=bus,
            model=self.model,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            brave_api_key=brave_api_key,
            exec_config=self.exec_config,
            filesystem_config=self.filesystem_config,
            restrict_to_workspace=restrict_to_workspace,
        )

        self._running = False
        self._stopped_event = asyncio.Event()
        self._mcp_servers = mcp_servers or {}
        self._mcp_stack: AsyncExitStack | None = None
        self._mcp_connected = False
        self._mcp_connecting = False
        self._consolidating: set[str] = set()  # Session keys with consolidation in progress
        self._consolidation_tasks: dict[str, asyncio.Task] = {}  # session_key -> in-flight task
        self._consolidation_locks: dict[str, asyncio.Lock] = {}
        self._register_default_tools()

    def _register_default_tools(self) -> None:
        """Register the default set of tools."""
        allowed_dir = self.workspace if self.restrict_to_workspace else None
        audit = self.filesystem_config.audit_operations
        for cls in (ReadFileTool, WriteFileTool, EditFileTool, ListDirTool):
            self.tools.register(cls(workspace=self.workspace, allowed_dir=allowed_dir, audit_operations=audit))
        self.tools.register(ExecTool(
            working_dir=str(self.workspace),
            timeout=self.exec_config.timeout,
            deny_patterns=self.exec_config.deny_patterns,
            allow_patterns=self.exec_config.allow_patterns,
            restrict_to_workspace=self.restrict_to_workspace,
            audit_executions=self.exec_config.audit_executions,
        ))
        self.tools.register(WebSearchTool(api_key=self.brave_api_key))
        self.tools.register(WebFetchTool())
        self.tools.register(MessageTool(send_callback=self.bus.publish_outbound))
        self.tools.register(SpawnTool(manager=self.subagents))
        if self.cron_service:
            self.tools.register(CronTool(self.cron_service))

    async def _connect_mcp(self) -> None:
        """Connect to configured MCP servers (one-time, lazy)."""
        if self._mcp_connected or self._mcp_connecting or not self._mcp_servers:
            return
        self._mcp_connecting = True
        from nanobot.agent.tools.mcp import connect_mcp_servers
        try:
            self._mcp_stack = AsyncExitStack()
            await self._mcp_stack.__aenter__()
            await connect_mcp_servers(self._mcp_servers, self.tools, self._mcp_stack)
            self._mcp_connected = True
        except Exception as e:
            logger.error("Failed to connect MCP servers (will retry next message)", error=str(e))
            if self._mcp_stack:
                try:
                    await self._mcp_stack.aclose()
                except Exception:
                    pass
                self._mcp_stack = None
        finally:
            self._mcp_connecting = False

    def _set_tool_context(self, channel: str, chat_id: str, message_id: str | None = None) -> None:
        """Update context for all tools that need routing info."""
        if message_tool := self.tools.get("message"):
            if isinstance(message_tool, MessageTool):
                message_tool.set_context(channel, chat_id, message_id)

        if spawn_tool := self.tools.get("spawn"):
            if isinstance(spawn_tool, SpawnTool):
                spawn_tool.set_context(channel, chat_id)

        if cron_tool := self.tools.get("cron"):
            if isinstance(cron_tool, CronTool):
                cron_tool.set_context(channel, chat_id)

    @staticmethod
    def _strip_think(text: str | None) -> str | None:
        """Remove <think>…</think> blocks that some models embed in content."""
        if not text:
            return None
        return re.sub(r"<think>[\s\S]*?</think>", "", text).strip() or None

    @staticmethod
    def _tool_hint(tool_calls: list) -> str:
        """Format tool calls as concise hint, e.g. 'web_search("query")'."""
        def _fmt(tc):
            val = next(iter(tc.arguments.values()), None) if tc.arguments else None
            if not isinstance(val, str):
                return tc.name
            return f'{tc.name}("{val[:40]}…")' if len(val) > 40 else f'{tc.name}("{val}")'
        return ", ".join(_fmt(tc) for tc in tool_calls)

    _IN_LOOP_TOOL_RESULT_MAX_CHARS = 4000
    _IN_LOOP_ASSISTANT_TEXT_MAX_CHARS = 1000

    def _estimate_msg_tokens(self, msg: dict[str, Any]) -> int:
        """Estimate message tokens using ContextBuilder's tokenizer helpers."""
        est = getattr(self.context, "_estimate_message_tokens", None)
        if callable(est):
            return int(est(msg))
        content = msg.get("content", "")
        if isinstance(content, str):
            return max(1, len(content) // 4) if content else 0
        return 0

    def _truncate_runtime_message_for_budget(self, msg: dict[str, Any]) -> dict[str, Any]:
        """Reduce oversized content in current-turn runtime messages."""
        role = msg.get("role")
        content = msg.get("content")
        if role == "tool" and isinstance(content, str) and len(content) > self._IN_LOOP_TOOL_RESULT_MAX_CHARS:
            return {**msg, "content": content[:self._IN_LOOP_TOOL_RESULT_MAX_CHARS] + "\n... (truncated)"}
        if (
            role == "assistant"
            and isinstance(content, str)
            and not msg.get("tool_calls")
            and len(content) > self._IN_LOOP_ASSISTANT_TEXT_MAX_CHARS
        ):
            return {**msg, "content": content[:self._IN_LOOP_ASSISTANT_TEXT_MAX_CHARS] + "\n... (truncated)"}
        return msg

    def _guard_loop_messages(
        self, messages: list[dict[str, Any]], current_turn_start: int
    ) -> tuple[list[dict[str, Any]], int]:
        """Re-budget messages before each LLM iteration to avoid context overflows."""
        if not messages:
            return messages, current_turn_start

        system_count = 1 if messages and messages[0].get("role") == "system" else 0
        current_turn_start = max(system_count, min(current_turn_start, len(messages)))

        system_part = messages[:system_count]
        history = messages[system_count:current_turn_start]
        current_turn = messages[current_turn_start:]

        # 1) Trim prior-history aggressively while keeping current turn intact.
        max_ctx = getattr(self.context, "_max_context_tokens", 30_000)
        reserve = max(512, min(int(getattr(self, "max_tokens", 4096) or 4096), 4096))
        fixed_tokens = sum(self._estimate_msg_tokens(m) for m in system_part + current_turn)
        history_budget = max_ctx - reserve - fixed_tokens

        compacted = self.context._compact_history(history) if history else []
        trimmed_history = self.context._trim_history(compacted, max(history_budget, 0)) if compacted else []

        # 2) If current turn itself is too large, truncate bulky tool/assistant text content.
        guarded_current_turn = [self._truncate_runtime_message_for_budget(m) for m in current_turn]
        guarded = [*system_part, *trimmed_history, *guarded_current_turn]
        new_turn_start = system_count + len(trimmed_history)

        total_tokens = sum(self._estimate_msg_tokens(m) for m in guarded)
        if total_tokens + reserve > max_ctx:
            logger.warning(
                "loop_context_budget_exceeded_after_guard",
                total_tokens=total_tokens,
                reserve=reserve,
                max_context_tokens=max_ctx,
                history_messages=len(history),
                trimmed_history_messages=len(trimmed_history),
                current_turn_messages=len(current_turn),
            )
        return guarded, new_turn_start

    async def _run_agent_loop(
        self,
        initial_messages: list[dict],
        on_progress: Callable[..., Awaitable[None]] | None = None,
    ) -> tuple[str | None, list[str], list[dict]]:
        """Run the agent iteration loop. Returns (final_content, tools_used, messages)."""
        messages = initial_messages
        current_turn_start = len(initial_messages) - 1 if initial_messages else 0
        iteration = 0
        final_content = None
        tools_used: list[str] = []

        while iteration < self.max_iterations:
            iteration += 1
            messages, current_turn_start = self._guard_loop_messages(messages, current_turn_start)

            response = await self.provider.chat(
                messages=messages,
                tools=self.tools.get_definitions(),
                model=self.model,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
            )

            if response.has_tool_calls:
                if on_progress:
                    clean = self._strip_think(response.content)
                    if clean:
                        await on_progress(clean)
                    await on_progress(self._tool_hint(response.tool_calls), tool_hint=True)

                tool_call_dicts = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments, ensure_ascii=False)
                        }
                    }
                    for tc in response.tool_calls
                ]
                messages = self.context.add_assistant_message(
                    messages, response.content, tool_call_dicts,
                    reasoning_content=response.reasoning_content,
                )

                for tool_call in response.tool_calls:
                    tools_used.append(tool_call.name)
                    args_str = json.dumps(tool_call.arguments, ensure_ascii=False)
                    logger.info("Tool call", tool=tool_call.name, args=args_str[:200])
                    result = await self.tools.execute(tool_call.name, tool_call.arguments)
                    messages = self.context.add_tool_result(
                        messages, tool_call.id, tool_call.name, result
                    )
            else:
                messages = self.context.add_assistant_message(
                    messages, response.content, reasoning_content=response.reasoning_content,
                )
                final_content = self._strip_think(response.content)
                break

        if final_content is None and iteration >= self.max_iterations:
            logger.warning("Max iterations ({}) reached", self.max_iterations)
            final_content = (
                f"I reached the maximum number of tool call iterations ({self.max_iterations}) "
                "without completing the task. You can try breaking the task into smaller steps."
            )

        return final_content, tools_used, messages

    async def run(self) -> None:
        """Run the agent loop, processing messages from the bus."""
        self._running = True
        self._stopped_event.clear()
        try:
            await self._connect_mcp()
            logger.info("Agent loop started")

            while self._running:
                try:
                    msg = await asyncio.wait_for(
                        self.bus.consume_inbound(),
                        timeout=1.0
                    )
                    try:
                        response = await self._process_message(msg)
                        if response is not None:
                            await self.bus.publish_outbound(response)
                        elif msg.channel == "cli":
                            await self.bus.publish_outbound(OutboundMessage(
                                channel=msg.channel, chat_id=msg.chat_id, content="", metadata=msg.metadata or {},
                            ))
                    except Exception as e:
                        logger.exception(
                            "Error processing message",
                            error_type=type(e).__name__,
                            channel=msg.channel,
                            sender_id=msg.sender_id,
                            session_key=msg.session_key,
                        )
                        await self.bus.publish_outbound(OutboundMessage(
                            channel=msg.channel,
                            chat_id=msg.chat_id,
                            content="Sorry, I encountered an error. Please try again."
                        ))
                except asyncio.TimeoutError:
                    continue
        finally:
            self._running = False
            try:
                await self.close_mcp()
            except asyncio.CancelledError:
                # Ensure wait_stopped() can complete even if run() is cancelled during shutdown.
                self._stopped_event.set()
                logger.info("Agent loop stopped")
                raise
            self._stopped_event.set()
            logger.info("Agent loop stopped")

    async def close_mcp(self) -> None:
        """Close MCP connections."""
        if self._mcp_stack:
            try:
                await self._mcp_stack.aclose()
            except (RuntimeError, BaseExceptionGroup):
                pass  # MCP SDK cancel scope cleanup is noisy but harmless
            self._mcp_stack = None

    def stop(self) -> None:
        """Signal the agent loop to stop. Returns immediately; use wait_stopped() to await shutdown."""
        self._running = False
        logger.info("Agent loop stopping")

    async def wait_stopped(self, timeout: float = 30.0) -> bool:
        """Wait for the agent loop to finish processing the current message and shut down.

        Args:
            timeout: Maximum seconds to wait. Returns False if timeout exceeded.

        Returns:
            True if loop stopped cleanly, False if timeout was reached.
        """
        try:
            await asyncio.wait_for(self._stopped_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            logger.warning("Agent loop did not stop within {}s, forcing close", timeout)
            await self.close_mcp()
            return False

    def _get_consolidation_lock(self, session_key: str) -> asyncio.Lock:
        lock = self._consolidation_locks.get(session_key)
        if lock is None:
            lock = asyncio.Lock()
            self._consolidation_locks[session_key] = lock
        return lock

    def _prune_consolidation_lock(self, session_key: str, lock: asyncio.Lock) -> None:
        """Drop lock entry if no longer in use; batch-clean when dict grows large."""
        if not lock.locked():
            self._consolidation_locks.pop(session_key, None)
        # Batch cleanup: when dict exceeds 100 entries, purge all unlocked
        if len(self._consolidation_locks) > 100:
            stale = [k for k, v in self._consolidation_locks.items() if not v.locked()]
            for k in stale:
                del self._consolidation_locks[k]

    async def _process_message(
        self,
        msg: InboundMessage,
        session_key: str | None = None,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> OutboundMessage | None:
        """Process a single inbound message and return the response."""
        # System messages: parse origin from chat_id ("channel:chat_id")
        if msg.channel == "system":
            channel, chat_id = (msg.chat_id.split(":", 1) if ":" in msg.chat_id
                                else ("cli", msg.chat_id))
            structlog.contextvars.clear_contextvars()
            structlog.contextvars.bind_contextvars(
                channel=channel, sender_id=msg.sender_id, session_key=f"{channel}:{chat_id}",
            )
            logger.info("Processing system message")
            key = f"{channel}:{chat_id}"
            session = self.sessions.get_or_create(key)
            self._set_tool_context(channel, chat_id, msg.metadata.get("message_id"))
            history = session.get_history(max_messages=self.memory_window)
            messages = self.context.build_messages(
                history=history,
                current_message=msg.content, channel=channel, chat_id=chat_id,
            )
            skip = len(messages) - 1  # -1 to include current user message in saved turn
            final_content, _, all_msgs = await self._run_agent_loop(messages)
            self._save_turn(session, all_msgs, skip)
            self.sessions.save(session)
            return OutboundMessage(channel=channel, chat_id=chat_id,
                                  content=final_content or "Background task completed.")

        preview = msg.content[:80] + "..." if len(msg.content) > 80 else msg.content

        key = session_key or msg.session_key
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            channel=msg.channel, sender_id=msg.sender_id,
            session_key=key, chat_id=msg.chat_id,
        )
        logger.info("Processing message", preview=preview)

        session = self.sessions.get_or_create(key)

        # Slash commands
        cmd_raw = msg.content.strip()
        cmd = cmd_raw.lower()
        force_new = cmd in {"/new!", "/new --force", "/new -f"}
        if cmd == "/new" or force_new:
            # Cancel any in-flight consolidation for this session so /new
            # doesn't block waiting for a potentially stuck LLM call.
            running = self._consolidation_tasks.pop(session.key, None)
            if running and not running.done():
                running.cancel()
                try:
                    await running
                except (asyncio.CancelledError, Exception):
                    pass
            lock = self._get_consolidation_lock(session.key)
            self._consolidating.add(session.key)
            try:
                async with lock:
                    snapshot = session.messages[session.last_consolidated:]
                    if snapshot:
                        temp = Session(key=session.key)
                        temp.messages = list(snapshot)
                        if not await self._consolidate_memory(temp, archive_all=True):
                            if force_new:
                                logger.warning("/new force mode: archival failed, clearing session anyway", session_key=session.key)
                            else:
                                return OutboundMessage(
                                    channel=msg.channel, chat_id=msg.chat_id,
                                    content="Memory archival failed, session not cleared. Please try again.",
                                )
            except Exception:
                logger.exception("/new archival failed", session_key=session.key)
                if not force_new:
                    return OutboundMessage(
                        channel=msg.channel, chat_id=msg.chat_id,
                        content="Memory archival failed, session not cleared. Please try again.",
                    )
            finally:
                self._consolidating.discard(session.key)
                self._prune_consolidation_lock(session.key, lock)

            session.clear()
            self.sessions.save(session)
            self.sessions.invalidate(session.key)
            if force_new:
                return OutboundMessage(
                    channel=msg.channel, chat_id=msg.chat_id,
                    content="New session started (forced). Memory archival may have failed.",
                )
            return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id,
                                  content="New session started.")
        if cmd == "/help":
            return OutboundMessage(channel=msg.channel, chat_id=msg.chat_id,
                                  content="🐈 nanobot commands:\n/new — Archive and start a new conversation\n/new! — Force new conversation (clear even if archival fails)\n/help — Show available commands")

        if len(session.messages) > self.memory_window and session.key not in self._consolidating:
            self._consolidating.add(session.key)
            lock = self._get_consolidation_lock(session.key)

            async def _consolidate_and_unlock():
                try:
                    async with lock:
                        await self._consolidate_memory(session)
                finally:
                    self._consolidating.discard(session.key)
                    self._prune_consolidation_lock(session.key, lock)
                    # Remove ourselves from the task dict
                    self._consolidation_tasks.pop(session.key, None)

            _task = asyncio.create_task(_consolidate_and_unlock())
            self._consolidation_tasks[session.key] = _task

        self._set_tool_context(msg.channel, msg.chat_id, msg.metadata.get("message_id"))
        if message_tool := self.tools.get("message"):
            if isinstance(message_tool, MessageTool):
                message_tool.start_turn()

        history = session.get_history(max_messages=self.memory_window)
        initial_messages = self.context.build_messages(
            history=history,
            current_message=msg.content,
            media=msg.media if msg.media else None,
            channel=msg.channel, chat_id=msg.chat_id,
        )

        async def _bus_progress(content: str, *, tool_hint: bool = False) -> None:
            meta = dict(msg.metadata or {})
            meta["_progress"] = True
            meta["_tool_hint"] = tool_hint
            await self.bus.publish_outbound(OutboundMessage(
                channel=msg.channel, chat_id=msg.chat_id, content=content, metadata=meta,
            ))

        # Record length before _run_agent_loop mutates initial_messages in-place.
        # initial_messages = [system, compacted_history..., current_user]
        # We want to save current_user + all LLM responses, so skip system + history.
        skip = len(initial_messages) - 1  # -1 to include current_user

        final_content, _, all_msgs = await self._run_agent_loop(
            initial_messages, on_progress=on_progress or _bus_progress,
        )

        if final_content is None:
            final_content = "I've completed processing but have no response to give."

        preview = final_content[:120] + "..." if len(final_content) > 120 else final_content
        logger.info("Response sent", preview=preview)

        self._save_turn(session, all_msgs, skip)
        self.sessions.save(session)

        if message_tool := self.tools.get("message"):
            if isinstance(message_tool, MessageTool) and message_tool._sent_in_turn:
                return None

        return OutboundMessage(
            channel=msg.channel, chat_id=msg.chat_id, content=final_content,
            metadata=msg.metadata or {},
        )

    _TOOL_RESULT_MAX_CHARS = 500
    _ASSISTANT_HISTORY_MAX_CHARS = 300

    @staticmethod
    def _strip_images_from_content(content: Any) -> Any:
        """Replace base64 image blocks with a lightweight placeholder."""
        if not isinstance(content, list):
            return content
        stripped: list[dict] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "image_url":
                stripped.append({"type": "text", "text": "[image]"})
            else:
                stripped.append(block)
        # If only text blocks remain, collapse to a plain string
        texts = [b["text"] for b in stripped if isinstance(b, dict) and b.get("type") == "text"]
        if len(texts) == len(stripped):
            return " ".join(texts)
        return stripped

    def _save_turn(self, session: Session, messages: list[dict], skip: int) -> None:
        """Save new-turn messages into session, truncating large content and stripping images."""
        from datetime import datetime
        for m in messages[skip:]:
            entry = {k: v for k, v in m.items() if k != "reasoning_content"}
            # Strip base64 images from multimodal content
            if "content" in entry:
                entry["content"] = self._strip_images_from_content(entry["content"])
            # Truncate long assistant replies (keep summary-length prefix)
            if entry.get("role") == "assistant" and isinstance(entry.get("content"), str):
                content = entry["content"]
                if len(content) > self._ASSISTANT_HISTORY_MAX_CHARS:
                    entry["content"] = content[:self._ASSISTANT_HISTORY_MAX_CHARS] + "\n... (truncated)"
            if entry.get("role") == "tool" and isinstance(entry.get("content"), str):
                content = entry["content"]
                if len(content) > self._TOOL_RESULT_MAX_CHARS:
                    entry["content"] = content[:self._TOOL_RESULT_MAX_CHARS] + "\n... (truncated)"
            entry.setdefault("timestamp", datetime.now().isoformat())
            session.messages.append(entry)
        session.updated_at = datetime.now()

    async def _consolidate_memory(self, session, archive_all: bool = False) -> bool:
        """Delegate to MemoryStore.consolidate(). Returns True on success."""
        return await MemoryStore(self.workspace).consolidate(
            session, self.provider, self.model,
            archive_all=archive_all, memory_window=self.memory_window,
        )

    async def process_direct(
        self,
        content: str,
        session_key: str = "cli:direct",
        channel: str = "cli",
        chat_id: str = "direct",
        on_progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> str:
        """Process a message directly (for CLI or cron usage)."""
        await self._connect_mcp()
        msg = InboundMessage(channel=channel, sender_id="user", chat_id=chat_id, content=content)
        response = await self._process_message(msg, session_key=session_key, on_progress=on_progress)
        return response.content if response else ""
