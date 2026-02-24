"""Tool registry for dynamic tool management."""

import time
from typing import Any

from nanobot.agent.tools.base import Tool, ToolExecutionResult
from nanobot.logging import get_logger

audit_log = get_logger("nanobot.audit")


class ToolRegistry:
    """
    Registry for agent tools.

    Allows dynamic registration and execution of tools.
    """

    _TRUNCATE_KEYS = {"content", "task", "message", "command"}
    _REDACT_KEYS = {"new_content"}

    def __init__(self, audit: bool = True):
        self._tools: dict[str, Tool] = {}
        self._audit = audit

    def register(self, tool: Tool) -> None:
        """Register a tool."""
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        """Unregister a tool by name."""
        self._tools.pop(name, None)

    def get(self, name: str) -> Tool | None:
        """Get a tool by name."""
        return self._tools.get(name)

    def has(self, name: str) -> bool:
        """Check if a tool is registered."""
        return name in self._tools

    def get_definitions(self) -> list[dict[str, Any]]:
        """Get all tool definitions in OpenAI format."""
        return [tool.to_schema() for tool in self._tools.values()]

    def _sanitize_params(self, tool_name: str, params: dict) -> dict:
        """Sanitize parameters for audit logging (truncate/redact sensitive values)."""
        sanitized = {}
        for k, v in params.items():
            if k in self._REDACT_KEYS:
                sanitized[k] = f"<{len(str(v))} chars>"
            elif k in self._TRUNCATE_KEYS and isinstance(v, str) and len(v) > 200:
                sanitized[k] = v[:200] + "..."
            else:
                sanitized[k] = v
        return sanitized

    @staticmethod
    def _ensure_result(result: str | ToolExecutionResult) -> ToolExecutionResult:
        if isinstance(result, ToolExecutionResult):
            return result
        return ToolExecutionResult(text=str(result))

    async def execute_result(self, name: str, params: dict[str, Any]) -> ToolExecutionResult:
        """Execute a tool and return a structured result (compatibly wraps legacy string returns)."""
        _HINT = "\n\n[Analyze the error above and try a different approach.]"

        tool = self._tools.get(name)
        if not tool:
            return ToolExecutionResult(
                text=f"Error: Tool '{name}' not found. Available: {', '.join(self.tool_names)}",
                is_error=True,
            )

        if self._audit:
            audit_log.info(
                "tool_call_started",
                tool=name,
                params=self._sanitize_params(name, params),
            )

        t0 = time.monotonic()
        try:
            errors = tool.validate_params(params)
            if errors:
                msg = f"Error: Invalid parameters for tool '{name}': " + "; ".join(errors) + _HINT
                if self._audit:
                    audit_log.warning("tool_call_failed", tool=name, error="invalid_params")
                return ToolExecutionResult(text=msg, is_error=True)

            raw_result = await tool.execute(**params)
            result = self._ensure_result(raw_result)
            if result.text.startswith("Error") and not result.text.endswith(_HINT):
                result = ToolExecutionResult(
                    text=result.text + _HINT,
                    details=result.details,
                    is_error=True if not result.is_error else result.is_error,
                )
            if self._audit:
                elapsed = (time.monotonic() - t0) * 1000
                audit_log.info(
                    "tool_call_completed",
                    tool=name,
                    duration_ms=round(elapsed, 1),
                    result_length=len(result.text),
                    is_error=result.is_error,
                    has_details=bool(result.details),
                    detail_op=result.details.get("op") if result.details else None,
                )
            return result
        except Exception as e:
            if self._audit:
                elapsed = (time.monotonic() - t0) * 1000
                audit_log.warning(
                    "tool_call_failed",
                    tool=name,
                    error=str(e),
                    duration_ms=round(elapsed, 1),
                )
            return ToolExecutionResult(text=f"Error executing {name}: {str(e)}" + _HINT, is_error=True)

    async def execute(self, name: str, params: dict[str, Any]) -> str:
        """
        Execute a tool by name with given parameters.

        Args:
            name: Tool name.
            params: Tool parameters.

        Returns:
            Tool execution result as string.
        """
        return (await self.execute_result(name, params)).text

    @property
    def tool_names(self) -> list[str]:
        """Get list of registered tool names."""
        return list(self._tools.keys())

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools
