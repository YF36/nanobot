"""Centralized structured logging configuration using structlog."""

import logging
import sys

import structlog


def setup_logging(json_output: bool = True, level: str = "INFO") -> None:
    """Configure structlog with stdlib logging backend.

    Args:
        json_output: If True, output JSON lines; otherwise human-readable console output.
        level: Root log level for the ``nanobot`` logger hierarchy.
    """
    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if json_output:
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer()

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(formatter)

    root = logging.getLogger("nanobot")
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper()))


def get_logger(name: str = "nanobot") -> structlog.stdlib.BoundLogger:
    """Return a structlog bound logger for the given name."""
    return structlog.get_logger(name)
