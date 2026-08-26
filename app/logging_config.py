from __future__ import annotations

import json
import logging

from app.time import utc_now


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": utc_now().isoformat().replace("+00:00", "Z"),
            "severity": record.levelname,
            "event": record.getMessage(),
        }
        for field in ("request_id", "error_class"):
            value = getattr(record, field, None)
            if value:
                payload[field] = value
        return json.dumps(payload)


def configure_logging(log_level: str) -> logging.Logger:
    logger = logging.getLogger("silent_relay")
    if not any(getattr(handler, "silent_relay_handler", False) for handler in logger.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        handler.silent_relay_handler = True  # type: ignore[attr-defined]
        logger.addHandler(handler)
    logger.setLevel(log_level)
    return logger
