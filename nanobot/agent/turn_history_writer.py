"""Persist and normalize turn history entries for sessions."""

from __future__ import annotations

from datetime import datetime
from typing import Any


class TurnHistoryWriter:
    """Save turn messages into session history with truncation/sanitization."""

    def __init__(
        self,
        *,
        tool_result_max_chars: int = 500,
        assistant_history_max_chars: int = 300,
    ) -> None:
        self.tool_result_max_chars = tool_result_max_chars
        self.assistant_history_max_chars = assistant_history_max_chars

    @staticmethod
    def strip_images_from_content(content: Any) -> Any:
        """Replace base64 image blocks with a lightweight placeholder."""
        if not isinstance(content, list):
            return content
        stripped: list[dict] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "image_url":
                stripped.append({"type": "text", "text": "[image]"})
            else:
                stripped.append(block)
        texts = [b["text"] for b in stripped if isinstance(b, dict) and b.get("type") == "text"]
        if len(texts) == len(stripped):
            return " ".join(texts)
        return stripped

    def save_turn(self, session: Any, messages: list[dict], skip: int) -> None:
        """Save new-turn messages into session, truncating large content and stripping images."""
        for m in messages[skip:]:
            entry = {k: v for k, v in m.items() if k != "reasoning_content"}
            if "content" in entry:
                entry["content"] = self.strip_images_from_content(entry["content"])
            if entry.get("role") == "assistant" and isinstance(entry.get("content"), str):
                content = entry["content"]
                if len(content) > self.assistant_history_max_chars:
                    entry["content"] = content[:self.assistant_history_max_chars] + "\n... (truncated)"
            if entry.get("role") == "tool" and isinstance(entry.get("content"), str):
                content = entry["content"]
                if len(content) > self.tool_result_max_chars:
                    entry["content"] = content[:self.tool_result_max_chars] + "\n... (truncated)"
            entry.setdefault("timestamp", datetime.now().isoformat())
            session.messages.append(entry)
        session.updated_at = datetime.now()
