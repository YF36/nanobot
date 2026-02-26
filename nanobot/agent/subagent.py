"""Subagent manager for background task execution."""

import asyncio
import uuid
from pathlib import Path
from typing import Any

from nanobot.logging import get_logger

from nanobot.agent.turn_runner import TurnRunner
from nanobot.agent.turn_events import (
    TURN_EVENT_TOOL_END,
    TURN_EVENT_TOOL_START,
    TurnEventPayload,
    turn_event_trace_fields,
)
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMProvider
from nanobot.agent.tools.factory import create_standard_tool_registry

logger = get_logger(__name__)


class _SubagentContextAdapter:
    """Minimal context adapter for TurnRunner, preserving subagent message shape."""

    @staticmethod
    def add_assistant_message(
        messages: list[dict[str, Any]],
        content: str | None,
        tool_calls: list[dict[str, Any]] | None = None,
        reasoning_content: str | None = None,
    ) -> list[dict[str, Any]]:
        entry: dict[str, Any] = {
            "role": "assistant",
            "content": content or "",
        }
        if tool_calls:
            entry["tool_calls"] = tool_calls
        # Subagent loop historically ignored reasoning_content; keep behavior.
        messages.append(entry)
        return messages


class _SubagentPromptBuilder:
    """Build focused system prompts for subagent runs."""

    @staticmethod
    def build(workspace: Path, task: str) -> str:
        from datetime import datetime
        import time as _time

        now = datetime.now().strftime("%Y-%m-%d %H:%M (%A)")
        tz = _time.strftime("%Z") or "UTC"

        return f"""# Subagent

## Current Time
{now} ({tz})

You are a subagent spawned by the main agent to complete a specific task.

## Rules
1. Stay focused - complete only the assigned task, nothing else
2. Your final response will be reported back to the main agent
3. Do not initiate conversations or take on side tasks
4. Be concise but informative in your findings

## What You Can Do
- Read and write files in the workspace
- Execute shell commands
- Search the web and fetch web pages
- Complete the task thoroughly

## What You Cannot Do
- Send messages directly to users (no message tool available)
- Spawn other subagents
- Access the main agent's conversation history

## Workspace
Your workspace is at: {workspace}
Skills are available at: {workspace}/skills/ (read SKILL.md files as needed)

When you have completed the task, provide a clear summary of your findings or actions."""


class _SubagentResultAnnouncer:
    """Format and publish subagent results back to the main agent via the bus."""

    def __init__(self, bus: MessageBus) -> None:
        self.bus = bus

    @staticmethod
    def build_content(label: str, task: str, result: str, status: str) -> str:
        status_text = "completed successfully" if status == "ok" else "failed"
        return f"""[Subagent '{label}' {status_text}]

Task: {task}

Result:
{result}

Summarize this naturally for the user. Keep it brief (1-2 sentences). Do not mention technical details like "subagent" or task IDs."""

    async def publish(
        self,
        *,
        task_id: str,
        label: str,
        task: str,
        result: str,
        origin: dict[str, str],
        status: str,
    ) -> None:
        msg = InboundMessage(
            channel="system",
            sender_id="subagent",
            chat_id=f"{origin['channel']}:{origin['chat_id']}",
            content=self.build_content(label, task, result, status),
        )
        await self.bus.publish_inbound(msg)
        logger.debug("Subagent announced result", task_id=task_id, channel=origin['channel'], chat_id=origin['chat_id'])

    @staticmethod
    def add_tool_result(
        messages: list[dict[str, Any]],
        tool_call_id: str,
        tool_name: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        messages.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "name": tool_name,
            "content": content,
        })
        # Ignore internal metadata in subagent loop to preserve previous provider payload shape.
        return messages


class SubagentManager:
    """
    Manages background subagent execution.
    
    Subagents are lightweight agent instances that run in the background
    to handle specific tasks. They share the same LLM provider but have
    isolated context and a focused system prompt.
    """
    
    def __init__(
        self,
        provider: LLMProvider,
        workspace: Path,
        bus: MessageBus,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
        brave_api_key: str | None = None,
        web_search_max_results: int = 5,
        web_search_timeout_s: float = 15.0,
        web_search_max_retries: int = 1,
        exec_config: "ExecToolConfig | None" = None,
        filesystem_config: "FilesystemToolConfig | None" = None,
        restrict_to_workspace: bool = False,
        max_concurrent_subagents: int = 5,
        subagent_timeout: float = 300.0,
        subagent_max_iterations: int = 15,
    ):
        from nanobot.config.schema import ExecToolConfig, FilesystemToolConfig
        self.provider = provider
        self.workspace = workspace
        self.bus = bus
        self.model = model or provider.get_default_model()
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.brave_api_key = brave_api_key
        self.web_search_max_results = web_search_max_results
        self.web_search_timeout_s = web_search_timeout_s
        self.web_search_max_retries = web_search_max_retries
        self.exec_config = exec_config or ExecToolConfig()
        self.filesystem_config = filesystem_config or FilesystemToolConfig()
        self.restrict_to_workspace = restrict_to_workspace
        self.max_concurrent_subagents = max_concurrent_subagents
        self.subagent_timeout = subagent_timeout
        self.subagent_max_iterations = subagent_max_iterations
        self._running_tasks: dict[str, asyncio.Task[None]] = {}
        self._session_tasks: dict[str, set[str]] = {}  # session_key -> subagent task ids
        self._result_announcer = _SubagentResultAnnouncer(bus)
    
    async def spawn(
        self,
        task: str,
        label: str | None = None,
        origin_channel: str = "cli",
        origin_chat_id: str = "direct",
        session_key: str | None = None,
    ) -> str:
        """
        Spawn a subagent to execute a task in the background.
        
        Args:
            task: The task description for the subagent.
            label: Optional human-readable label for the task.
            origin_channel: The channel to announce results to.
            origin_chat_id: The chat ID to announce results to.
        
        Returns:
            Status message indicating the subagent was started.
        """
        if len(self._running_tasks) >= self.max_concurrent_subagents:
            return (
                f"Cannot spawn subagent: limit of {self.max_concurrent_subagents} "
                f"concurrent subagents reached. Wait for one to finish."
            )

        task_id = str(uuid.uuid4())[:8]
        display_label = label or task[:30] + ("..." if len(task) > 30 else "")

        origin = {
            "channel": origin_channel,
            "chat_id": origin_chat_id,
        }

        # Create background task with timeout wrapper
        bg_task = asyncio.create_task(
            asyncio.wait_for(
                self._run_subagent(task_id, task, display_label, origin),
                timeout=self.subagent_timeout,
            )
        )
        self._running_tasks[task_id] = bg_task
        if session_key:
            self._session_tasks.setdefault(session_key, set()).add(task_id)

        def _cleanup(_: asyncio.Task[Any]) -> None:
            self._running_tasks.pop(task_id, None)
            if session_key and (ids := self._session_tasks.get(session_key)) is not None:
                ids.discard(task_id)
                if not ids:
                    self._session_tasks.pop(session_key, None)

        bg_task.add_done_callback(_cleanup)

        logger.info("Spawned subagent", task_id=task_id, label=display_label, session_key=session_key)
        return f"Subagent [{display_label}] started (id: {task_id}). I'll notify you when it completes."
    
    async def _run_subagent(
        self,
        task_id: str,
        task: str,
        label: str,
        origin: dict[str, str],
    ) -> None:
        """Execute the subagent task and announce the result."""
        logger.info("Subagent starting task", task_id=task_id, label=label)
        
        try:
            # Build subagent tools (no message tool, no spawn tool)
            tools = create_standard_tool_registry(
                workspace=self.workspace,
                brave_api_key=self.brave_api_key,
                web_search_max_results=self.web_search_max_results,
                web_search_timeout_s=self.web_search_timeout_s,
                web_search_max_retries=self.web_search_max_retries,
                exec_config=self.exec_config,
                filesystem_config=self.filesystem_config,
                restrict_to_workspace=self.restrict_to_workspace,
            )
            
            # Build messages with subagent-specific prompt
            system_prompt = self._build_subagent_prompt(task)
            messages: list[dict[str, Any]] = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": task},
            ]

            # Reuse shared turn loop logic with a minimal context adapter.
            runner = TurnRunner(
                provider=self.provider,
                tools=tools,
                context_builder=_SubagentContextAdapter(),
                model=self.model,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
                max_iterations=self.subagent_max_iterations,
                guard_loop_messages=lambda m, current: (m, current),
                strip_think=lambda text: text,
                tool_hint=lambda _: "",
            )
            final_result, _, _ = await runner.run(
                messages,
                on_event=self._on_turn_event,
                event_source="subagent",
            )

            if final_result is None:
                final_result = "Task completed but no final response was generated."
            
            logger.info("Subagent completed successfully", task_id=task_id)
            await self._announce_result(task_id, label, task, final_result, origin, "ok")
            
        except Exception as e:
            error_msg = f"Error: {str(e)}"
            logger.error("Subagent failed", task_id=task_id, error=str(e))
            await self._announce_result(task_id, label, task, error_msg, origin, "error")
    
    async def _announce_result(
        self,
        task_id: str,
        label: str,
        task: str,
        result: str,
        origin: dict[str, str],
        status: str,
    ) -> None:
        """Announce the subagent result to the main agent via the message bus."""
        await self._result_announcer.publish(
            task_id=task_id,
            label=label,
            task=task,
            result=result,
            origin=origin,
            status=status,
        )
    
    async def cancel_by_session(self, session_key: str) -> int:
        """Cancel all running subagents for a session. Returns count cancelled."""
        task_ids = list(self._session_tasks.get(session_key, set()))
        tasks = [
            self._running_tasks[tid]
            for tid in task_ids
            if tid in self._running_tasks and not self._running_tasks[tid].done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        return len(tasks)

    def _build_subagent_prompt(self, task: str) -> str:
        """Build a focused system prompt for the subagent."""
        return _SubagentPromptBuilder.build(self.workspace, task)

    async def _on_turn_event(self, event: TurnEventPayload) -> None:
        """Internal subagent turn event sink for debug/observability."""
        event_type = event.get("type", "unknown")
        event_kind = event.get("kind", event_type)
        if event_type in {TURN_EVENT_TOOL_START, TURN_EVENT_TOOL_END}:
            logger.debug(
                "subagent_turn_event",
                event_kind=event_kind,
                event_type=event_type,
                **turn_event_trace_fields(event),
                tool=event.get("tool"),
                iteration=event.get("iteration"),
                tool_call_id=event.get("tool_call_id"),
                is_error=event.get("is_error"),
                detail_op=event.get("detail_op"),
            )
            return
        logger.debug(
            "subagent_turn_event",
            event_kind=event_kind,
            event_type=event_type,
            **turn_event_trace_fields(event),
            payload=event,
        )
    
    def get_running_count(self) -> int:
        """Return the number of currently running subagents."""
        return len(self._running_tasks)
