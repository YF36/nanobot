"""Memory system for persistent agent memory."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from nanobot.logging import get_logger

from nanobot.utils.helpers import ensure_dir

if TYPE_CHECKING:
    from nanobot.providers.base import LLMProvider
    from nanobot.session.manager import Session

logger = get_logger(__name__)


_SAVE_MEMORY_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "save_memory",
            "description": "Save the memory consolidation result to persistent storage.",
            "parameters": {
                "type": "object",
                "properties": {
                    "history_entry": {
                        "type": "string",
                        "description": "A paragraph (2-5 sentences) summarizing key events/decisions/topics. "
                        "Start with [YYYY-MM-DD HH:MM]. Include detail useful for grep search.",
                    },
                    "memory_update": {
                        "type": "string",
                        "description": "Full updated long-term memory as markdown. Include all existing "
                        "facts plus new ones. Return unchanged if nothing new.",
                    },
                },
                "required": ["history_entry", "memory_update"],
            },
        },
    }
]


class MemoryStore:
    """Two-layer memory: MEMORY.md (long-term facts) + HISTORY.md (grep-searchable log)."""

    _CHARS_PER_TOKEN = 4
    _CONSOLIDATION_REPLY_RESERVE_TOKENS = 4096
    _CONSOLIDATION_SOFT_INPUT_TOKENS = 24_000
    _CONSOLIDATION_TOOLCALL_RETRIES = 1
    _MEMORY_TRUNCATION_NOTICE = "\n\n[... long-term memory truncated for consolidation ...]\n\n"

    def __init__(self, workspace: Path):
        self.memory_dir = ensure_dir(workspace / "memory")
        self.memory_file = self.memory_dir / "MEMORY.md"
        self.history_file = self.memory_dir / "HISTORY.md"

    def read_long_term(self) -> str:
        if self.memory_file.exists():
            return self.memory_file.read_text(encoding="utf-8")
        return ""

    def write_long_term(self, content: str) -> None:
        self.memory_file.write_text(content, encoding="utf-8")

    def append_history(self, entry: str) -> None:
        with open(self.history_file, "a", encoding="utf-8") as f:
            f.write(entry.rstrip() + "\n\n")

    def get_memory_context(self) -> str:
        long_term = self.read_long_term()
        return f"## Long-term Memory\n{long_term}" if long_term else ""

    @classmethod
    def _estimate_tokens(cls, text: str) -> int:
        """Coarse token estimate for memory consolidation budgeting."""
        return max(1, len(text) // cls._CHARS_PER_TOKEN) if text else 0

    @staticmethod
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

    def _format_consolidation_lines(self, messages: list[dict]) -> list[str]:
        lines: list[str] = []
        for m in messages:
            if not m.get("content"):
                continue
            tools = f" [tools: {', '.join(m['tools_used'])}]" if m.get("tools_used") else ""
            lines.append(f"[{m.get('timestamp', '?')[:16]}] {m['role'].upper()}{tools}: {m['content']}")
        return lines

    def _build_consolidation_prompt(self, current_memory: str, lines: list[str]) -> str:
        return f"""Process this conversation and call the save_memory tool with your consolidation.

## Current Long-term Memory
{current_memory or "(empty)"}

## Conversation to Process
{chr(10).join(lines)}"""

    @staticmethod
    def _consolidation_system_prompt(strict_tool_call: bool = False) -> str:
        base = "You are a memory consolidation agent. Call the save_memory tool with your consolidation of the conversation."
        if not strict_tool_call:
            return base
        return (
            base
            + " Do not reply with plain text. You MUST call save_memory exactly once "
              "with both history_entry and memory_update."
        )

    def _fit_chunk_by_soft_budget(
        self,
        messages: list[dict],
        current_memory: str,
    ) -> list[dict]:
        """Select a prefix chunk that fits a conservative input budget."""
        if not messages:
            return []
        # Prompt scaffolding estimate plus current memory
        budget_memory, _ = self._fit_memory_context_by_soft_budget(current_memory, [])
        scaffold = self._build_consolidation_prompt(budget_memory, [])
        budget = max(
            1,
            self._CONSOLIDATION_SOFT_INPUT_TOKENS - self._estimate_tokens(scaffold) - self._CONSOLIDATION_REPLY_RESERVE_TOKENS,
        )
        total = 0
        chunk: list[dict] = []
        for m in messages:
            lines = self._format_consolidation_lines([m])
            delta = self._estimate_tokens("\n".join(lines)) if lines else 1
            if chunk and total + delta > budget:
                break
            chunk.append(m)
            total += delta
        return chunk or messages[:1]

    def _fit_memory_context_by_soft_budget(
        self,
        current_memory: str,
        lines: list[str],
    ) -> tuple[str, bool]:
        """Trim long-term memory context to fit the consolidation input budget.

        Returns (memory_for_prompt, was_truncated).
        """
        if not current_memory:
            return current_memory, False

        # Available input budget after accounting for scaffold, conversation chunk, and reply reserve.
        prompt_without_memory = self._build_consolidation_prompt("", lines)
        available_tokens = (
            self._CONSOLIDATION_SOFT_INPUT_TOKENS
            - self._CONSOLIDATION_REPLY_RESERVE_TOKENS
            - self._estimate_tokens(prompt_without_memory)
        )
        if available_tokens <= 0:
            return "", True

        if self._estimate_tokens(current_memory) <= available_tokens:
            return current_memory, False

        # Keep a head+tail slice so stable section headers and recent facts both survive.
        max_chars = max(64, available_tokens * self._CHARS_PER_TOKEN)
        notice = self._MEMORY_TRUNCATION_NOTICE
        room = max_chars - len(notice)
        if room <= 0:
            return notice.strip(), True

        head_chars = max(1, room // 2)
        tail_chars = max(1, room - head_chars)
        trimmed = current_memory[:head_chars] + notice + current_memory[-tail_chars:]

        # Tighten if coarse char->token estimate still overshoots.
        while self._estimate_tokens(trimmed) > available_tokens and (head_chars > 16 or tail_chars > 16):
            head_chars = max(16, int(head_chars * 0.85))
            tail_chars = max(16, int(tail_chars * 0.85))
            trimmed = current_memory[:head_chars] + notice + current_memory[-tail_chars:]

        return trimmed, True

    async def consolidate(
        self,
        session: Session,
        provider: LLMProvider,
        model: str,
        *,
        archive_all: bool = False,
        memory_window: int = 50,
    ) -> bool:
        """Consolidate old messages into MEMORY.md + HISTORY.md via LLM tool call.

        Returns True on success (including no-op), False on failure.
        """
        # Snapshot length before the LLM call so new messages appended
        # concurrently don't shift the last_consolidated boundary.
        snapshot_len = len(session.messages)

        if archive_all:
            old_messages = session.messages[:snapshot_len]
            keep_count = 0
            logger.info("Memory consolidation (archive_all)", message_count=snapshot_len)
        else:
            keep_count = memory_window // 2
            if snapshot_len <= keep_count:
                return True
            if snapshot_len - session.last_consolidated <= 0:
                return True
            old_messages = session.messages[session.last_consolidated:snapshot_len - keep_count]
            if not old_messages:
                return True
            logger.info("Memory consolidation", to_consolidate=len(old_messages), keep=keep_count)

        start_index = session.last_consolidated
        target_last = 0 if archive_all else snapshot_len - keep_count
        pending = list(old_messages)
        processed_count = 0

        try:
            while pending:
                current_memory = self.read_long_term()
                chunk = self._fit_chunk_by_soft_budget(pending, current_memory)
                if not chunk:
                    break

                # Retry with smaller prefixes if provider still reports context overflow.
                while True:
                    lines = self._format_consolidation_lines(chunk)
                    if not lines:
                        # Nothing useful to summarize; just mark the messages as processed.
                        processed_count += len(chunk)
                        pending = pending[len(chunk):]
                        if not archive_all:
                            session.last_consolidated = min(target_last, start_index + processed_count)
                        break

                    prompt_memory, memory_truncated = self._fit_memory_context_by_soft_budget(current_memory, lines)
                    if memory_truncated:
                        logger.warning(
                            "Memory consolidation prompt truncating long-term memory context",
                            memory_chars=len(current_memory),
                            prompt_memory_chars=len(prompt_memory),
                            chunk_messages=len(chunk),
                        )
                    prompt = self._build_consolidation_prompt(prompt_memory, lines)
                    response = None
                    for attempt in range(self._CONSOLIDATION_TOOLCALL_RETRIES + 1):
                        strict_retry = attempt > 0
                        response = await provider.chat(
                            messages=[
                                {"role": "system", "content": self._consolidation_system_prompt(strict_tool_call=strict_retry)},
                                {"role": "user", "content": prompt},
                            ],
                            tools=_SAVE_MEMORY_TOOL,
                            model=model,
                            temperature=0.0,
                        )

                        if response.has_tool_calls:
                            break
                        if getattr(response, "finish_reason", "") == "error":
                            break
                        if attempt < self._CONSOLIDATION_TOOLCALL_RETRIES:
                            logger.warning(
                                "Memory consolidation response missing save_memory tool call, retrying",
                                retry=attempt + 1,
                            )

                    assert response is not None

                    if getattr(response, "finish_reason", "") == "error" and self._is_context_length_error(response.content):
                        if len(chunk) <= 1:
                            logger.warning("Memory consolidation failed: prompt exceeds context even for single message")
                            return False
                        chunk = chunk[: max(1, len(chunk) // 2)]
                        continue

                    if not response.has_tool_calls:
                        if getattr(response, "finish_reason", "") == "error":
                            logger.warning("Memory consolidation LLM call failed", error=response.content or "(empty)")
                        else:
                            logger.warning("Memory consolidation: LLM did not call save_memory, skipping")
                        return False

                    args = response.tool_calls[0].arguments
                    if entry := args.get("history_entry"):
                        if not isinstance(entry, str):
                            entry = json.dumps(entry, ensure_ascii=False)
                        self.append_history(entry)
                    if update := args.get("memory_update"):
                        if not isinstance(update, str):
                            update = json.dumps(update, ensure_ascii=False)
                        if memory_truncated:
                            logger.warning(
                                "Skipping memory_update write because long-term memory context was truncated",
                                current_memory_chars=len(current_memory),
                                returned_memory_chars=len(update),
                            )
                        elif update != current_memory:
                            self.write_long_term(update)

                    processed_count += len(chunk)
                    pending = pending[len(chunk):]
                    if not archive_all:
                        # Process at most one chunk per normal pass to keep latency bounded.
                        session.last_consolidated = min(target_last, start_index + processed_count)
                        logger.info(
                            "Memory consolidation done",
                            snapshot_len=snapshot_len,
                            last_consolidated=session.last_consolidated,
                            processed_messages=processed_count,
                            partial=(session.last_consolidated < target_last),
                        )
                        return True
                    break

            if archive_all:
                session.last_consolidated = 0
            else:
                session.last_consolidated = min(target_last, start_index + processed_count)
            logger.info(
                "Memory consolidation done",
                snapshot_len=snapshot_len,
                last_consolidated=session.last_consolidated,
                processed_messages=processed_count,
                partial=(not archive_all and session.last_consolidated < target_last),
            )
            return True
        except Exception:
            logger.exception("Memory consolidation failed")
            return False
