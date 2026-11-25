import json
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

FAILED_FILES_LOGGER = "app.failed_files"

_STANDARD_ATTRS = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        payload.update({k: v for k, v in record.__dict__.items() if k not in _STANDARD_ATTRS})
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)


FAILED_FILES_LOG_BYTES = 5 * 1024 * 1024
FAILED_FILES_LOG_BACKUPS = 3


def setup_failed_files_log(path: Path) -> None:
    """A hibás fájlok külön, JSON soros naplófájlja (forgatott, így nem nő korlátlanul)."""
    logger = logging.getLogger(FAILED_FILES_LOGGER)
    for existing in list(logger.handlers):
        logger.removeHandler(existing)
        existing.close()
    logger.propagate = False
    logger.setLevel(logging.INFO)
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        path, maxBytes=FAILED_FILES_LOG_BYTES, backupCount=FAILED_FILES_LOG_BACKUPS, encoding="utf-8"
    )
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
