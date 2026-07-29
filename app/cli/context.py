"""The composition root: builds and wires every object the CLI needs.

This is the only place in the application where concrete implementations are
chosen and connected. Every class below this layer receives its collaborators
through its constructor, which is what makes the "no globals, no singletons"
rule achievable rather than aspirational — there is no module-level engine, no
lazily-initialized settings accessor, and nothing anywhere else that reaches out
for a dependency instead of being handed one.

Construction is cheap and connects to nothing: SQLAlchemy opens no connection
until a session is used, and the HTTP client opens no socket until a request is
made. A command that fails validation therefore costs no I/O.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

import typer
from rich.console import Console
from sqlalchemy import Engine

from app.api.client import MetaMarketingClient
from app.auth.credentials import MetaCredentials
from app.config.settings import AppSettings, load_settings
from app.database.session import create_database_engine, create_session_factory
from app.recommendations.engine import RecommendationEngine
from app.repositories.unit_of_work import UnitOfWorkFactory
from app.services.account_service import AccountService
from app.services.campaign_service import CampaignService
from app.services.creative_service import CreativeService
from app.services.insight_service import InsightService
from app.services.optimization_service import OptimizationService
from app.services.sync_service import SyncService
from app.utils.logging import configure_logging

_logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ApplicationContext:
    """Every service a command may use, plus the resources backing them.

    Commands receive this and reach only for services. The engine and client are
    held so that :meth:`close` can release them; a command has no reason to
    touch either.

    Attributes:
        settings: The validated configuration everything was built from.
        console: Rich console for command output. Writes to stdout, while logs
            go to stderr, so piping a table gives a table.
        accounts: Ad account queries.
        campaigns: Campaign queries.
        creatives: Creative library queries.
        insights: Performance reporting.
        optimization: Recommendation generation and application.
    """

    settings: AppSettings
    console: Console
    accounts: AccountService
    campaigns: CampaignService
    creatives: CreativeService
    insights: InsightService
    optimization: OptimizationService
    _engine: Engine
    _client: MetaMarketingClient

    def close(self) -> None:
        """Release the HTTP connection pool and the database connection pool.

        Called when the CLI process finishes. Without it, the interpreter exits
        with pooled connections still open, which PostgreSQL logs as unexpected
        client disconnections.
        """
        self._client.close()
        self._engine.dispose()
        _logger.debug("Application resources released")


def build_application_context(*, console: Console | None = None) -> ApplicationContext:
    """Construct the fully wired application.

    Args:
        console: Rich console for command output. Injected so tests can capture
            it; defaults to a stdout console.

    Returns:
        A context whose services are ready to use.

    Raises:
        ConfigurationError: If the environment does not describe a usable
            configuration. Raised before any connection is attempted, so a
            missing token is reported as a configuration problem rather than as
            a failed request.
    """
    settings = load_settings()
    configure_logging(settings.logging)

    output_console = console if console is not None else Console()

    engine = create_database_engine(settings.database)
    session_factory = create_session_factory(engine)
    unit_of_work_factory = UnitOfWorkFactory(session_factory)

    credentials = MetaCredentials.from_settings(settings.meta)
    client = MetaMarketingClient(settings=settings.meta, credentials=credentials)

    sync_service = SyncService(unit_of_work_factory=unit_of_work_factory, client=client)
    insight_service = InsightService(
        unit_of_work_factory=unit_of_work_factory,
        sync_service=sync_service,
    )

    return ApplicationContext(
        settings=settings,
        console=output_console,
        accounts=AccountService(
            unit_of_work_factory=unit_of_work_factory,
            sync_service=sync_service,
        ),
        campaigns=CampaignService(
            unit_of_work_factory=unit_of_work_factory,
            sync_service=sync_service,
        ),
        creatives=CreativeService(
            unit_of_work_factory=unit_of_work_factory,
            sync_service=sync_service,
        ),
        insights=insight_service,
        optimization=OptimizationService(
            unit_of_work_factory=unit_of_work_factory,
            insight_service=insight_service,
            engine=RecommendationEngine(),
            client=client,
        ),
        _engine=engine,
        _client=client,
    )


class ApplicationContextProvider:
    """Builds the application context on first use, then reuses it.

    Deferring construction is what allows ``meta campaigns --help`` to work on a
    machine with no Meta token and no database. Click runs the group callback
    before it resolves a subcommand's ``--help``, so building eagerly there made
    reading the documentation require a fully configured environment — the exact
    moment a user is least likely to have one.

    The context is built at most once per invocation, so two commands in the
    same process share one engine and one connection pool.
    """

    def __init__(self, builder: Callable[[], ApplicationContext]) -> None:
        self._builder = builder
        self._context: ApplicationContext | None = None

    def get(self) -> ApplicationContext:
        """Return the context, constructing it if this is the first request.

        Returns:
            The wired application.

        Raises:
            ConfigurationError: If the environment is not usable.
        """
        if self._context is None:
            self._context = self._builder()
        return self._context

    def close(self) -> None:
        """Release resources, if any were ever acquired.

        A command that failed argument parsing never built a context, and
        closing one that does not exist would construct it purely in order to
        tear it down.
        """
        if self._context is not None:
            self._context.close()
            self._context = None


def application_context(ctx: typer.Context) -> ApplicationContext:
    """Retrieve the application context for this invocation, building it if needed.

    Args:
        ctx: The Typer context passed to a command.

    Returns:
        The wired application.

    Raises:
        TypeError: If the root callback did not run, leaving no provider to ask.
            This is a wiring defect rather than a user error, so it is
            deliberately not a
            :class:`~app.utils.exceptions.MetaOptimizerError` and is not caught
            by the CLI error handler.
        ConfigurationError: If the environment does not describe a usable
            configuration. Callers invoke this inside
            :func:`~app.cli.error_handling.handle_domain_errors`, so it is
            reported as a readable panel rather than a traceback.
    """
    provider = ctx.obj
    if not isinstance(provider, ApplicationContextProvider):
        message = (
            f"Application context is missing; the root CLI callback did not run "
            f"(got {type(ctx.obj).__name__})"
        )
        raise TypeError(message)
    return provider.get()
