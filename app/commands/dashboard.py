"""The ``meta dashboard`` command."""

from __future__ import annotations

import typer

from app.cli.context import application_context
from app.cli.error_handling import handle_domain_errors
from app.cli.options import resolve_account_id
from app.tui.dashboard import DashboardApp


def dashboard_command(
    ctx: typer.Context,
    account_id: str = typer.Option(
        "",
        "--account-id",
        "-a",
        help="Meta ad account ID (act_...). Defaults to META_AD_ACCOUNT_ID.",
    ),
) -> None:
    """Open the interactive dashboard for an ad account.

    A full-screen terminal view over the same data the other commands print:
    the account's campaigns, their performance this period against last, the
    creative library, and the outstanding recommendations. Findings can be
    applied or dismissed in place; applying is the one action that changes the
    live account, and it obeys the same rules as ``meta optimize --apply``.
    """
    with handle_domain_errors():
        context = application_context(ctx)
        resolved_account_id = resolve_account_id(context, account_id or None)
        DashboardApp(context=context, account_remote_id=resolved_account_id).run()
