"""The ``meta campaigns`` command."""

from __future__ import annotations

import typer

from app.cli.context import application_context
from app.cli.error_handling import handle_domain_errors
from app.cli.options import parse_statuses, resolve_account_id
from app.cli.rendering import render_campaigns


def campaigns_command(
    ctx: typer.Context,
    account_id: str = typer.Option(
        "",
        "--account-id",
        "-a",
        help="Meta ad account ID (act_...). Defaults to META_AD_ACCOUNT_ID.",
    ),
    status: str = typer.Option(
        "",
        "--status",
        "-s",
        help="Comma-separated statuses to include, e.g. 'active,paused'.",
    ),
    name: str = typer.Option(
        "",
        "--name",
        "-n",
        help="Case-insensitive substring match on the campaign name.",
    ),
    sync: bool = typer.Option(
        False,
        "--sync",
        help="Fetch the account structure from Meta before listing.",
    ),
) -> None:
    """List an ad account's campaigns.

    Filters are applied in the database rather than after loading, so narrowing
    by status on an account with thousands of archived campaigns stays fast.
    """
    with handle_domain_errors():
        context = application_context(ctx)
        resolved_account_id = resolve_account_id(context, account_id or None)
        campaigns = context.campaigns.list_campaigns(
            resolved_account_id,
            statuses=parse_statuses(status or None),
            name_contains=name or None,
            refresh=sync,
        )
        account = context.accounts.get_account(resolved_account_id)
        render_campaigns(context.console, campaigns, currency=account.currency)
