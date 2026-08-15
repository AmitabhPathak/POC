"""Logging configuration for the PnL engine."""

from __future__ import annotations

import logging
from pathlib import Path

LOGGER_NAME = "pnl_engine"
DEFAULT_LOG_FILE = Path("logs") / "pnl_engine.log"


def setup_logging(
    log_file: Path | None = DEFAULT_LOG_FILE,
    level: str = "INFO",
    console: bool = False,
) -> logging.Logger:
    """
    Configure application logging.

    Writes structured event logs to ``log_file`` by default.
    Pass ``log_file=None`` to disable file output.
    """
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.handlers.clear()
    logger.propagate = False

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if log_file is not None:
        log_file = Path(log_file)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    if console:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

    if not logger.handlers:
        logger.addHandler(logging.NullHandler())

    return logger


def get_logger() -> logging.Logger:
    """Return the shared PnL engine logger."""
    return logging.getLogger(LOGGER_NAME)
