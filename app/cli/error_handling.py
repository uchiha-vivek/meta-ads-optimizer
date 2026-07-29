"""Translation of domain exceptions into CLI output and exit codes.

Every command body runs inside :func:`handle_domain_errors`. It draws the
distinction a user cares about: an expected, actionable failure — an expired
token, an account that has not been synchronized, a rate limit — is reported as
a readable message and a non-zero exit code, while anything else is a bug and
keeps its traceback so it can be diagnosed rather than swallowed.

Exit codes are distinct per failure class so that a shell script or scheduler
can react differently to "the token expired" than to "we were throttled", which
is the difference between paging someone and retrying in ten minutes.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Final

import typer
from rich.console import Console
from rich.panel import Panel

from app.utils.exceptions import (
    ConfigurationError,
    DatabaseError,
    MetaApiAuthenticationError,
    MetaApiError,
    MetaApiPermissionError,
    MetaApiRateLimitError,
    MetaOptimizerError,
)

_logger = logging.getLogger(__name__)

EXIT_GENERAL_ERROR: Final[int] = 1
EXIT_CONFIGURATION_ERROR: Final[int] = 2
EXIT_AUTHENTICATION_ERROR: Final[int] = 3
EXIT_PERMISSION_ERROR: Final[int] = 4
EXIT_RATE_LIMITED: Final[int] = 5
EXIT_DATABASE_ERROR: Final[int] = 6


@dataclass(frozen=True, slots=True)
class FailureProfile:
    """How one class of failure is presented and reported.

    Attributes:
        error_type: The exception class this profile describes.
        title: Heading shown on the error panel.
        exit_code: Process exit status, distinct per class so a scheduler can
            distinguish a retryable throttle from an expired credential.
        remedy: Actionable guidance, or ``None`` when the message suffices.
    """

    error_type: type[MetaOptimizerError]
    title: str
    exit_code: int
    remedy: str | None


# Ordered most specific first: the first matching profile wins, so
# MetaApiRateLimitError must precede the MetaApiError it derives from.
_FAILURE_PROFILES: Final[tuple[FailureProfile, ...]] = (
    FailureProfile(
        error_type=ConfigurationError,
        title="Configuration error",
        exit_code=EXIT_CONFIGURATION_ERROR,
        remedy=(
            "Check the variables in your .env against .env.example. "
            "Inside Docker, run: docker compose config"
        ),
    ),
    FailureProfile(
        error_type=MetaApiAuthenticationError,
        title="Authentication failed",
        exit_code=EXIT_AUTHENTICATION_ERROR,
        remedy=(
            "META_ACCESS_TOKEN is expired, revoked, or issued for a different app. "
            "Generate a fresh system user token in Business Manager; user tokens "
            "expire after 60 days, system user tokens do not."
        ),
    ),
    FailureProfile(
        error_type=MetaApiPermissionError,
        title="Insufficient permissions",
        exit_code=EXIT_PERMISSION_ERROR,
        remedy=(
            "The token is valid but lacks the required permission. Read commands "
            "need ads_read; applying an optimization needs ads_management."
        ),
    ),
    FailureProfile(
        error_type=MetaApiRateLimitError,
        title="Rate limited by Meta",
        exit_code=EXIT_RATE_LIMITED,
        remedy=(
            "Meta is throttling this app. Its penalty window is measured in "
            "minutes, so wait before retrying rather than re-running immediately."
        ),
    ),
    FailureProfile(
        error_type=MetaApiError,
        title="Meta API error",
        exit_code=EXIT_GENERAL_ERROR,
        remedy=None,
    ),
    FailureProfile(
        error_type=DatabaseError,
        title="Database error",
        exit_code=EXIT_DATABASE_ERROR,
        remedy=(
            "The database rejected the operation. Confirm the stack is up with "
            "docker compose ps, and that migrations are applied with "
            "docker compose run --rm app alembic upgrade head"
        ),
    ),
)

_FALLBACK_PROFILE: Final[FailureProfile] = FailureProfile(
    error_type=MetaOptimizerError,
    title="Error",
    exit_code=EXIT_GENERAL_ERROR,
    remedy=None,
)


@contextmanager
def handle_domain_errors(console: Console | None = None) -> Iterator[None]:
    """Run a command body, reporting known failures instead of raising.

    The block must enclose the application context lookup as well as the work
    itself. Building the context is where a :class:`ConfigurationError` is
    raised, and a misconfigured environment is precisely the case that most
    needs a readable message rather than a traceback.

    Args:
        console: Console the error panel is written to. Defaults to stderr,
            which keeps diagnostics out of piped table output and works before
            an application context exists.

    Yields:
        Control to the command body.

    Raises:
        typer.Exit: With a failure-specific exit code, when a known application
            error occurs. Unknown exceptions propagate untouched, because a bug
            must not be disguised as a handled condition.
    """
    destination = console if console is not None else Console(stderr=True)
    try:
        yield
    except MetaOptimizerError as error:
        profile = profile_for(error)
        _report(destination, error, profile)
        raise typer.Exit(code=profile.exit_code) from error


def profile_for(error: MetaOptimizerError) -> FailureProfile:
    """Return the presentation profile for a failure.

    Args:
        error: The raised application error.

    Returns:
        The first profile whose type matches, or a generic fallback. Exposed for
        tests, which assert on exit codes without invoking the CLI.
    """
    for profile in _FAILURE_PROFILES:
        if isinstance(error, profile.error_type):
            return profile
    return _FALLBACK_PROFILE


def _report(console: Console, error: MetaOptimizerError, profile: FailureProfile) -> None:
    """Print an error panel and log the failure with its structured context."""
    _logger.error(
        "Command failed: %s",
        type(error).__name__,
        extra={"error_type": type(error).__name__, **error.context},
    )

    body = str(error)
    if profile.remedy:
        body = f"{body}\n\n{profile.remedy}"
    if isinstance(error, MetaApiError) and error.fbtrace_id:
        # Meta support cannot investigate a failure without this identifier.
        body = f"{body}\n\nMeta trace ID: {error.fbtrace_id}"

    console.print(Panel(body, title=profile.title, border_style="red", expand=False))
