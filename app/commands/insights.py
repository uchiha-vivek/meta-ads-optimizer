"""The ``meta insights`` command."""

from __future__ import annotations

import typer

from app.cli.context import application_context
from app.cli.error_handling import handle_domain_errors
from app.cli.options import (
    DEFAULT_LOOKBACK_DAYS,
    parse_level,
    resolve_account_id,
    resolve_window,
)
from app.cli.rendering import render_performance


def insights_command(
    ctx: typer.Context,
    account_id: str = typer.Option(
        "",
        "--account-id",
        "-a",
        help="Meta ad account ID (act_...). Defaults to META_AD_ACCOUNT_ID.",
    ),
    level: str = typer.Option(
        "campaign",
        "--level",
        "-l",
        help="Aggregation level: account, campaign, adset, or ad.",
    ),
    days: int = typer.Option(
        DEFAULT_LOOKBACK_DAYS,
        "--days",
        "-d",
        help="Length of the reporting window, ending yesterday.",
    ),
    sync: bool = typer.Option(
        False,
        "--sync",
        help="Fetch insights from Meta before reporting.",
    ),
) -> None:
    """Report performance for an ad account.

    Every row is shown against the equally long period immediately before it, so
    a figure can be read as better or worse rather than merely large or small.
    The window ends yesterday, because Meta continues to restate the current day
    as attribution windows close.
    """
    with handle_domain_errors():
        context = application_context(ctx)
        resolved_account_id = resolve_account_id(context, account_id or None)
        since, until = resolve_window(days)
        report = context.insights.performance_report(
            resolved_account_id,
            level=parse_level(level),
            since=since,
            until=until,
            refresh=sync,
        )
        render_performance(context.console, report)
