"""The ``meta`` command-line entry point.

Assembles the Typer application and registers each command. Command bodies live
in :mod:`app.commands`; this module only wires them, so adding a command means
adding a module and one registration line rather than editing a growing
dispatcher.

The root callback builds the application context once per invocation and hands
it to commands through Typer's context object, which is what lets commands
receive fully wired services without any of them reaching for a global.
"""

from __future__ import annotations

import typer

from app.cli.context import ApplicationContextProvider, build_application_context
from app.commands.accounts import accounts_command
from app.commands.campaigns import campaigns_command
from app.commands.creatives import creatives_command
from app.commands.dashboard import dashboard_command
from app.commands.insights import insights_command
from app.commands.optimize import optimize_command

app = typer.Typer(
    name="meta",
    help="Analyze and optimize Meta advertising campaigns.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_show_locals=False,
)


@app.callback()
def configure(ctx: typer.Context) -> None:
    """Attach a lazy application context to this invocation.

    Nothing is constructed here. Click runs this callback before it resolves a
    subcommand's ``--help``, so building eagerly would make reading the
    documentation require a valid Meta token and a reachable database. The
    context is built by the first command body that asks for it.

    An already-attached provider is left in place. That is what allows a test to
    drive the real command bodies against stub services, so the wiring between
    CLI and services is covered rather than assumed.

    Args:
        ctx: Typer context, which carries the provider to the command.
    """
    if isinstance(ctx.obj, ApplicationContextProvider):
        return

    provider = ApplicationContextProvider(build_application_context)
    ctx.obj = provider
    # Registered rather than done in a finally block so it also runs when a
    # command exits through typer.Exit, which is how every handled error leaves.
    # It is a no-op when no command ever built a context.
    ctx.call_on_close(provider.close)


app.command(name="accounts")(accounts_command)
app.command(name="campaigns")(campaigns_command)
app.command(name="insights")(insights_command)
app.command(name="optimize")(optimize_command)
app.command(name="creatives")(creatives_command)
app.command(name="dashboard")(dashboard_command)


def run() -> None:
    """Invoke the CLI. Used as the console script entry point."""
    app()


if __name__ == "__main__":
    run()
