"""The ``meta creatives`` command."""

from __future__ import annotations

import typer

from app.cli.context import application_context
from app.cli.error_handling import handle_domain_errors
from app.cli.options import resolve_account_id
from app.cli.rendering import render_creatives


def creatives_command(
    ctx: typer.Context,
    account_id: str = typer.Option(
        "",
        "--account-id",
        "-a",
        help="Meta ad account ID (act_...). Defaults to META_AD_ACCOUNT_ID.",
    ),
    in_use: bool = typer.Option(
        False,
        "--in-use",
        help="Show only creatives referenced by at least one delivering ad.",
    ),
    sync: bool = typer.Option(
        False,
        "--sync",
        help="Fetch the account structure from Meta before listing.",
    ),
) -> None:
    """List an ad account's creative library with deployment counts.

    The counts are the point: a creative used by thirty ads is a template whose
    weakness is systemic, while one used by a single ad is a local problem.
    Mature accounts accumulate hundreds of retired creatives, so ``--in-use``
    narrows the list to those currently spending money.
    """
    with handle_domain_errors():
        context = application_context(ctx)
        resolved_account_id = resolve_account_id(context, account_id or None)
        usages = context.creatives.list_creatives(
            resolved_account_id,
            refresh=sync,
            in_use_only=in_use,
        )
        render_creatives(context.console, usages)
