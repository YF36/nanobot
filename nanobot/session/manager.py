"""Session management for conversation history."""

import json
import shutil
import time
from pathlib import Path
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from nanobot.logging import get_logger

from nanobot.utils.helpers import ensure_dir, safe_filename

logger = get_logger(__name__)


@dataclass
class Session:
    """
    A conversation session.

    Stores messages in JSONL format for easy reading and persistence.

    Important: Messages are append-only for LLM cache efficiency.
    The consolidation process writes summaries to MEMORY.md/HISTORY.md
    but does NOT modify the messages list or get_history() output.
    """

    key: str  # channel:chat_id
    messages: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    metadata: dict[str, Any] = field(default_factory=dict)
    last_consolidated: int = 0  # Number of messages already consolidated to files
    
    def add_message(self, role: str, content: str, **kwargs: Any) -> None:
        """Add a message to the session."""
        msg = {
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat(),
            **kwargs
        }
        self.messages.append(msg)
        self.updated_at = datetime.now()
    
    def get_history(self, max_messages: int = 500) -> list[dict[str, Any]]:
        """Get recent messages in LLM format, preserving tool metadata."""
        out: list[dict[str, Any]] = []
        for m in self.messages[-max_messages:]:
            entry: dict[str, Any] = {"role": m["role"], "content": m.get("content", "")}
            for k in ("tool_calls", "tool_call_id", "name"):
                if k in m:
                    entry[k] = m[k]
            out.append(entry)
        return out
    
    def clear(self) -> None:
        """Clear all messages and reset session to initial state."""
        self.messages = []
        self.last_consolidated = 0
        self.updated_at = datetime.now()


class SessionManager:
    """
    Manages conversation sessions.

    Sessions are stored as JSONL files in the sessions directory.
    """

    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.sessions_dir = ensure_dir(self.workspace / "sessions")
        self.legacy_sessions_dir = Path.home() / ".nanobot" / "sessions"
        self._cache: dict[str, Session] = {}
        self._persisted_signatures: dict[str, str] = {}
        self._save_writes = 0
        self._save_skips = 0
    
    def _get_session_path(self, key: str) -> Path:
        """Get the file path for a session."""
        safe_key = safe_filename(key.replace(":", "_"))
        return self.sessions_dir / f"{safe_key}.jsonl"

    def _get_legacy_session_path(self, key: str) -> Path:
        """Legacy global session path (~/.nanobot/sessions/)."""
        safe_key = safe_filename(key.replace(":", "_"))
        return self.legacy_sessions_dir / f"{safe_key}.jsonl"
    
    def get_or_create(self, key: str) -> Session:
        """
        Get an existing session or create a new one.
        
        Args:
            key: Session key (usually channel:chat_id).
        
        Returns:
            The session.
        """
        if key in self._cache:
            return self._cache[key]
        
        loaded = self._load(key)
        session = loaded or Session(key=key)
        
        self._cache[key] = session
        if loaded is not None:
            self._persisted_signatures[key] = self._persist_signature(session)
        return session
    
    def _load(self, key: str) -> Session | None:
        """Load a session from disk."""
        path = self._get_session_path(key)
        if not path.exists():
            legacy_path = self._get_legacy_session_path(key)
            if legacy_path.exists():
                try:
                    shutil.move(str(legacy_path), str(path))
                    logger.info("Migrated session from legacy path", session_key=key)
                except Exception:
                    logger.exception("Failed to migrate session", session_key=key)

        if not path.exists():
            return None

        try:
            messages = []
            metadata = {}
            created_at = None
            updated_at = None
            last_consolidated = 0

            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue

                    data = json.loads(line)

                    if data.get("_type") == "metadata":
                        metadata = data.get("metadata", {})
                        created_at = datetime.fromisoformat(data["created_at"]) if data.get("created_at") else None
                        updated_at = datetime.fromisoformat(data["updated_at"]) if data.get("updated_at") else None
                        last_consolidated = data.get("last_consolidated", 0)
                    else:
                        messages.append(data)

            return Session(
                key=key,
                messages=messages,
                created_at=created_at or datetime.now(),
                updated_at=updated_at or datetime.now(),
                metadata=metadata,
                last_consolidated=last_consolidated
            )
        except Exception as e:
            logger.warning("Failed to load session", session_key=key, error=str(e))
            return None

    @staticmethod
    def _persist_signature(session: Session) -> str:
        """Compute a compact signature for persisted session content."""
        metadata_json = json.dumps(session.metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        last_msg_json = (
            json.dumps(session.messages[-1], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if session.messages else ""
        )
        updated_at = (
            session.updated_at.isoformat() if isinstance(session.updated_at, datetime) else str(session.updated_at)
        )
        created_at = (
            session.created_at.isoformat() if isinstance(session.created_at, datetime) else str(session.created_at)
        )
        return "|".join((
            session.key,
            created_at,
            updated_at,
            str(session.last_consolidated),
            str(len(session.messages)),
            metadata_json,
            last_msg_json,
        ))

    @staticmethod
    def _write_session_file(path: Path, session: Session) -> None:
        """Write the full session JSONL snapshot to disk."""
        with open(path, "w", encoding="utf-8") as f:
            metadata_line = {
                "_type": "metadata",
                "key": session.key,
                "created_at": session.created_at.isoformat(),
                "updated_at": session.updated_at.isoformat(),
                "metadata": session.metadata,
                "last_consolidated": session.last_consolidated
            }
            f.write(json.dumps(metadata_line, ensure_ascii=False) + "\n")
            for msg in session.messages:
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")
    
    def save(self, session: Session) -> None:
        """Save a session to disk."""
        path = self._get_session_path(session.key)
        started = time.perf_counter()
        signature = self._persist_signature(session)
        if path.exists() and self._persisted_signatures.get(session.key) == signature:
            self._save_skips += 1
            elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
            logger.debug(
                "session_save_skipped",
                session_key=session.key,
                message_count=len(session.messages),
                last_consolidated=session.last_consolidated,
                elapsed_ms=elapsed_ms,
                save_writes=self._save_writes,
                save_skips=self._save_skips,
            )
            self._cache[session.key] = session
            return

        self._write_session_file(path, session)
        self._save_writes += 1
        elapsed_ms = round((time.perf_counter() - started) * 1000, 3)

        self._cache[session.key] = session
        self._persisted_signatures[session.key] = signature
        logger.debug(
            "session_save_written",
            session_key=session.key,
            message_count=len(session.messages),
            last_consolidated=session.last_consolidated,
            elapsed_ms=elapsed_ms,
            file_bytes=path.stat().st_size if path.exists() else None,
            save_writes=self._save_writes,
            save_skips=self._save_skips,
        )
    
    def invalidate(self, key: str) -> None:
        """Remove a session from the in-memory cache."""
        self._cache.pop(key, None)
        self._persisted_signatures.pop(key, None)
    
    def list_sessions(self) -> list[dict[str, Any]]:
        """
        List all sessions.
        
        Returns:
            List of session info dicts.
        """
        sessions = []
        
        for path in self.sessions_dir.glob("*.jsonl"):
            try:
                # Read just the metadata line
                with open(path, encoding="utf-8") as f:
                    first_line = f.readline().strip()
                    if first_line:
                        data = json.loads(first_line)
                        if data.get("_type") == "metadata":
                            key = data.get("key") or path.stem.replace("_", ":", 1)
                            sessions.append({
                                "key": key,
                                "created_at": data.get("created_at"),
                                "updated_at": data.get("updated_at"),
                                "path": str(path)
                            })
            except Exception:
                continue
        
        return sorted(sessions, key=lambda x: x.get("updated_at", ""), reverse=True)
