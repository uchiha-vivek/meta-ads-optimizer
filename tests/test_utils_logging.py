"""Tests for logging configuration and the JSON formatter."""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterator

import pytest
from rich.console import Console

from app.config.settings import LogFormat, LoggingSettings, LogLevel
from app.utils.logging import JsonLogFormatter, configure_logging


@pytest.fixture(autouse=True)
def restore_root_logger() -> Iterator[None]:
    """Put the root logger back as it was, since configuration is global."""
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    yield
    root.handlers = original_handlers
    root.setLevel(original_level)


def make_record(**overrides: object) -> logging.LogRecord:
    """Build a log record with optional extra attributes attached."""
    record = logging.LogRecord(
        name="app.test",
        level=logging.INFO,
        pathname="/workspace/app/test.py",
        lineno=42,
        msg="Something happened: %s",
        args=("detail",),
        exc_info=None,
        func="do_work",
    )
    for key, value in overrides.items():
        setattr(record, key, value)
    return record


def test_json_formatter_emits_a_single_line_object() -> None:
    rendered = JsonLogFormatter().format(make_record())

    assert "\n" not in rendered
    payload = json.loads(rendered)
    assert payload["level"] == "INFO"
    assert payload["logger"] == "app.test"
    assert payload["message"] == "Something happened: detail"
    assert payload["line"] == 42
    assert payload["function"] == "do_work"


def test_timestamps_are_timezone_aware_utc() -> None:
    payload = json.loads(JsonLogFormatter().format(make_record()))

    # A naive timestamp is ambiguous once logs from two hosts are merged.
    assert payload["timestamp"].endswith("+00:00")


def test_extra_fields_become_top_level_keys() -> None:
    """This is what makes an exception's structured context queryable."""
    record = make_record(account_remote_id="act_123", rows=17)

    payload = json.loads(JsonLogFormatter().format(record))

    assert payload["account_remote_id"] == "act_123"
    assert payload["rows"] == 17


def test_unencodable_extras_degrade_to_text_instead_of_raising() -> None:
    # A formatter that raises destroys the very record meant to explain a failure.
    record = make_record(payload=object())

    payload = json.loads(JsonLogFormatter().format(record))

    assert isinstance(payload["payload"], str)


def _raise_value_error() -> None:
    """Raise a known error so a test can capture real exception info."""
    message = "boom"
    raise ValueError(message)


def test_exceptions_are_included() -> None:
    try:
        _raise_value_error()
    except ValueError:
        record = make_record()
        record.exc_info = sys.exc_info()

    payload = json.loads(JsonLogFormatter().format(record))

    assert "ValueError" in payload["exception"]


def test_console_mode_installs_a_rich_handler() -> None:
    console = Console(record=True, width=120)

    configure_logging(
        LoggingSettings(level=LogLevel.DEBUG, output_format=LogFormat.CONSOLE),
        console=console,
    )

    root = logging.getLogger()
    assert len(root.handlers) == 1
    assert root.level == logging.DEBUG


def test_json_mode_installs_the_json_formatter() -> None:
    configure_logging(LoggingSettings(level=LogLevel.WARNING, output_format=LogFormat.JSON))

    root = logging.getLogger()
    assert isinstance(root.handlers[0].formatter, JsonLogFormatter)
    assert root.level == logging.WARNING


def test_logs_are_written_to_stderr_so_stdout_stays_pipeable() -> None:
    configure_logging(LoggingSettings(level=LogLevel.INFO, output_format=LogFormat.JSON))

    handler = logging.getLogger().handlers[0]
    assert isinstance(handler, logging.StreamHandler)
    # `meta campaigns > file` must produce campaigns, not campaigns plus logs.
    assert handler.stream is sys.stderr


def test_configuring_twice_does_not_duplicate_records() -> None:
    settings = LoggingSettings(level=LogLevel.INFO, output_format=LogFormat.JSON)

    configure_logging(settings)
    configure_logging(settings)

    assert len(logging.getLogger().handlers) == 1


def test_noisy_library_loggers_are_pinned_above_debug() -> None:
    configure_logging(LoggingSettings(level=LogLevel.DEBUG, output_format=LogFormat.JSON))

    # At DEBUG these emit a record per connection and bury application logs.
    assert logging.getLogger("urllib3").level == logging.WARNING
    assert logging.getLogger("requests").level == logging.WARNING
