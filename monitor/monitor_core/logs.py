"""结构化日志：每行一个 JSON，按大小滚动，不会无限增长。"""
import datetime
import json
import logging
import logging.handlers
from pathlib import Path


class _JsonFormatter(logging.Formatter):
    def format(self, record):
        entry = {
            "ts": datetime.datetime.fromtimestamp(record.created).astimezone().isoformat(timespec="seconds"),
            "level": record.levelname.lower(),
            "mod": getattr(record, "mod", "core"),
            "msg": record.getMessage(),
        }
        entry.update(getattr(record, "fields", None) or {})
        if record.exc_info:
            entry["error"] = self.formatException(record.exc_info).splitlines()[-1]
        return json.dumps(entry, ensure_ascii=False, default=str)


def setup(directory, max_bytes=1048576, backups=3, name="xiaoba.monitor", filename="monitor.log"):
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    if directory is None:
        logger.addHandler(logging.NullHandler())
        return logger
    Path(directory).mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        str(Path(directory) / filename), maxBytes=max_bytes, backupCount=backups, encoding="utf-8")
    handler.setFormatter(_JsonFormatter())
    logger.addHandler(handler)
    return logger


def event(logger, mod, msg, level="info", **fields):
    """写一条带模块名与附加字段的日志。"""
    logger.log(getattr(logging, level.upper(), logging.INFO), msg, extra={"mod": mod, "fields": fields})
