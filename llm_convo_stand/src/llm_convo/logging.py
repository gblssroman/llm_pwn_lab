import json
import logging
from datetime import UTC, datetime
from typing import Any


def configure_logging(level: str) -> None:
    logging.basicConfig(level=level.upper(), format="%(message)s", force=True)


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    **fields: Any,
) -> None:
    payload = {
        "timestamp": datetime.now(UTC).isoformat(),
        "level": logging.getLevelName(level).lower(),
        "event": event,
        **fields,
    }
    logger.log(level, json.dumps(payload, ensure_ascii=False, default=str))
