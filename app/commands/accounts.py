"""The ``meta accounts`` command."""

from __future__ import annotations

import typer

from app.cli.context import application_context
from app.cli.error_handling import handle_domain_errors
from app.cli.rendering import render_accounts


def accounts_command(
    ctx: typer.Context,
    sync: bool = typer.Option(
        False,
        "--sync",
        help="Fetch accounts from Meta before listing. Required on first use.",
    ),
) -> None:
    """List the ad accounts this token can reach.

    Reads from the local database. Nothing is stored until a sync has run, so
    the first invocation on a fresh install needs ``--sync``.
    """
    with handle_domain_errors():
        context = application_context(ctx)
        accounts = context.accounts.list_accounts(refresh=sync)
        render_accounts(context.console, accounts)
