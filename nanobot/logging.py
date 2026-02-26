"""Centralized structured logging configuration using structlog."""

import json
import logging
import re
import sys

import structlog


# Patterns that look like secrets in log output
_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{10,}"),           # OpenAI / Anthropic style
    re.compile(r"Bearer\s+[A-Za-z0-9_\-.]{10,}"),    # Authorization headers
    re.compile(r"xoxb-[A-Za-z0-9-]{10,}"),           # Slack bot tokens
    re.compile(r"xapp-[A-Za-z0-9-]{10,}"),           # Slack app tokens
    re.compile(r"pk-[A-Za-z0-9_-]{10,}"),            # Langfuse public key
    re.compile(r"ghp_[A-Za-z0-9]{10,}"),             # GitHub PAT
    re.compile(r"gho_[A-Za-z0-9]{10,}"),             # GitHub OAuth
    re.compile(r"glpat-[A-Za-z0-9_-]{10,}"),         # GitLab PAT
]


def mask_secret(value: str) -> str:
    """Mask a secret value, keeping first 4 and last 4 chars visible.

    >>> mask_secret("sk-abc123456789xyz")
    'sk-a****9xyz'
    """
    if len(value) <= 8:
        return "****"
    return value[:4] + "****" + value[-4:]


def _redact_value(value: str) -> str:
    """Replace any secret-looking substrings in *value*."""
    for pattern in _SECRET_PATTERNS:
        value = pattern.sub(lambda m: mask_secret(m.group(0)), value)
    return value


def _redact_event(
    logger: logging.Logger,
    method_name: str,
    event_dict: dict,
) -> dict:
    """Structlog processor that redacts secrets from all string values."""
    for key, val in event_dict.items():
        if isinstance(val, str):
            event_dict[key] = _redact_value(val)
    return event_dict


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
        _redact_event,
    ]

    if json_output:
        renderer = structlog.processors.JSONRenderer(serializer=lambda obj, **kw: json.dumps(obj, ensure_ascii=False, **kw))
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
