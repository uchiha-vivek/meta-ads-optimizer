"""Closed value sets shared by the ORM models, services, and the CLI.

Defined once so that a status is never compared against a bare string literal.
Values are lower-case and match Meta's own vocabulary where one exists, which is
why :class:`InsightLevel` members are exactly the strings the Graph API accepts
for its ``level`` parameter — the enum can be sent straight to the API without a
translation table that could drift.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final


class EntityStatus(StrEnum):
    """Configured delivery status of a campaign, ad set, or ad.

    This is the status the advertiser set, not the status Meta computes. An
    ``ACTIVE`` campaign whose parent is paused does not deliver; that
    distinction lives in each model's ``effective_status`` field, which is kept
    as free text because Meta extends its vocabulary without notice.
    """

    ACTIVE = "active"
    PAUSED = "paused"
    DELETED = "deleted"
    ARCHIVED = "archived"
    UNKNOWN = "unknown"

    @classmethod
    def from_meta(cls, raw_status: str | None) -> EntityStatus:
        """Convert Meta's upper-case status string into a member.

        Unrecognized values become :attr:`UNKNOWN` rather than raising. A status
        the application has never seen is not a reason to abandon a
        synchronization run covering hundreds of healthy campaigns; the row is
        still worth storing.

        Args:
            raw_status: Status string as returned by the Graph API, or ``None``.

        Returns:
            The matching member, or :attr:`UNKNOWN`.
        """
        if raw_status is None:
            return cls.UNKNOWN
        try:
            return cls(raw_status.strip().lower())
        except ValueError:
            return cls.UNKNOWN

    @property
    def is_delivering(self) -> bool:
        """Whether an entity in this state is configured to spend money."""
        return self is EntityStatus.ACTIVE


class InsightLevel(StrEnum):
    """Aggregation level of an insights row.

    Member values are the exact strings the Graph API's ``level`` parameter
    accepts, so they are passed through without translation.
    """

    ACCOUNT = "account"
    CAMPAIGN = "campaign"
    ADSET = "adset"
    AD = "ad"


class RecommendationSeverity(StrEnum):
    """How urgently a recommendation deserves attention."""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        """Sort key, ascending by urgency.

        Enum members are not ordered by declaration, so an explicit rank is
        needed to sort a recommendation list with the most urgent items first.
        """
        return _SEVERITY_RANKS[self]


class RecommendationAction(StrEnum):
    """The concrete change a recommendation proposes.

    This is the discriminator ``meta optimize --apply`` switches on to decide
    which API call to make, which is why the set is closed: an action with no
    corresponding implementation must be impossible to construct.
    """

    INCREASE_BUDGET = "increase_budget"
    DECREASE_BUDGET = "decrease_budget"
    PAUSE_ENTITY = "pause_entity"
    ROTATE_CREATIVE = "rotate_creative"
    REVIEW_TARGETING = "review_targeting"
    REVIEW_CONVERSION_TRACKING = "review_conversion_tracking"

    @property
    def is_automatable(self) -> bool:
        """Whether this action can be applied through the API without a human.

        Budget changes and pauses are single, reversible API calls. Rotating a
        creative or revising targeting requires judgement the engine does not
        have, so those are reported and never applied automatically.
        """
        return self in _AUTOMATABLE_ACTIONS


class RecommendationStatus(StrEnum):
    """Lifecycle state of a stored recommendation."""

    OPEN = "open"
    APPLIED = "applied"
    DISMISSED = "dismissed"
    SUPERSEDED = "superseded"


_SEVERITY_RANKS: Final[dict[RecommendationSeverity, int]] = {
    RecommendationSeverity.INFO: 0,
    RecommendationSeverity.WARNING: 1,
    RecommendationSeverity.CRITICAL: 2,
}

_AUTOMATABLE_ACTIONS: Final[frozenset[RecommendationAction]] = frozenset(
    {
        RecommendationAction.INCREASE_BUDGET,
        RecommendationAction.DECREASE_BUDGET,
        RecommendationAction.PAUSE_ENTITY,
    }
)
