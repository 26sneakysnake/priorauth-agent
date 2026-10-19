"""Structured JSON logs that cannot carry record content.

Only the message and allowlisted `extra` fields are written; every other attribute of a LogRecord (including
arbitrary `extra` keys and exception text) is dropped. Messages in this code base are fixed strings; identifiers go in
`extra`. Third-party loggers that could echo payloads are raised to WARNING (SQLAlchemy would log SQL parameters at
INFO; the engine also hides them, see priorauth.jobs.db.make_engine).
"""

import json
import logging
from typing import Any

ALLOWED_EXTRA = frozenset(
    {"job_id", "case_id", "attempt", "outcome", "stored", "replayed", "error_kind", "cost_usd", "status_code"}
)
QUIET = ("sqlalchemy", "httpx", "httpcore", "asyncio", "aiosqlite", "asyncpg")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out: dict[str, Any] = {
            "ts": round(record.created, 3),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.msg if isinstance(record.msg, str) else type(record.msg).__name__,
        }
        out |= {k: v for k, v in record.__dict__.items() if k in ALLOWED_EXTRA}
        if record.exc_info and record.exc_info[0] is not None:
            out["exc_type"] = record.exc_info[0].__name__  # the type only: messages can quote input data
        return json.dumps(out, default=str)


def configure(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for name in QUIET:
        logging.getLogger(name).setLevel(logging.WARNING)
