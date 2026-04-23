"""
Structured logging with loguru.
"""

from __future__ import annotations

import sys

from loguru import logger as _logger


def configure_logging(level: str = "INFO", log_file: str | None = "data/hub.log") -> None:
    _logger.remove()
    fmt = (
        "<green>{time:HH:mm:ss}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
        "<level>{message}</level>"
    )
    _logger.add(sys.stdout, level=level, format=fmt, colorize=True)
    if log_file:
        _logger.add(
            log_file,
            rotation="10 MB",
            retention="7 days",
            level=level,
            enqueue=True,
        )


def get_logger(name: str | None = None):
    if name:
        return _logger.bind(context=name)
    return _logger


info = _logger.info
warning = _logger.warning
debug = _logger.debug
error = _logger.error
