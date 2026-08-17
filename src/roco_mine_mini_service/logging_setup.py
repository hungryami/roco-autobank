"""Application logging with Shanghai timestamps and rotating local files."""

from __future__ import annotations

import logging
import os
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")
LOGGER_NAME = "roco_mine_mini_service"
DEFAULT_LOG_FILE = "logs/roco-mini-service.log"
_HANDLER_MARKER = "_roco_application_handler"


class ShanghaiFormatter(logging.Formatter):
    """Render every record in Asia/Shanghai regardless of host timezone."""

    def formatTime(  # noqa: N802 - logging.Formatter API
        self,
        record: logging.LogRecord,
        datefmt: str | None = None,
    ) -> str:
        current = datetime.fromtimestamp(record.created, SHANGHAI)
        return current.strftime(datefmt or "%Y-%m-%d %H:%M:%S%z")


def configure_application_logging() -> Path | None:
    """Configure console and optional rotating-file business logs once."""

    logger = logging.getLogger(LOGGER_NAME)
    if any(getattr(handler, _HANDLER_MARKER, False) for handler in logger.handlers):
        return _configured_log_path(logger)

    logger.setLevel(logging.INFO)
    logger.propagate = False
    formatter = ShanghaiFormatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S%z",
    )

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    setattr(console, _HANDLER_MARKER, True)
    logger.addHandler(console)

    raw_path = os.environ.get("ROCO_LOG_FILE", DEFAULT_LOG_FILE).strip()
    if not raw_path:
        logger.info("business_logging_ready file=disabled")
        return None

    log_path = Path(raw_path).expanduser()
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_path,
            maxBytes=10 * 1024 * 1024,
            backupCount=7,
            encoding="utf-8",
        )
    except OSError as exc:
        logger.warning(
            "business_log_file_unavailable error_type=%s",
            type(exc).__name__,
        )
        return None

    file_handler.setFormatter(formatter)
    setattr(file_handler, _HANDLER_MARKER, True)
    setattr(file_handler, "_roco_log_path", log_path)
    logger.addHandler(file_handler)
    logger.info("business_logging_ready file=%s", log_path)
    return log_path


def masked_uin(uin: str) -> str:
    """Return a stable, human-readable UIN suffix without logging the full UIN."""

    suffix = uin[-4:]
    return f"***{suffix}"


def _configured_log_path(logger: logging.Logger) -> Path | None:
    for handler in logger.handlers:
        path = getattr(handler, "_roco_log_path", None)
        if isinstance(path, Path):
            return path
    return None
