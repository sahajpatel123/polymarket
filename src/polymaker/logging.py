"""structlog configuration: human console in dev, rotated JSON files in prod.

T1-04: JSON lines with consistent fields (timestamp, level, logger, event),
daily UTC rotation, and market-scoped fields greppable via scripts/grep_logs.py.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any

import structlog

_REQUIRED_JSON_FIELDS = ("timestamp", "level", "event", "logger")


def _add_logger_name(
    logger: logging.Logger, method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """Ensure every JSON line carries a stable `logger` field."""
    event_dict.setdefault("logger", getattr(logger, "name", "polymaker"))
    return event_dict


def configure(
    *,
    level: str = "INFO",
    json_file: Path | None = None,
    console: bool = True,
    rotate_when: str = "midnight",
    backup_count: int = 14,
) -> None:
    """Set up structlog + stdlib logging once at process start.

    When `json_file` is set, writes newline-delimited JSON with daily UTC
    rotation (`backup_count` retained files).
    """
    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        _add_logger_name,
    ]

    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)

    if console:
        ch = logging.StreamHandler(sys.stderr)
        ch.setFormatter(
            structlog.stdlib.ProcessorFormatter(
                processors=[
                    structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                    structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty()),
                ]
            )
        )
        root.addHandler(ch)

    if json_file is not None:
        json_file.parent.mkdir(parents=True, exist_ok=True)
        fh: logging.Handler = TimedRotatingFileHandler(
            filename=str(json_file),
            when=rotate_when,
            interval=1,
            backupCount=backup_count,
            utc=True,
            encoding="utf-8",
        )
        fh.setFormatter(
            structlog.stdlib.ProcessorFormatter(
                processors=[
                    structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                    structlog.processors.JSONRenderer(),
                ]
            )
        )
        root.addHandler(fh)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]


def required_json_fields() -> tuple[str, ...]:
    return _REQUIRED_JSON_FIELDS
