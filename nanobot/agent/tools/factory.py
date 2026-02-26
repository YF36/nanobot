"""Helpers for creating the standard nanobot tool set."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from nanobot.agent.tools.cron import CronTool
from nanobot.agent.tools.filesystem import EditFileTool, ListDirTool, ReadFileTool, WriteFileTool
from nanobot.agent.tools.message import MessageTool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.agent.tools.shell import ExecTool
from nanobot.agent.tools.spawn import SpawnTool
from nanobot.agent.tools.web import WebFetchTool, WebSearchTool

if TYPE_CHECKING:
    from nanobot.config.schema import ExecToolConfig, FilesystemToolConfig
    from nanobot.cron.service import CronService
    from nanobot.agent.subagent import SubagentManager


def create_standard_tool_registry(
    *,
    workspace: Path,
    brave_api_key: str | None,
    exec_config: "ExecToolConfig",
    filesystem_config: "FilesystemToolConfig",
    restrict_to_workspace: bool,
    audit_tool_calls: bool = True,
    message_send_callback: Callable[[Any], Any] | None = None,
    spawn_manager: "SubagentManager | None" = None,
    cron_service: "CronService | None" = None,
) -> ToolRegistry:
    """Create the default tool registry used by the main agent and subagents."""
    registry = ToolRegistry(audit=audit_tool_calls)

    allowed_dir = workspace if restrict_to_workspace else None
    audit_fs = filesystem_config.audit_operations
    for cls in (ReadFileTool, WriteFileTool, EditFileTool, ListDirTool):
        registry.register(cls(workspace=workspace, allowed_dir=allowed_dir, audit_operations=audit_fs))

    registry.register(
        ExecTool(
            working_dir=str(workspace),
            timeout=exec_config.timeout,
            deny_patterns=exec_config.deny_patterns,
            allow_patterns=exec_config.allow_patterns,
            path_append=exec_config.path_append,
            restrict_to_workspace=restrict_to_workspace,
            audit_executions=exec_config.audit_executions,
        )
    )
    registry.register(WebSearchTool(api_key=brave_api_key))
    registry.register(WebFetchTool())

    if message_send_callback is not None:
        registry.register(MessageTool(send_callback=message_send_callback))
    if spawn_manager is not None:
        registry.register(SpawnTool(manager=spawn_manager))
    if cron_service is not None:
        registry.register(CronTool(cron_service))

    return registry
