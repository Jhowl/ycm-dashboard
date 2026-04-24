"""Structured logging configuration.

Uses structlog when available, falls back to a tidy stdlib formatter otherwise so
the project remains testable without extra runtime dependencies during CI.
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Any


def _stdlib_formatter(use_json: bool) -> logging.Formatter:
    if use_json:
        import json

        class JsonFormatter(logging.Formatter):
            def format(self, record: logging.LogRecord) -> str:  # noqa: D401
                payload: dict[str, Any] = {
                    "level": record.levelname,
                    "logger": record.name,
                    "message": record.getMessage(),
                    "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
                }
                if record.exc_info:
                    payload["exc_info"] = self.formatException(record.exc_info)
                return json.dumps(payload, ensure_ascii=False)

        return JsonFormatter()

    return logging.Formatter(
        fmt="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def configure_logging(level: str | None = None, use_json: bool | None = None) -> None:
    """Configure process-wide logging. Safe to call multiple times."""
    resolved_level = (level or os.getenv("YCM_LOG_LEVEL") or "INFO").upper()
    resolved_json = (
        use_json
        if use_json is not None
        else str(os.getenv("YCM_LOG_JSON", "false")).lower() in {"1", "true", "yes", "on"}
    )

    root = logging.getLogger()
    root.setLevel(resolved_level)

    # Replace handlers so re-config in tests does not leak
    for handler in list(root.handlers):
        root.removeHandler(handler)

    stream_handler = logging.StreamHandler(stream=sys.stdout)
    stream_handler.setFormatter(_stdlib_formatter(resolved_json))
    root.addHandler(stream_handler)

    # Quiet down chatty third-party libraries
    for noisy in ("httpx", "httpcore", "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    try:
        import structlog

        structlog.configure(
            processors=[
                structlog.processors.add_log_level,
                structlog.processors.TimeStamper(fmt="iso"),
                structlog.processors.StackInfoRenderer(),
                structlog.processors.format_exc_info,
                (
                    structlog.processors.JSONRenderer()
                    if resolved_json
                    else structlog.dev.ConsoleRenderer(colors=False)
                ),
            ],
            wrapper_class=structlog.make_filtering_bound_logger(
                logging.getLevelName(resolved_level)
            ),
            cache_logger_on_first_use=True,
        )
    except ImportError:
        # structlog missing (e.g. minimal test env): stdlib logging is enough.
        pass


def get_logger(name: str) -> Any:
    """Return a structlog logger when available, else a stdlib Logger."""
    try:
        import structlog

        return structlog.get_logger(name)
    except ImportError:
        return logging.getLogger(name)
