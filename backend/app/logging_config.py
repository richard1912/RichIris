"""Structured logging setup using structlog + stdlib logging."""

import logging
import sys
from pathlib import Path

import structlog


def setup_logging(level: str = "DEBUG", json_output: bool = False) -> None:
    """Configure structured logging for the application."""
    log_level = getattr(logging, level.upper(), logging.DEBUG)

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
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
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    _configure_root_logger(formatter, log_level)
    _silence_noisy_loggers()


def _get_renderer(json_output: bool) -> structlog.types.Processor:
    """Return JSON or console renderer based on config."""
    if json_output:
        return structlog.processors.JSONRenderer()
    return structlog.dev.ConsoleRenderer()


def _configure_root_logger(formatter: logging.Formatter, level: int) -> None:
    """Set up the root logger with stream and file handlers."""
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)

    # Console output (for dev mode) - force UTF-8 to avoid cp1252 errors on Windows
    stdout_stream = open(sys.stdout.fileno(), mode="w", encoding="utf-8", closefd=False)
    stdout_handler = logging.StreamHandler(stdout_stream)
    stdout_handler.setFormatter(formatter)
    root.addHandler(stdout_handler)

    # File output (for service running in background)
    log_dir = Path(__file__).parent.parent.parent / "data" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "richiris.log"

    try:
        from logging.handlers import RotatingFileHandler
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=10 * 1024 * 1024,  # 10 MB
            backupCount=5,  # Keep 5 old files
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except Exception as e:
        root.warning(f"Failed to set up file logging: {e}")


def _silence_noisy_loggers() -> None:
    """Reduce log verbosity for chatty third-party libraries."""
    for name in ("uvicorn.access", "aiosqlite", "sqlalchemy.engine"):
        logging.getLogger(name).setLevel(logging.WARNING)
