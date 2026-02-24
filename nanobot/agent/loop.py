"""Agent loop: the core processing engine."""

from __future__ import annotations

import asyncio
import re
from contextlib import AsyncExitStack
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from nanobot.logging import get_logger

from nanobot.agent.consolidation_coordinator import ConsolidationCoordinator
from nanobot.agent.context import ContextBuilder
from nanobot.agent.memory import MemoryStore
from nanobot.agent.message_processor import MessageProcessor
from nanobot.agent.session_command_handler import SessionCommandHandler
from nanobot.agent.subagent import SubagentManager
from nanobot.agent.turn_runner import TurnRunner
from nanobot.agent.tools.cron import CronTool
from nanobot.agent.tools.factory import create_standard_tool_registry
from nanobot.agent.tools.message import MessageTool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.spawn import SpawnTool
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMProvider
from nanobot.session.manager import SessionManager

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
        self.audit_tool_calls = audit_tool_calls

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
        self._consolidation = ConsolidationCoordinator()
        # Compatibility aliases for existing tests and internal references.
        self._consolidating = self._consolidation.in_progress
        self._consolidation_tasks = self._consolidation.tasks
        self._consolidation_locks = self._consolidation.locks
        self._command_handler = SessionCommandHandler(
            sessions=self.sessions,
            consolidation=self._consolidation,
            consolidate_memory=self._consolidate_memory_for_command,
        )
        self._message_processor = MessageProcessor(
            sessions=self.sessions,
            context=self.context,
            tools=self.tools,
            bus=self.bus,
            command_handler=self._command_handler,
            consolidation=self._consolidation,
            memory_window=self.memory_window,
            set_tool_context=self._set_tool_context,
            run_agent_loop=self._run_agent_loop,
            save_turn=self._save_turn,
            consolidate_memory=self._consolidate_memory_default,
        )
        self._register_default_tools()

    def _register_default_tools(self) -> None:
        """Register the default set of tools."""
        self.tools = create_standard_tool_registry(
            workspace=self.workspace,
            brave_api_key=self.brave_api_key,
            exec_config=self.exec_config,
            filesystem_config=self.filesystem_config,
            restrict_to_workspace=self.restrict_to_workspace,
            audit_tool_calls=self.audit_tool_calls,
            message_send_callback=self.bus.publish_outbound,
            spawn_manager=self.subagents,
            cron_service=self.cron_service,
        )

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
        runner = TurnRunner(
            provider=self.provider,
            tools=self.tools,
            context_builder=self.context,
            model=self.model,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            max_iterations=self.max_iterations,
            guard_loop_messages=self._guard_loop_messages,
            strip_think=self._strip_think,
            tool_hint=self._tool_hint,
        )
        return await runner.run(initial_messages, on_progress=on_progress)

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
        return self._consolidation.get_lock(session_key)

    def _prune_consolidation_lock(self, session_key: str, lock: asyncio.Lock) -> None:
        self._consolidation.prune_lock(session_key, lock)

    async def _process_message(
        self,
        msg: InboundMessage,
        session_key: str | None = None,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> OutboundMessage | None:
        """Process a single inbound message and return the response."""
        return await self._message_processor.process(msg, session_key=session_key, on_progress=on_progress)

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

    async def _consolidate_memory_for_command(self, session, archive_all: bool) -> bool:
        """Adapter used by SessionCommandHandler to match a simpler callback signature."""
        return await self._consolidate_memory(session, archive_all=archive_all)

    async def _consolidate_memory_default(self, session) -> bool:
        """Adapter for background consolidation callback signature."""
        return await self._consolidate_memory(session)

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
