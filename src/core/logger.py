"""
logger.py — Centralised structured logging.

Design decisions:
  - Uses structlog for structured (JSON-compatible) log records.
  - In development: pretty console output with colours.
  - In production: JSON lines to stdout (compatible with log aggregators
    like Datadog, Grafana Loki, AWS CloudWatch).
  - Every log record carries: session_id, engine, timestamp automatically.
  - A rotating file handler writes to outputs/logs/ so nothing is lost.
"""

import logging
import logging.handlers
import os
import sys
from pathlib import Path
from typing import Any

import structlog
from structlog.types import EventDict, WrappedLogger


# ─────────────────────────────────────────────────────────────────────────────
# Log level from environment (default INFO)
# ─────────────────────────────────────────────────────────────────────────────

_LOG_LEVEL_NAME = os.getenv("QA_LOG_LEVEL", "INFO").upper()
_LOG_LEVEL = getattr(logging, _LOG_LEVEL_NAME, logging.INFO)
_ENV = os.getenv("QA_ENV", "development").lower()


# ─────────────────────────────────────────────────────────────────────────────
# Custom processors
# ─────────────────────────────────────────────────────────────────────────────

def _add_log_level(
    logger: WrappedLogger, method_name: str, event_dict: EventDict
) -> EventDict:
    """Add the log level string to every record."""
    event_dict["level"] = method_name.upper()
    return event_dict


def _drop_color_message_key(
    logger: WrappedLogger, method_name: str, event_dict: EventDict
) -> EventDict:
    """
    Uvicorn injects a 'color_message' key that pollutes JSON logs.
    Drop it silently.
    """
    event_dict.pop("color_message", None)
    return event_dict


# ─────────────────────────────────────────────────────────────────────────────
# Configure structlog + stdlib logging
# ─────────────────────────────────────────────────────────────────────────────

def setup_logging(output_dir: str = "outputs") -> None:
    """
    Call once at application startup (in cli.py or main.py).
    Idempotent — safe to call multiple times.
    """
    log_dir = Path(output_dir) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    # Shared processors run on every log record regardless of renderer
    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,   # Inject session_id etc.
        _add_log_level,
        _drop_color_message_key,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.stdlib.add_logger_name,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if _ENV == "production":
        # JSON lines — one record per line, machine-readable
        renderer = structlog.processors.JSONRenderer()
    else:
        # Human-friendly coloured console output for development
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    structlog.configure(
        processors=shared_processors + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(_LOG_LEVEL),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processor=renderer,
        foreign_pre_chain=shared_processors,
    )

    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    console_handler.setLevel(_LOG_LEVEL)

    # Rotating file handler — keeps last 7 days / 10 MB per file
    file_handler = logging.handlers.RotatingFileHandler(
        filename=log_dir / "qa-bot.log",
        maxBytes=10 * 1024 * 1024,   # 10 MB
        backupCount=7,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.DEBUG)   # Always verbose in file

    # Root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)    # Let handlers filter independently

    # Avoid duplicate handlers if setup_logging() called more than once
    if not root_logger.handlers:
        root_logger.addHandler(console_handler)
        root_logger.addHandler(file_handler)

    # Quieten noisy third-party libraries
    logging.getLogger("playwright").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)


# ─────────────────────────────────────────────────────────────────────────────
# Public factory — use this everywhere instead of logging.getLogger()
# ─────────────────────────────────────────────────────────────────────────────

def get_logger(name: str) -> Any:
    """
    Return a structlog logger bound to the given name.

    Usage:
        from src.core.logger import get_logger
        log = get_logger(__name__)
        log.info("page_loaded", url=url, duration_ms=elapsed)
    """
    return structlog.get_logger(name)


# ─────────────────────────────────────────────────────────────────────────────
# Context binding helpers
# ─────────────────────────────────────────────────────────────────────────────

def bind_session(session_id: str) -> None:
    """
    Bind session_id to the async context so every subsequent log record
    in this coroutine (and its children) carries it automatically.
    """
    structlog.contextvars.bind_contextvars(session_id=session_id)


def bind_engine(engine: str) -> None:
    """Bind the current engine name to the async context."""
    structlog.contextvars.bind_contextvars(engine=engine)


def clear_context() -> None:
    """Clear all context vars — call at the start of each test session."""
    structlog.contextvars.clear_contextvars()
