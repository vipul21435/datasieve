"""Structured logging for curator.

Library code only ever does ``logger = logging.getLogger(__name__)`` and logs an
*event name* as the message, with the details passed as ``extra`` fields::

    logger.info("validate.finished", extra={"valid": 98, "invalid": 2})

Applications (the CLI, the API server) call :func:`configure_logging` once. In
``json`` format every record becomes one JSON object per line on stderr::

    {"ts":"2026-01-01T12:00:00.000Z","level":"info","logger":"curator.stages.validate",
     "event":"validate.finished","stage":"validate","valid":98,"invalid":2}

:func:`log_context` binds fields (a run id, the current stage, ...) to every
record logged inside a ``with`` block, including from nested calls. It is
backed by a :class:`contextvars.ContextVar`, so concurrent threads and asyncio
tasks each see their own context.

Only the ``curator`` logger hierarchy is configured; the root logger and other
libraries' loggers are left alone.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterator
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC
from datetime import date
from datetime import datetime
from enum import Enum
from pathlib import PurePath
from types import MappingProxyType
from typing import Literal
from typing import TextIO
from typing import assert_never

LOGGER_NAME = "curator"

LogFormat = Literal["json", "text"]
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

# Keys every JSON line starts with. Context and ``extra`` fields cannot override them.
BASE_KEYS = ("ts", "level", "logger", "event")

# Attributes every LogRecord has; anything else on a record came from ``extra``.
_RECORD_ATTRS = frozenset(vars(logging.LogRecord("", logging.INFO, "", 0, "", None, None))) | {
    "message",
    "asctime",
}

_context: ContextVar[Mapping[str, object]] = ContextVar("curator_log_context", default=MappingProxyType({}))

# Marks the handler configure_logging installed so a second call replaces it instead of adding another.
_HANDLER_FLAG = "_curator_handler"


@contextmanager
def log_context(**fields: object) -> Iterator[None]:
    """Attach ``fields`` to every record logged inside the ``with`` block.

    Nested blocks add to (and may override) the outer fields; leaving a block
    restores the previous context.
    """
    token = _context.set({**_context.get(), **fields})
    try:
        yield
    finally:
        _context.reset(token)


def current_context() -> dict[str, object]:
    """Return a copy of the fields bound by the enclosing :func:`log_context` blocks."""
    return dict(_context.get())


def _extra_fields(record: logging.LogRecord) -> dict[str, object]:
    return {key: value for key, value in vars(record).items() if key not in _RECORD_ATTRS and not key.startswith("_")}


def _json_default(value: object) -> object:
    """Serialise the non-JSON types that commonly show up in log fields."""
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, PurePath):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, set | frozenset):
        return sorted(value, key=repr)
    return repr(value)


class JsonFormatter(logging.Formatter):
    """Format each record as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {**current_context(), **_extra_fields(record)}
        payload.update(
            ts=datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            level=record.levelname.lower(),
            logger=record.name,
            event=record.getMessage(),
        )
        if record.exc_info and record.exc_info[1] is not None:
            exc = record.exc_info[1]
            payload["exc_type"] = type(exc).__qualname__
            payload["exc_message"] = str(exc)
            payload["traceback"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        ordered = {key: payload.pop(key) for key in BASE_KEYS} | payload
        return json.dumps(ordered, default=_json_default, separators=(",", ":"))


class TextFormatter(logging.Formatter):
    """Human-readable format: the event followed by ``key=value`` fields."""

    def __init__(self) -> None:
        super().__init__("%(asctime)s %(levelname)-8s %(name)s: %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        fields = {**current_context(), **_extra_fields(record)}
        if fields:
            rendered = " ".join(f"{key}={json.dumps(value, default=_json_default)}" for key, value in fields.items())
            first, newline, rest = line.partition("\n")
            line = f"{first} {rendered}{newline}{rest}"
        return line


def configure_logging(
    level: LogLevel | int = "INFO",
    fmt: LogFormat = "json",
    *,
    stream: TextIO | None = None,
) -> logging.Logger:
    """Send ``curator.*`` logs to ``stream`` (stderr by default) in the given format.

    Safe to call more than once: the handler installed by a previous call is
    replaced, never duplicated. Records do not propagate to the root logger, so
    an application's own logging setup does not print them a second time.
    """
    formatter: logging.Formatter
    if fmt == "json":
        formatter = JsonFormatter()
    elif fmt == "text":
        formatter = TextFormatter()
    else:  # pragma: no cover - guarded by the LogFormat type and by settings validation
        assert_never(fmt)

    logger = logging.getLogger(LOGGER_NAME)
    for old in [h for h in logger.handlers if getattr(h, _HANDLER_FLAG, False)]:
        logger.removeHandler(old)
        old.close()

    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.setFormatter(formatter)
    setattr(handler, _HANDLER_FLAG, True)
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    return logger


__all__ = [
    "BASE_KEYS",
    "LOGGER_NAME",
    "JsonFormatter",
    "LogFormat",
    "LogLevel",
    "TextFormatter",
    "configure_logging",
    "current_context",
    "log_context",
]
