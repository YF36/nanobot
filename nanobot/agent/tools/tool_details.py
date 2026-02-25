"""Shared constants/helpers for structured tool result details."""

from __future__ import annotations

from pathlib import Path
from typing import Any

OP_READ_FILE = "read_file"
OP_WRITE_FILE = "write_file"
OP_EDIT_FILE = "edit_file"
OP_LIST_DIR = "list_dir"
OP_EXEC = "exec"
OP_MESSAGE = "message"
OP_SPAWN = "spawn"


def details_with_op(op: str, **fields: Any) -> dict[str, Any]:
    """Build a structured details payload with a normalized ``op`` field."""
    return {"op": op, **fields}


def file_details_base(op: str, file_path: Path, requested_path: str) -> dict[str, Any]:
    """Common structured metadata fields for filesystem tools."""
    return details_with_op(
        op,
        path=str(file_path),
        requested_path=requested_path,
    )

