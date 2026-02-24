"""Context builder for assembling agent prompts."""

import base64
import io
import json
import mimetypes
import platform
from pathlib import Path
from typing import Any

from nanobot.agent.memory import MemoryStore
from nanobot.agent.skills import SkillsLoader

# Lazy-loaded tiktoken encoder; None if tiktoken is not installed.
_tiktoken_encoder: Any = None
_tiktoken_loaded: bool = False


def _get_encoder() -> Any:
    """Return a tiktoken encoder, or None if tiktoken is unavailable."""
    global _tiktoken_encoder, _tiktoken_loaded
    if _tiktoken_loaded:
        return _tiktoken_encoder
    _tiktoken_loaded = True
    try:
        import tiktoken
        # cl100k_base covers GPT-4, Claude (approximate), and most modern models.
        _tiktoken_encoder = tiktoken.get_encoding("cl100k_base")
    except Exception:
        _tiktoken_encoder = None
    return _tiktoken_encoder


def count_tokens(text: str) -> int:
    """Count tokens in *text*. Falls back to char/4 estimate if tiktoken is absent."""
    enc = _get_encoder()
    if enc is not None:
        return len(enc.encode(text))
    return max(1, len(text) // 4)


class ContextBuilder:
    """
    Builds the context (system prompt + messages) for the agent.

    Assembles bootstrap files, memory, skills, and conversation history
    into a coherent prompt for the LLM.
    """

    BOOTSTRAP_FILES = ["AGENTS.md", "SOUL.md", "USER.md", "TOOLS.md", "IDENTITY.md"]

    # Rough char-to-token ratio used only when tiktoken is unavailable
    _CHARS_PER_TOKEN = 4
    # Default conservative context budget (tokens, not chars)
    _DEFAULT_MAX_CONTEXT_TOKENS = 30_000

    def __init__(self, workspace: Path, max_context_tokens: int | None = None):
        self.workspace = workspace
        self.memory = MemoryStore(workspace)
        self.skills = SkillsLoader(workspace)
        self._max_context_tokens = max_context_tokens or self._DEFAULT_MAX_CONTEXT_TOKENS
    
    def build_system_prompt(self, skill_names: list[str] | None = None) -> str:
        """
        Build the system prompt from bootstrap files, memory, and skills.
        
        Args:
            skill_names: Optional list of skills to include.
        
        Returns:
            Complete system prompt.
        """
        parts = []
        
        # Core identity
        parts.append(self._get_identity())
        
        # Bootstrap files
        bootstrap = self._load_bootstrap_files()
        if bootstrap:
            parts.append(bootstrap)
        
        # Memory context
        memory = self.memory.get_memory_context()
        if memory:
            parts.append(f"# Memory\n\n{memory}")
        
        # Skills - progressive loading
        # 1. Always-loaded skills: include full content
        always_skills = self.skills.get_always_skills()
        if always_skills:
            always_content = self.skills.load_skills_for_context(always_skills)
            if always_content:
                parts.append(f"# Active Skills\n\n{always_content}")
        
        # 2. Available skills: only show summary (agent uses read_file to load)
        skills_summary = self.skills.build_skills_summary()
        if skills_summary:
            parts.append(f"""# Skills

The following skills extend your capabilities. To use a skill, read its SKILL.md file using the read_file tool.
Skills with available="false" need dependencies installed first - you can try installing them with apt/brew.

{skills_summary}""")
        
        return "\n\n---\n\n".join(parts)
    
    def _get_identity(self) -> str:
        """Get the core identity section."""
        from datetime import datetime
        import time as _time
        now = datetime.now().strftime("%Y-%m-%d %H:%M (%A)")
        tz = _time.strftime("%Z") or "UTC"
        workspace_path = str(self.workspace.expanduser().resolve())
        system = platform.system()
        runtime = f"{'macOS' if system == 'Darwin' else system} {platform.machine()}, Python {platform.python_version()}"
        
        return f"""# nanobot 🐈

You are nanobot, a helpful AI assistant. 

## Current Time
{now} ({tz})

## Runtime
{runtime}

## Workspace
Your workspace is at: {workspace_path}
- Long-term memory: {workspace_path}/memory/MEMORY.md
- History log: {workspace_path}/memory/HISTORY.md (grep-searchable)
- Custom skills: {workspace_path}/skills/{{skill-name}}/SKILL.md

Reply directly with text for conversations. Only use the 'message' tool to send to a specific chat channel.

## Tool Call Guidelines
- Before calling tools, you may briefly state your intent (e.g. "Let me check that"), but NEVER predict or describe the expected result before receiving it.
- Before modifying a file, read it first to confirm its current content.
- Do not assume a file or directory exists — use list_dir or read_file to verify.
- After writing or editing a file, re-read it if accuracy matters.
- If a tool call fails, analyze the error before retrying with a different approach.

## Memory
- Remember important facts: write to {workspace_path}/memory/MEMORY.md
- Recall past events: grep {workspace_path}/memory/HISTORY.md"""
    
    def _load_bootstrap_files(self) -> str:
        """Load all bootstrap files from workspace."""
        parts = []
        
        for filename in self.BOOTSTRAP_FILES:
            file_path = self.workspace / filename
            if file_path.exists():
                content = file_path.read_text(encoding="utf-8")
                parts.append(f"## {filename}\n\n{content}")
        
        return "\n\n".join(parts) if parts else ""
    
    @staticmethod
    def _estimate_message_tokens(msg: dict[str, Any]) -> int:
        """Estimate token count of a message for context budgeting."""
        total = 0
        content = msg.get("content", "")
        if isinstance(content, str):
            total += count_tokens(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        total += count_tokens(block.get("text", ""))
                    elif block.get("type") == "image_url":
                        # base64 images: count chars/4 (tiktoken can't encode binary)
                        url = block.get("image_url", {}).get("url", "")
                        total += max(1, len(url) // 4)
        for key in ("tool_call_id", "name"):
            value = msg.get(key)
            if isinstance(value, str):
                total += count_tokens(value)
        tool_calls = msg.get("tool_calls")
        if isinstance(tool_calls, list):
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    total += count_tokens(str(tc))
                    continue
                for key in ("id", "type"):
                    value = tc.get(key)
                    if isinstance(value, str):
                        total += count_tokens(value)
                fn = tc.get("function")
                if isinstance(fn, dict):
                    fn_name = fn.get("name")
                    if isinstance(fn_name, str):
                        total += count_tokens(fn_name)
                    args = fn.get("arguments")
                    if isinstance(args, str):
                        total += count_tokens(args)
                    elif args is not None:
                        total += count_tokens(json.dumps(args, ensure_ascii=False))
        return total

    def _trim_history(
        self, history: list[dict[str, Any]], budget_tokens: int
    ) -> list[dict[str, Any]]:
        """Trim history by user-turn chunks to avoid breaking tool-call structure."""
        if not history:
            return history
        if budget_tokens <= 0:
            return []
        history = self._drop_leading_non_user(history)
        if not history:
            return []
        total = sum(self._estimate_message_tokens(m) for m in history)
        if total <= budget_tokens:
            return history

        chunks = self._split_history_chunks(history)
        kept_reversed: list[list[dict[str, Any]]] = []
        kept_total = 0
        for chunk in reversed(chunks):
            chunk_tokens = sum(self._estimate_message_tokens(m) for m in chunk)
            if kept_total + chunk_tokens > budget_tokens:
                break
            kept_reversed.append(chunk)
            kept_total += chunk_tokens

        if not kept_reversed:
            return []
        return [m for chunk in reversed(kept_reversed) for m in chunk]

    _ERROR_PREFIXES = ("Error calling LLM:", "error:", "Error:")

    @staticmethod
    def _msg_text(msg: dict[str, Any]) -> str:
        """Extract plain text from a message content."""
        c = msg.get("content", "")
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            return " ".join(
                b.get("text", "") for b in c
                if isinstance(b, dict) and b.get("type") == "text"
            )
        return ""

    @staticmethod
    def _is_tool_protocol_message(msg: dict[str, Any]) -> bool:
        """Return True for messages that carry tool-call protocol semantics."""
        if msg.get("role") == "tool":
            return True
        return bool(msg.get("tool_calls")) or "tool_call_id" in msg

    def _can_dedupe_message(self, msg: dict[str, Any]) -> bool:
        """Only dedupe plain user/assistant text messages."""
        role = msg.get("role")
        if role == "user":
            return True
        if role != "assistant":
            return False
        return not self._is_tool_protocol_message(msg)

    def _can_merge_assistant_messages(
        self, prev: dict[str, Any], curr: dict[str, Any]
    ) -> bool:
        """Only merge consecutive plain-text assistant messages."""
        if prev.get("role") != "assistant" or curr.get("role") != "assistant":
            return False
        if self._is_tool_protocol_message(prev) or self._is_tool_protocol_message(curr):
            return False
        return isinstance(prev.get("content"), str) and isinstance(curr.get("content"), str)

    @staticmethod
    def _drop_leading_non_user(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Drop malformed leading history until the first user message."""
        for i, msg in enumerate(history):
            if msg.get("role") == "user":
                return history[i:]
        return []

    @staticmethod
    def _split_history_chunks(history: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        """Split history into chunks anchored by user messages."""
        chunks: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        for msg in history:
            if msg.get("role") == "user" and current:
                chunks.append(current)
                current = [msg]
            else:
                current.append(msg)
        if current:
            chunks.append(current)
        return chunks

    # Maximum number of conversation turns (user+assistant pairs) to keep
    _MAX_HISTORY_TURNS = 20
    _ASSISTANT_SUMMARY_CHARS = 300

    def _compact_history(self, history: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Compact history: sliding window, remove errors, deduplicate, merge same-role, truncate."""
        if not history:
            return history

        # 0. Sliding window: keep only the last N turns
        user_indices = [i for i, m in enumerate(history) if m.get("role") == "user"]
        if len(user_indices) > self._MAX_HISTORY_TURNS:
            start = user_indices[-self._MAX_HISTORY_TURNS]
            history = history[start:]

        # 1. Remove assistant messages that are just error echoes
        cleaned = [
            m for m in history
            if not (
                m.get("role") == "assistant"
                and not self._is_tool_protocol_message(m)
                and any(self._msg_text(m).startswith(p) for p in self._ERROR_PREFIXES)
            )
        ]

        # 2. Truncate long assistant replies to summary length
        truncated = []
        for m in cleaned:
            if (
                m.get("role") == "assistant"
                and not self._is_tool_protocol_message(m)
                and isinstance(m.get("content"), str)
            ):
                if len(m["content"]) > self._ASSISTANT_SUMMARY_CHARS:
                    truncated.append({**m, "content": m["content"][:self._ASSISTANT_SUMMARY_CHARS] + "\n... (truncated)"})
                    continue
            truncated.append(m)

        # 3. Deduplicate consecutive identical messages
        deduped: list[dict[str, Any]] = []
        for m in truncated:
            if deduped and deduped[-1].get("role") == m.get("role"):
                if self._can_dedupe_message(deduped[-1]) and self._can_dedupe_message(m):
                    prev_text = self._msg_text(deduped[-1])
                    curr_text = self._msg_text(m)
                    if prev_text == curr_text:
                        continue
            deduped.append(m)

        # 4. Handle consecutive same-role messages
        #    - consecutive user messages: keep only the last one (earlier ones are abandoned questions)
        #    - consecutive plain assistant messages: merge them
        merged: list[dict[str, Any]] = []
        for m in deduped:
            if merged and merged[-1].get("role") == m.get("role"):
                if m.get("role") == "user":
                    # Replace previous user msg — it was never answered
                    merged[-1] = m
                elif self._can_merge_assistant_messages(merged[-1], m):
                    prev_text = self._msg_text(merged[-1])
                    curr_text = self._msg_text(m)
                    merged[-1] = {**merged[-1], "content": prev_text + "\n" + curr_text}
                else:
                    merged.append(m)
            else:
                merged.append(m)

        return self._drop_leading_non_user(merged)

    def build_messages(
        self,
        history: list[dict[str, Any]],
        current_message: str,
        skill_names: list[str] | None = None,
        media: list[str] | None = None,
        channel: str | None = None,
        chat_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Build the complete message list for an LLM call.

        Args:
            history: Previous conversation messages.
            current_message: The new user message.
            skill_names: Optional skills to include.
            media: Optional list of local file paths for images/media.
            channel: Current channel (telegram, feishu, etc.).
            chat_id: Current chat/user ID.

        Returns:
            List of messages including system prompt.
        """
        messages = []

        # System prompt
        system_prompt = self.build_system_prompt(skill_names)
        if channel and chat_id:
            system_prompt += f"\n\n## Current Session\nChannel: {channel}\nChat ID: {chat_id}"
        messages.append({"role": "system", "content": system_prompt})

        # Build current message first so image payload size can be counted in budgeting.
        user_content = self._build_user_content(current_message, media)

        # Budget for history = total budget minus system prompt and current message (tokens)
        system_tokens = count_tokens(system_prompt)
        current_tokens = self._estimate_message_tokens({"content": user_content})
        # Reserve 4096 tokens for the LLM reply
        budget_tokens = self._max_context_tokens - system_tokens - current_tokens - 4096
        budget_tokens = max(budget_tokens, 0)

        # History (compact then trim to fit budget)
        compacted = self._compact_history(history)
        trimmed = self._trim_history(compacted, budget_tokens)

        # Drop trailing user message in history if it was never answered —
        # the current message supersedes it, avoids consecutive user messages.
        if trimmed and trimmed[-1].get("role") == "user":
            trimmed.pop()

        messages.extend(trimmed)

        # Current message (with optional image attachments)
        messages.append({"role": "user", "content": user_content})

        return messages

    # Max dimension (px) and file size (bytes) for images sent to LLM
    _IMAGE_MAX_DIM = 1024
    _IMAGE_MAX_BYTES = 200_000  # ~200 KB after compression

    def _compress_image(self, path: Path) -> tuple[str, str] | None:
        """Resize and compress an image, return (base64_str, mime_type) or None."""
        try:
            from PIL import Image
        except ImportError:
            # Fallback: read raw but cap file size
            raw = path.read_bytes()
            if len(raw) > self._IMAGE_MAX_BYTES * 3:
                return None  # too large without PIL
            mime, _ = mimetypes.guess_type(str(path))
            return base64.b64encode(raw).decode(), mime or "image/jpeg"

        try:
            img = Image.open(path)
            img.thumbnail((self._IMAGE_MAX_DIM, self._IMAGE_MAX_DIM))
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            buf = io.BytesIO()
            quality = 85
            img.save(buf, format="JPEG", quality=quality)
            # Reduce quality if still too large
            while buf.tell() > self._IMAGE_MAX_BYTES and quality > 30:
                quality -= 15
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=quality)
            return base64.b64encode(buf.getvalue()).decode(), "image/jpeg"
        except Exception:
            return None

    def _build_user_content(self, text: str, media: list[str] | None) -> str | list[dict[str, Any]]:
        """Build user message content with optional compressed images."""
        if not media:
            return text

        images = []
        for path in media:
            p = Path(path)
            mime, _ = mimetypes.guess_type(path)
            if not p.is_file() or not mime or not mime.startswith("image/"):
                continue
            result = self._compress_image(p)
            if result:
                b64, img_mime = result
                images.append({"type": "image_url", "image_url": {"url": f"data:{img_mime};base64,{b64}"}})

        if not images:
            return text
        return images + [{"type": "text", "text": text}]
    
    def add_tool_result(
        self,
        messages: list[dict[str, Any]],
        tool_call_id: str,
        tool_name: str,
        result: str
    ) -> list[dict[str, Any]]:
        """
        Add a tool result to the message list.
        
        Args:
            messages: Current message list.
            tool_call_id: ID of the tool call.
            tool_name: Name of the tool.
            result: Tool execution result.
        
        Returns:
            Updated message list.
        """
        messages.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "name": tool_name,
            "content": result
        })
        return messages
    
    def add_assistant_message(
        self,
        messages: list[dict[str, Any]],
        content: str | None,
        tool_calls: list[dict[str, Any]] | None = None,
        reasoning_content: str | None = None,
    ) -> list[dict[str, Any]]:
        """
        Add an assistant message to the message list.
        
        Args:
            messages: Current message list.
            content: Message content.
            tool_calls: Optional tool calls.
            reasoning_content: Thinking output (Kimi, DeepSeek-R1, etc.).
        
        Returns:
            Updated message list.
        """
        msg: dict[str, Any] = {"role": "assistant"}

        # Always include content — some providers (e.g. StepFun) reject
        # assistant messages that omit the key entirely.
        msg["content"] = content

        if tool_calls:
            msg["tool_calls"] = tool_calls

        # Include reasoning content when provided (required by some thinking models)
        if reasoning_content is not None:
            msg["reasoning_content"] = reasoning_content

        messages.append(msg)
        return messages
