"""Core turn runner for LLM + tool iteration."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any, Awaitable, Callable, cast

from nanobot.logging import get_logger
from nanobot.agent.turn_events import (
    TURN_EVENT_TOOL_END,
    TURN_EVENT_TOOL_START,
    TURN_EVENT_TURN_END,
    TURN_EVENT_TURN_START,
    TurnEventCallback,
    TurnEventPayload,
)

logger = get_logger(__name__)

_CHAT_RETRY_MAX_ATTEMPTS = 2
_CHAT_RETRY_BASE_DELAY_S = 0.2
_CONTEXT_OVERFLOW_EXTRA_COMPACTIONS = 1


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


def _is_context_length_error(text: str | None) -> bool:
    if not text:
        return False
    lower = text.lower()
    return (
        "maximum context length" in lower
        or "exceeds the model's maximum context length" in lower
        or "input tokens exceeds" in lower
        or "context length" in lower
    )


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

    @staticmethod
    def _compact_history_once(
        messages: list[dict[str, Any]],
        current_turn_start: int,
    ) -> tuple[list[dict[str, Any]], int]:
        """Emergency compaction: drop one oldest history message before current turn."""
        if not messages:
            return messages, current_turn_start
        system_count = 1 if messages and messages[0].get("role") == "system" else 0
        if current_turn_start <= system_count:
            return messages, current_turn_start
        drop_index = system_count
        compacted = messages[:drop_index] + messages[drop_index + 1 :]
        return compacted, max(system_count, current_turn_start - 1)

    async def _chat_with_retries(
        self,
        *,
        messages: list[dict[str, Any]],
        current_turn_start: int,
    ) -> tuple[Any, list[dict[str, Any]], int, dict[str, int]]:
        """Call provider.chat with bounded retries and one overflow compaction retry path."""
        attempt = 0
        overflow_compactions = 0
        exception_retries = 0
        error_finish_retries = 0
        local_messages = messages
        local_turn_start = current_turn_start

        while True:
            attempt += 1
            try:
                response = await self.provider.chat(
                    messages=local_messages,
                    tools=self.tools.get_definitions(),
                    model=self.model,
                    temperature=self.temperature,
                    max_tokens=self.max_tokens,
                )
            except Exception as e:
                if attempt >= _CHAT_RETRY_MAX_ATTEMPTS:
                    raise
                exception_retries += 1
                delay = _CHAT_RETRY_BASE_DELAY_S * (2 ** (attempt - 1))
                logger.warning(
                    "llm_chat_retry",
                    attempt=attempt,
                    delay_s=delay,
                    error_type=type(e).__name__,
                    reason="exception",
                )
                await asyncio.sleep(delay)
                continue

            if getattr(response, "finish_reason", "") == "error":
                content = getattr(response, "content", None)
                if _is_context_length_error(content) and overflow_compactions < _CONTEXT_OVERFLOW_EXTRA_COMPACTIONS:
                    overflow_compactions += 1
                    guarded_messages, guarded_start = self.guard_loop_messages(local_messages, local_turn_start)
                    if guarded_messages == local_messages and guarded_start == local_turn_start:
                        guarded_messages, guarded_start = self._compact_history_once(local_messages, local_turn_start)
                    if guarded_messages != local_messages or guarded_start != local_turn_start:
                        logger.warning(
                            "llm_context_overflow_retry_after_compaction",
                            attempt=attempt,
                            compaction_retry=overflow_compactions,
                            messages_before=len(local_messages),
                            messages_after=len(guarded_messages),
                        )
                        local_messages, local_turn_start = guarded_messages, guarded_start
                        continue
                if attempt < _CHAT_RETRY_MAX_ATTEMPTS:
                    error_finish_retries += 1
                    delay = _CHAT_RETRY_BASE_DELAY_S * (2 ** (attempt - 1))
                    logger.warning(
                        "llm_chat_retry",
                        attempt=attempt,
                        delay_s=delay,
                        reason="finish_reason_error",
                        context_overflow=_is_context_length_error(content),
                    )
                    await asyncio.sleep(delay)
                    continue

            return response, local_messages, local_turn_start, {
                "llm_retry_count": exception_retries + error_finish_retries,
                "llm_exception_retry_count": exception_retries,
                "llm_error_finish_retry_count": error_finish_retries,
                "llm_overflow_compaction_retries": overflow_compactions,
            }

    async def run(
        self,
        initial_messages: list[dict[str, Any]],
        on_progress: Callable[..., Awaitable[None]] | None = None,
        on_event: TurnEventCallback | None = None,
        event_source: str = "turn_runner",
        should_interrupt_after_tool: Callable[[], bool | Awaitable[bool]] | None = None,
    ) -> tuple[str | None, list[str], list[dict[str, Any]]]:
        """Run the iterative turn loop. Returns (final_content, tools_used, messages)."""
        messages = initial_messages
        current_turn_start = len(initial_messages) - 1 if initial_messages else 0
        iteration = 0
        final_content = None
        interrupted_for_followup = False
        interruption_info: dict[str, Any] = {}
        retry_stats = {
            "llm_retry_count": 0,
            "llm_exception_retry_count": 0,
            "llm_error_finish_retry_count": 0,
            "llm_overflow_compaction_retries": 0,
        }
        tools_used: list[str] = []
        turn_id = f"turn_{uuid.uuid4().hex[:12]}"
        event_sequence = 0

        async def _emit_event(payload: dict[str, Any]) -> None:
            nonlocal event_sequence
            if not on_event:
                return
            event_sequence += 1
            event = cast(TurnEventPayload, {
                "turn_id": turn_id,
                "sequence": event_sequence,
                "timestamp_ms": int(time.time() * 1000),
                "source": event_source,
                **payload,
            })
            await on_event(event)

        if on_event:
            await _emit_event({
                "type": TURN_EVENT_TURN_START,
                "initial_message_count": len(initial_messages),
                "max_iterations": self.max_iterations,
            })

        while iteration < self.max_iterations:
            iteration += 1
            messages, current_turn_start = self.guard_loop_messages(messages, current_turn_start)

            response, messages, current_turn_start, chat_retry_stats = await self._chat_with_retries(
                messages=messages,
                current_turn_start=current_turn_start,
            )
            for key, value in chat_retry_stats.items():
                retry_stats[key] = retry_stats.get(key, 0) + int(value)

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
                            "type": TURN_EVENT_TOOL_START,
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
                            "type": TURN_EVENT_TOOL_END,
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
                    if should_interrupt_after_tool is not None:
                        should_interrupt = should_interrupt_after_tool()
                        if isinstance(should_interrupt, bool):
                            decision: dict[str, Any] = {"interrupt": should_interrupt}
                        else:
                            resolved = await should_interrupt
                            if isinstance(resolved, bool):
                                decision = {"interrupt": resolved}
                            elif isinstance(resolved, dict):
                                decision = {**resolved}
                                decision.setdefault("interrupt", True)
                            else:
                                decision = {"interrupt": bool(resolved)}
                        interrupt_now = bool(decision.get("interrupt"))
                        if interrupt_now:
                            interrupted_for_followup = True
                            interruption_info = {
                                "reason": str(decision.get("reason") or "pending_followup"),
                                "pending_followup_count": decision.get("pending_followup_count"),
                                "next_followup_preview": decision.get("next_followup_preview"),
                                "interrupted_at_iteration": iteration,
                                "interrupted_after_tool": tool_call.name,
                            }
                            preview = interruption_info.get("next_followup_preview")
                            pending_count = interruption_info.get("pending_followup_count")
                            if isinstance(preview, str) and preview:
                                final_content = (
                                    "A newer message arrived, so I paused this task and will handle it next: "
                                    f"{preview}"
                                )
                            elif isinstance(pending_count, int) and pending_count > 0:
                                final_content = (
                                    "A newer message arrived, so I paused this task and will handle the next "
                                    f"queued message now ({pending_count} waiting)."
                                )
                            else:
                                final_content = (
                                    "A newer message arrived, so I paused this task and will handle the newer message next."
                                )
                            logger.info(
                                "Turn interrupted for pending follow-up",
                                tool=tool_call.name,
                                iteration=iteration,
                                pending_followup_count=interruption_info.get("pending_followup_count"),
                                next_followup_preview=interruption_info.get("next_followup_preview"),
                            )
                            break
                if interrupted_for_followup:
                    break
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
            end_event: dict[str, Any] = {
                "type": TURN_EVENT_TURN_END,
                "iterations": iteration,
                "tool_count": len(tools_used),
                "completed": final_content is not None,
                "interrupted_for_followup": interrupted_for_followup,
                "max_iterations_reached": iteration >= self.max_iterations and final_content is not None and (
                    final_content.startswith("I reached the maximum number of tool call iterations")
                ),
            }
            if interrupted_for_followup:
                if interruption_info.get("reason"):
                    end_event["interruption_reason"] = interruption_info["reason"]
                if interruption_info.get("interrupted_at_iteration") is not None:
                    end_event["interrupted_at_iteration"] = interruption_info["interrupted_at_iteration"]
                if interruption_info.get("interrupted_after_tool"):
                    end_event["interrupted_after_tool"] = interruption_info["interrupted_after_tool"]
                if interruption_info.get("pending_followup_count") is not None:
                    end_event["pending_followup_count"] = interruption_info["pending_followup_count"]
                if interruption_info.get("next_followup_preview"):
                    end_event["next_followup_preview"] = interruption_info["next_followup_preview"]
            for key, value in retry_stats.items():
                if value:
                    end_event[key] = value
            await _emit_event(end_event)

        return final_content, tools_used, messages
