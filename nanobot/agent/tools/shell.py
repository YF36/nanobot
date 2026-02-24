"""Shell execution tool."""

import asyncio
import os
import re
import shlex
from pathlib import Path
from typing import Any, Callable

from nanobot.agent.tools.base import Tool
from nanobot.logging import get_logger

audit_log = get_logger("nanobot.audit")


class ShellOutputFormatter:
    """Format and truncate process output consistently."""

    def __init__(self, max_len: int = 10000):
        self.max_len = max_len

    def format(self, stdout: bytes | None, stderr: bytes | None, returncode: int | None) -> str:
        output_parts: list[str] = []

        if stdout:
            output_parts.append(stdout.decode("utf-8", errors="replace"))

        if stderr:
            stderr_text = stderr.decode("utf-8", errors="replace")
            if stderr_text.strip():
                output_parts.append(f"STDERR:\n{stderr_text}")

        if returncode not in (None, 0):
            output_parts.append(f"\nExit code: {returncode}")

        result = "\n".join(output_parts) if output_parts else "(no output)"
        if len(result) > self.max_len:
            result = result[: self.max_len] + f"\n... (truncated, {len(result) - self.max_len} more chars)"
        return result


class ShellExecutor:
    """Execute shell commands with timeout handling."""

    async def run(self, command: str, *, cwd: str, timeout: int, formatter: ShellOutputFormatter) -> str:
        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            process.kill()
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass
            return f"Error: Command timed out after {timeout} seconds"

        return formatter.format(stdout, stderr, process.returncode)


class ShellGuard:
    """Best-effort command safety checks."""

    def __init__(
        self,
        *,
        deny_patterns: list[str],
        allow_patterns: list[str],
        restrict_to_workspace: bool,
        audit_blocked: Callable[[str, str], None],
    ):
        self.deny_patterns = deny_patterns
        self.allow_patterns = allow_patterns
        self.restrict_to_workspace = restrict_to_workspace
        self._audit_blocked = audit_blocked

    @staticmethod
    def normalize_whitespace(cmd: str) -> str:
        """Replace tabs, newlines, and other exotic whitespace with plain spaces."""
        return re.sub(r"[\t\n\r\v\f]+", " ", cmd)

    def structural_check(self, command: str) -> str | None:
        """Shlex-based structural analysis of the command tokens."""
        HARD_REJECT = {"mkfs", "diskpart", "shutdown", "reboot", "poweroff"}
        REJECT_FIRST_TOKEN = {"sudo", "su", "eval", "exec"}

        try:
            tokens = shlex.split(command)
        except ValueError:
            return None

        if not tokens:
            return None

        first = os.path.basename(tokens[0])
        if first in HARD_REJECT:
            return f"Error: Command blocked by safety guard (dangerous command: {first})"
        if first in REJECT_FIRST_TOKEN:
            return f"Error: Command blocked by safety guard (dangerous command: {first})"

        if first == "rm" and len(tokens) > 1:
            for tok in tokens[1:]:
                if tok.startswith("-") and not tok.startswith("--"):
                    if "r" in tok or "f" in tok:
                        return "Error: Command blocked by safety guard (dangerous rm flags)"
                elif tok in ("--recursive", "--force"):
                    return "Error: Command blocked by safety guard (dangerous rm flags)"

        return None

    def guard(self, command: str, cwd: str) -> str | None:
        """Return error string if blocked, else None."""
        cmd = self.normalize_whitespace(command.strip())
        lower = cmd.lower()

        for pattern in self.deny_patterns:
            if re.search(pattern, lower):
                self._audit_blocked(command, f"deny_pattern matched: {pattern}")
                return "Error: Command blocked by safety guard (dangerous pattern detected)"

        if self.allow_patterns and not any(re.search(p, lower) for p in self.allow_patterns):
            self._audit_blocked(command, "not in allowlist")
            return "Error: Command blocked by safety guard (not in allowlist)"

        structural_error = self.structural_check(cmd)
        if structural_error:
            self._audit_blocked(command, "structural_check")
            return structural_error

        if self.restrict_to_workspace:
            if "..\\" in cmd or "../" in cmd:
                self._audit_blocked(command, "path traversal")
                return "Error: Command blocked by safety guard (path traversal detected)"

            cwd_path = Path(cwd).resolve()
            win_paths = re.findall(r"[A-Za-z]:\\[^\\\"']+", cmd)
            posix_paths = re.findall(r"(?:^|[\s|>])(/[^\s\"'>]+)", cmd)
            for raw in win_paths + posix_paths:
                try:
                    p = Path(raw.strip()).resolve()
                except Exception:
                    continue
                if p.is_absolute() and cwd_path not in p.parents and p != cwd_path:
                    self._audit_blocked(command, "path outside workspace")
                    return "Error: Command blocked by safety guard (path outside working dir)"

        return None


class ExecTool(Tool):
    """Tool to execute shell commands."""
    
    DEFAULT_DENY_PATTERNS = [
        # --- Destructive file operations ---
        r"\brm\s+(-[a-z]*[rf]|--(recursive|force))",  # rm -rf, rm -rfi, rm --recursive, etc.
        r"\bdel\s+/[fq]\b",              # del /f, del /q
        r"\brmdir\s+/s\b",               # rmdir /s
        # --- Disk / partition ---
        r"(?:^|[;&|]\s*)format\b",        # format (as standalone command)
        r"\b(mkfs|diskpart)\b",           # disk operations
        r"\bdd\s+if=",                    # dd
        r">\s*/dev/sd",                   # write to disk
        # --- System power ---
        r"\b(shutdown|reboot|poweroff)\b",
        # --- Fork bomb ---
        r":\(\)\s*\{.*\};\s*:",
        # --- Privilege escalation ---
        r"\bsudo\b",
        r"\bsu\s+-?\s*\w*",              # su / su - root
        # --- Ownership / permissions ---
        r"\bchmod\s+[0-7]*7[0-7]*\b",    # chmod 777 etc. (world-writable)
        r"\bchown\b",
        # --- Dynamic execution ---
        r"\beval\b",
        r"\bexec\b",
        # --- Remote code execution ---
        r"\b(curl|wget)\b.*\|\s*(ba)?sh\b",           # curl/wget pipe to sh
        r"\b(python|python3|perl|ruby|node)\s+-[ec]\b",  # interpreter one-liners
        # --- Command substitution with dangerous commands ---
        r"\$\(.*\b(rm|mkfs|dd|shutdown|reboot)\b",    # $() substitution
        r"`[^`]*\b(rm|mkfs|dd|shutdown|reboot)\b",    # backtick substitution
    ]

    def __init__(
        self,
        timeout: int = 60,
        working_dir: str | None = None,
        deny_patterns: list[str] | None = None,
        allow_patterns: list[str] | None = None,
        restrict_to_workspace: bool = False,
        audit_executions: bool = True,
    ):
        self.timeout = timeout
        self.working_dir = working_dir
        self.deny_patterns = deny_patterns if deny_patterns is not None else list(self.DEFAULT_DENY_PATTERNS)
        self.allow_patterns = allow_patterns or []
        self.restrict_to_workspace = restrict_to_workspace
        self.audit_executions = audit_executions
        self._formatter = ShellOutputFormatter()
        self._executor = ShellExecutor()
    
    @property
    def name(self) -> str:
        return "exec"
    
    @property
    def description(self) -> str:
        return "Execute a shell command and return its output. Use with caution."
    
    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute"
                },
                "working_dir": {
                    "type": "string",
                    "description": "Optional working directory for the command"
                }
            },
            "required": ["command"]
        }
    
    async def execute(self, command: str, working_dir: str | None = None, **kwargs: Any) -> str:
        cwd = working_dir or self.working_dir or os.getcwd()
        guard_error = self._guard_command(command, cwd)
        if guard_error:
            return guard_error

        if self.audit_executions:
            audit_log.info("shell_command_executed", command=command, working_dir=cwd)

        try:
            return await self._executor.run(command, cwd=cwd, timeout=self.timeout, formatter=self._formatter)
        except Exception as e:
            return f"Error executing command: {str(e)}"

    def _audit_blocked(self, command: str, reason: str) -> None:
        """Log a blocked command to the audit log."""
        audit_log.warning("shell_command_blocked", command=command, reason=reason)

    @staticmethod
    def _normalize_whitespace(cmd: str) -> str:
        """Replace tabs, newlines, and other exotic whitespace with plain spaces."""
        return ShellGuard.normalize_whitespace(cmd)

    def _structural_check(self, command: str) -> str | None:
        """Shlex-based structural analysis of the command tokens."""
        return self._build_guard().structural_check(command)

    def _guard_command(self, command: str, cwd: str) -> str | None:
        """Best-effort safety guard for potentially destructive commands."""
        return self._build_guard().guard(command, cwd)

    def _build_guard(self) -> ShellGuard:
        return ShellGuard(
            deny_patterns=self.deny_patterns,
            allow_patterns=self.allow_patterns,
            restrict_to_workspace=self.restrict_to_workspace,
            audit_blocked=self._audit_blocked,
        )
