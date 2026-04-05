"""Structured logging setup using structlog + stdlib logging."""

import logging
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import structlog


def _make_timestamper(tz: ZoneInfo):
    """Return a structlog processor that timestamps events in the given timezone."""
    def timestamper(logger, method, event_dict):
        event_dict["timestamp"] = datetime.now(tz).isoformat()
        return event_dict
    return timestamper


def setup_logging(level: str = "DEBUG", json_output: bool = False, timezone: str = "UTC") -> None:
    """Configure structured logging for the application."""
    log_level = getattr(logging, level.upper(), logging.DEBUG)
    tz = ZoneInfo(timezone)

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.ExtraAdder(),
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        _make_timestamper(tz),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    renderer = _get_renderer(json_output)

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
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    _configure_root_logger(formatter, log_level)
    _silence_noisy_loggers()

    # App loggers get the configured level (e.g. DEBUG); root stays at INFO
    # so third-party libraries don't flood with debug output
    logging.getLogger("app").setLevel(log_level)


def _get_renderer(json_output: bool) -> structlog.types.Processor:
    """Return JSON or console renderer based on config."""
    if json_output:
        return structlog.processors.JSONRenderer()
    return structlog.dev.ConsoleRenderer()


def _configure_root_logger(formatter: logging.Formatter, level: int) -> None:
    """Set up the root logger with stream and file handlers."""
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.INFO)

    # Console output (for dev mode) - force UTF-8 to avoid cp1252 errors on Windows
    stdout_stream = open(sys.stdout.fileno(), mode="w", encoding="utf-8", closefd=False)
    stdout_handler = logging.StreamHandler(stdout_stream)
    stdout_handler.setFormatter(formatter)
    root.addHandler(stdout_handler)

    # File output (for service running in background)
    from app.config import get_bootstrap
    bootstrap = get_bootstrap()
    log_dir = Path(bootstrap.data_dir) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "richiris.log"

    try:
        from logging.handlers import RotatingFileHandler
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=10 * 1024 * 1024,  # 10 MB
            backupCount=5,  # Keep 5 old files
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except Exception as e:
        root.warning(f"Failed to set up file logging: {e}")


def _silence_noisy_loggers() -> None:
    """Reduce log verbosity for chatty third-party libraries."""
    for name in ("uvicorn.access", "aiosqlite", "sqlalchemy.engine", "httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
