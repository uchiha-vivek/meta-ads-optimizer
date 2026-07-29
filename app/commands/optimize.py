"""The ``meta optimize`` command."""

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
from app.cli.rendering import render_recommendations


def optimize_command(
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
        help="Aggregation level to evaluate: campaign, adset, or ad.",
    ),
    days: int = typer.Option(
        DEFAULT_LOOKBACK_DAYS,
        "--days",
        "-d",
        help="Length of the evaluation window, ending yesterday.",
    ),
    sync: bool = typer.Option(
        False,
        "--sync",
        help="Fetch fresh insights from Meta before evaluating.",
    ),
    detail: bool = typer.Option(
        False,
        "--detail",
        help="Print the full reasoning behind each recommendation.",
    ),
    open_only: bool = typer.Option(
        False,
        "--open",
        help="List outstanding recommendations without re-evaluating.",
    ),
    apply_id: int = typer.Option(
        0,
        "--apply",
        help="Apply one recommendation by ID. This changes the live ad account.",
    ),
    dismiss_id: int = typer.Option(
        0,
        "--dismiss",
        help="Mark one recommendation as rejected, by ID.",
    ),
) -> None:
    """Generate optimization recommendations, and optionally act on them.

    Running with no flags evaluates the account and prints findings; it changes
    nothing in Meta. Applying a change is a separate, explicit request naming a
    single recommendation by ID, because the one irreversible thing this tool
    can do should never be a side effect of asking it a question.
    """
    with handle_domain_errors():
        context = application_context(ctx)
        resolved_account_id = resolve_account_id(context, account_id or None)

        if apply_id:
            applied = context.optimization.apply_recommendation(apply_id)
            context.console.print(
                f"Applied recommendation {applied.id}: {applied.title}",
                style="green",
            )
            return

        if dismiss_id:
            dismissed = context.optimization.dismiss_recommendation(dismiss_id)
            context.console.print(f"Dismissed recommendation {dismissed.id}", style="yellow")
            return

        if open_only:
            outstanding = context.optimization.list_open_recommendations(resolved_account_id)
            render_recommendations(context.console, outstanding, show_rationale=detail)
            return

        since, until = resolve_window(days)
        result = context.optimization.generate_recommendations(
            resolved_account_id,
            level=parse_level(level),
            since=since,
            until=until,
            refresh=sync,
        )
        context.console.print(
            f"Evaluated {result.entities_evaluated} {result.level.value}(s) over "
            f"{since.isoformat()} to {until.isoformat()}."
        )
        render_recommendations(context.console, result.recommendations, show_rationale=detail)
