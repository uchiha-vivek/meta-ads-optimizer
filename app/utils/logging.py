"""Logging configuration for meta-optimizer.

Two rendering modes, selected by ``LOG_FORMAT``: ``console`` routes records
through Rich for interactive use, ``json`` emits one object per line for a log
aggregator.

Both modes write to **stderr**, never stdout. The CLI prints Rich tables to
stdout, so keeping the two streams separate is what makes
``meta campaigns > campaigns.txt`` produce a file of campaigns rather than a
file of campaigns interleaved with log lines.

:func:`configure_logging` is called once, from the CLI composition root. Modules
elsewhere obtain a logger with ``logging.getLogger(__name__)`` and never
configure handlers themselves.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any, Final

from rich.console import Console
from rich.logging import RichHandler

from app.config.settings import LogFormat, LoggingSettings

# Third-party loggers that emit a record per HTTP connection at DEBUG. At
# LOG_LEVEL=DEBUG they bury application records, so they are pinned higher.
_NOISY_LIBRARY_LOGGERS: Final[tuple[str, ...]] = ("urllib3", "requests", "asyncio")

_LIBRARY_LOG_LEVEL: Final[int] = logging.WARNING

# Attributes present on every LogRecord. Anything outside this set was attached
# by application code via `extra=` and is promoted to a top-level JSON field.
_STANDARD_RECORD_ATTRIBUTES: Final[frozenset[str]] = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)


class JsonLogFormatter(logging.Formatter):
    """Render log records as single-line JSON objects.

    Keys are stable so that a log aggregator can index them: ``timestamp``,
    ``level``, ``logger``, ``message``, ``module``, ``function``, ``line``, and
    optionally ``exception``. Any keyword passed through ``extra=`` at the call
    site becomes a top-level field, which is what makes the structured context
    carried by :class:`~app.utils.exceptions.MetaOptimizerError` queryable.
    """

    def format(self, record: logging.LogRecord) -> str:
        """Serialize a single record to a JSON string."""
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }

        for key, value in record.__dict__.items():
            if key not in _STANDARD_RECORD_ATTRIBUTES and not key.startswith("_"):
                payload[key] = _to_json_safe(value)

        if record.exc_info is not None:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info is not None:
            payload["stack"] = self.formatStack(record.stack_info)

        return json.dumps(payload, default=str, separators=(",", ":"))


def _to_json_safe(value: object) -> object:
    """Return ``value`` unchanged when JSON-encodable, otherwise its ``repr``.

    A formatter that raises destroys the very record meant to explain a failure,
    so unencodable values degrade to text rather than propagating.
    """
    if isinstance(value, str | int | float | bool | type(None)):
        return value
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return repr(value)
    return value


def configure_logging(
    settings: LoggingSettings,
    *,
    console: Console | None = None,
) -> None:
    """Install the root log handler described by ``settings``.

    Idempotent: existing root handlers are removed first, so calling this twice
    does not duplicate every record.

    Args:
        settings: Level and rendering style, read from the environment.
        console: Rich console used in ``console`` mode. Injected so tests can
            capture output; defaults to a stderr console.
    """
    handler = _build_handler(settings, console)

    root_logger = logging.getLogger()
    for existing in list(root_logger.handlers):
        root_logger.removeHandler(existing)
    root_logger.addHandler(handler)
    root_logger.setLevel(settings.level.value)

    for library_logger_name in _NOISY_LIBRARY_LOGGERS:
        logging.getLogger(library_logger_name).setLevel(_LIBRARY_LOG_LEVEL)


def _build_handler(settings: LoggingSettings, console: Console | None) -> logging.Handler:
    """Construct the handler matching the configured output format."""
    if settings.output_format is LogFormat.JSON:
        json_handler = logging.StreamHandler(stream=sys.stderr)
        json_handler.setFormatter(JsonLogFormatter())
        return json_handler

    rich_handler = RichHandler(
        console=console if console is not None else Console(stderr=True),
        rich_tracebacks=True,
        show_path=False,
        markup=False,
        log_time_format="[%Y-%m-%d %H:%M:%S]",
    )
    rich_handler.setFormatter(logging.Formatter(fmt="%(message)s", datefmt="[%X]"))
    return rich_handler
