"""Core turn runner for LLM + tool iteration."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Awaitable, Callable

from nanobot.logging import get_logger

logger = get_logger(__name__)

TurnEventCallback = Callable[[dict[str, Any]], Awaitable[None]]


def _session_tool_details(details: dict[str, Any]) -> dict[str, Any]:
    """Keep a compact, versioned subset of tool details for session persistence only."""
    if not details:
        return {}
    keep_keys = (
        "op",
        "path",
        "requested_path",
        "first_changed_line",
        "replacement_count",
        "diff_truncated",
        "channel",
        "chat_id",
        "message_id",
        "attachment_count",
        "sent",
        "accepted",
        "origin_channel",
        "origin_chat_id",
        "label",
        "task_len",
        "blocked",
        "timed_out",
        "exit_code",
    )
    compact = {k: details[k] for k in keep_keys if k in details}
    if not compact:
        return {}
    return {
        "schema_version": 1,
        "tool": details.get("op"),
        "data": compact,
    }


class TurnRunner:
    """Run a single agent turn including iterative tool calls."""

    def __init__(
        self,
        *,
        provider: Any,
        tools: Any,
        context_builder: Any,
        model: str,
        temperature: float,
        max_tokens: int,
        max_iterations: int,
        guard_loop_messages: Callable[[list[dict[str, Any]], int], tuple[list[dict[str, Any]], int]],
        strip_think: Callable[[str | None], str | None],
        tool_hint: Callable[[list[Any]], str],
    ) -> None:
        self.provider = provider
        self.tools = tools
        self.context = context_builder
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.max_iterations = max_iterations
        self.guard_loop_messages = guard_loop_messages
        self.strip_think = strip_think
        self.tool_hint = tool_hint

    async def run(
        self,
        initial_messages: list[dict[str, Any]],
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_event: TurnEventCallback | None = None,
        event_source: str = "turn_runner",
    ) -> tuple[str | None, list[str], list[dict[str, Any]]]:
        """Run the iterative turn loop. Returns (final_content, tools_used, messages)."""
        messages = initial_messages
        current_turn_start = len(initial_messages) - 1 if initial_messages else 0
        iteration = 0
        final_content = None
        tools_used: list[str] = []
        turn_id = f"turn_{uuid.uuid4().hex[:12]}"
        event_sequence = 0

        async def _emit_event(payload: dict[str, Any]) -> None:
            nonlocal event_sequence
            if not on_event:
                return
            event_sequence += 1
            await on_event({
                "turn_id": turn_id,
                "sequence": event_sequence,
                "timestamp_ms": int(time.time() * 1000),
                "source": event_source,
                **payload,
            })

        if on_event:
            await _emit_event({
                "type": "turn_start",
                "initial_message_count": len(initial_messages),
                "max_iterations": self.max_iterations,
            })

        while iteration < self.max_iterations:
            iteration += 1
            messages, current_turn_start = self.guard_loop_messages(messages, current_turn_start)

            response = await self.provider.chat(
                messages=messages,
                tools=self.tools.get_definitions(),
                model=self.model,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
            )

            if response.has_tool_calls:
                if on_progress:
                    clean = self.strip_think(response.content)
                    if clean:
                        await on_progress(clean)
                    await on_progress(self.tool_hint(response.tool_calls), tool_hint=True)

                tool_call_dicts = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                        },
                    }
                    for tc in response.tool_calls
                ]
                messages = self.context.add_assistant_message(
                    messages,
                    response.content,
                    tool_call_dicts,
                    reasoning_content=response.reasoning_content,
                )

                for tool_call in response.tool_calls:
                    tools_used.append(tool_call.name)
                    args_str = json.dumps(tool_call.arguments, ensure_ascii=False)
                    logger.info("Tool call", tool=tool_call.name, args=args_str[:200])
                    if on_event:
                        await _emit_event({
                            "type": "tool_start",
                            "iteration": iteration,
                            "tool": tool_call.name,
                            "tool_call_id": tool_call.id,
                            "arguments": tool_call.arguments,
                        })
                    tool_result = await self.tools.execute_result(tool_call.name, tool_call.arguments)
                    if tool_result.details:
                        logger.debug(
                            "Tool result details",
                            tool=tool_call.name,
                            detail_keys=sorted(tool_result.details.keys()),
                            details={
                                k: tool_result.details.get(k)
                                for k in ("op", "path", "first_changed_line", "diff_truncated")
                                if k in tool_result.details
                            },
                        )
                    if on_event:
                        await _emit_event({
                            "type": "tool_end",
                            "iteration": iteration,
                            "tool": tool_call.name,
                            "tool_call_id": tool_call.id,
                            "is_error": tool_result.is_error,
                            "has_details": bool(tool_result.details),
                            "detail_op": tool_result.details.get("op") if tool_result.details else None,
                        })
                    messages = self.context.add_tool_result(
                        messages,
                        tool_call.id,
                        tool_call.name,
                        tool_result.text,
                        metadata=_session_tool_details(tool_result.details),
                    )
            else:
                messages = self.context.add_assistant_message(
                    messages,
                    response.content,
                    reasoning_content=response.reasoning_content,
                )
                final_content = self.strip_think(response.content)
                break

        if final_content is None and iteration >= self.max_iterations:
            logger.warning("Max iterations ({}) reached", self.max_iterations)
            final_content = (
                f"I reached the maximum number of tool call iterations ({self.max_iterations}) "
                "without completing the task. You can try breaking the task into smaller steps."
            )
        if on_event:
            await _emit_event({
                "type": "turn_end",
                "iterations": iteration,
                "tool_count": len(tools_used),
                "completed": final_content is not None,
                "max_iterations_reached": iteration >= self.max_iterations and final_content is not None and (
                    final_content.startswith("I reached the maximum number of tool call iterations")
                ),
            })

        return final_content, tools_used, messages
