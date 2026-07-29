"""Shared option parsing and defaulting for CLI commands.

Kept in one place so that ``--account-id`` means the same thing in every
command and the reporting window is computed identically wherever it appears.
Parsing user input into domain enums also happens here, so a command body never
holds a raw string it later compares against a literal.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Final

import typer

from app.cli.context import ApplicationContext
from app.models.enums import EntityStatus, InsightLevel

DEFAULT_LOOKBACK_DAYS: Final[int] = 7

# Meta restates the current day continuously as events arrive and attribution
# windows close, so today's figures are always incomplete. Reporting through
# yesterday means a number does not change after it has been read.
REPORTING_LAG_DAYS: Final[int] = 1

_MINIMUM_LOOKBACK_DAYS: Final[int] = 1


def resolve_account_id(context: ApplicationContext, account_id: str | None) -> str:
    """Return the account to operate on, falling back to configuration.

    Args:
        context: The wired application context.
        account_id: Value supplied on the command line, if any.

    Returns:
        The explicit account ID when given, otherwise ``META_AD_ACCOUNT_ID``.
    """
    if account_id:
        return account_id
    return context.settings.meta.ad_account_id


def resolve_window(days: int, *, today: date | None = None) -> tuple[date, date]:
    """Compute the reporting window ``days`` long, ending yesterday.

    Args:
        days: Length of the window in days, inclusive of both ends.
        today: Current date. Injected so tests are not time-dependent; defaults
            to the current UTC date.

    Returns:
        The ``(since, until)`` bounds, both inclusive.

    Raises:
        typer.BadParameter: If ``days`` is less than one, which would produce a
            window ending before it starts.
    """
    if days < _MINIMUM_LOOKBACK_DAYS:
        message = f"--days must be at least {_MINIMUM_LOOKBACK_DAYS}, got {days}"
        raise typer.BadParameter(message)

    reference_date = today if today is not None else datetime.now(UTC).date()
    until = reference_date - timedelta(days=REPORTING_LAG_DAYS)
    since = until - timedelta(days=days - 1)
    return since, until


def parse_level(value: str) -> InsightLevel:
    """Convert a ``--level`` argument into an aggregation level.

    Args:
        value: Raw command-line value.

    Returns:
        The matching level.

    Raises:
        typer.BadParameter: If the value is not a level Meta reports at. The
            message lists the valid values, since guessing is not reasonable.
    """
    try:
        return InsightLevel(value.strip().lower())
    except ValueError as exc:
        valid = ", ".join(level.value for level in InsightLevel)
        message = f"Unknown level {value!r}. Choose one of: {valid}"
        raise typer.BadParameter(message) from exc


def parse_statuses(value: str | None) -> list[EntityStatus] | None:
    """Convert a comma-separated ``--status`` argument into domain statuses.

    Args:
        value: Raw command-line value, e.g. ``"active,paused"``.

    Returns:
        The parsed statuses, or ``None`` when no filter was given.

    Raises:
        typer.BadParameter: If any entry is not a known status.
    """
    if value is None or not value.strip():
        return None

    statuses: list[EntityStatus] = []
    valid = ", ".join(status.value for status in EntityStatus)
    for raw_entry in value.split(","):
        entry = raw_entry.strip().lower()
        if not entry:
            continue
        try:
            statuses.append(EntityStatus(entry))
        except ValueError as exc:
            message = f"Unknown status {raw_entry.strip()!r}. Choose from: {valid}"
            raise typer.BadParameter(message) from exc

    return statuses or None
