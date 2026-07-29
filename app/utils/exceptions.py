"""Custom exception hierarchy for meta-optimizer.

Every failure raised by application code derives from :class:`MetaOptimizerError`.
This lets the CLI draw the distinction that matters to a user: an expected,
actionable failure (expired token, rate limit, unknown campaign) is reported as
a clean message and a non-zero exit code, while anything else is a genuine bug
and keeps its traceback.

Exceptions carry a structured ``context`` mapping rather than interpolating
values into the message. The same data can then be rendered for a human and
emitted as JSON log fields without being parsed back out of a string.
"""

from __future__ import annotations

from typing import Any


class MetaOptimizerError(Exception):
    """Base class for every error raised by meta-optimizer."""

    def __init__(self, message: str, *, context: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.context: dict[str, Any] = dict(context or {})

    def __str__(self) -> str:
        """Render the message followed by any structured context."""
        if not self.context:
            return self.message
        rendered = ", ".join(f"{key}={value!r}" for key, value in sorted(self.context.items()))
        return f"{self.message} ({rendered})"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class ConfigurationError(MetaOptimizerError):
    """Raised when the environment does not describe a usable configuration.

    Raised during startup only. Reaching any other layer with invalid
    configuration is treated as a defect, because the process should have
    refused to start.
    """


# ---------------------------------------------------------------------------
# Meta Marketing API
# ---------------------------------------------------------------------------


class MetaApiError(MetaOptimizerError):
    """Base class for every failure originating from the Meta Marketing API.

    Attributes:
        status_code: HTTP status returned by Meta, when a response was received.
        error_code: Meta's numeric ``error.code`` discriminator.
        error_subcode: Meta's numeric ``error.error_subcode``, when present.
        fbtrace_id: Opaque trace identifier. Meta support cannot investigate a
            failure without it, so it is preserved and logged verbatim.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_code: int | None = None,
        error_subcode: int | None = None,
        fbtrace_id: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, context=context)
        self.status_code = status_code
        self.error_code = error_code
        self.error_subcode = error_subcode
        self.fbtrace_id = fbtrace_id


class MetaApiTransportError(MetaApiError):
    """Raised when the request never produced a usable HTTP response.

    Covers connection failures, DNS errors, TLS failures and timeouts. These are
    retried automatically; this exception means retries were exhausted.
    """


class MetaApiAuthenticationError(MetaApiError):
    """Raised when Meta rejects the access token.

    Never retried: an expired, revoked, or malformed token will be rejected
    identically on every attempt, so retrying only delays the error the operator
    needs to see.
    """


class MetaApiPermissionError(MetaApiError):
    """Raised when the token is valid but lacks the permission for the call.

    Typically a token holding ``ads_read`` being used for a write performed by
    ``meta optimize``, which requires ``ads_management``.
    """


class MetaApiRateLimitError(MetaApiError):
    """Raised when Meta throttles the application and retries were exhausted.

    Attributes:
        retry_after_seconds: How long to wait before retrying, when Meta
            supplies an estimate. ``None`` when no estimate was provided.
    """

    def __init__(
        self,
        message: str,
        *,
        retry_after_seconds: float | None = None,
        status_code: int | None = None,
        error_code: int | None = None,
        error_subcode: int | None = None,
        fbtrace_id: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            message,
            status_code=status_code,
            error_code=error_code,
            error_subcode=error_subcode,
            fbtrace_id=fbtrace_id,
            context=context,
        )
        self.retry_after_seconds = retry_after_seconds


class MetaApiResponseError(MetaApiError):
    """Raised when a successful HTTP response does not match the expected shape.

    A 200 carrying a payload the client cannot parse is a contract violation,
    usually caused by a Graph API version change. It is surfaced explicitly
    rather than allowed to become a ``KeyError`` deep inside a service.
    """


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


class DatabaseError(MetaOptimizerError):
    """Base class for failures in the persistence layer."""


class RepositoryError(DatabaseError):
    """Raised when a repository operation fails.

    Wraps ``sqlalchemy.exc.SQLAlchemyError`` so that layers above the repository
    never import SQLAlchemy in order to handle a failure.
    """


class EntityNotFoundError(RepositoryError):
    """Raised when a lookup that must succeed finds no matching row.

    Methods whose contract permits absence return ``None`` instead. This is for
    the case where a missing row means the caller was given a bad identifier.
    """


# ---------------------------------------------------------------------------
# Services and domain logic
# ---------------------------------------------------------------------------


class ServiceError(MetaOptimizerError):
    """Base class for failures in the service layer."""


class SynchronizationError(ServiceError):
    """Raised when synchronizing remote Meta state into the database fails."""


class OptimizationError(ServiceError):
    """Raised when generating or applying optimizations fails."""


class RecommendationError(MetaOptimizerError):
    """Raised when the recommendation engine cannot evaluate its rules.

    A rule that legitimately has nothing to say returns ``None``; this is for a
    rule that could not run at all, such as one given an incoherent context.
    """
