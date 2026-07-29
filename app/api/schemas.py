"""Typed representations of Meta Marketing API responses.

Every Graph API payload is validated into one of these models at the transport
boundary, so no layer above the client handles raw dictionaries. A Graph API
version bump then surfaces as a single validation error naming the field that
moved, instead of a ``KeyError`` raised somewhere inside the recommendation
engine days later.

Two conventions matter here:

*Extra fields are ignored.* Meta adds fields to responses without notice, and a
strict model would fail on a payload that is perfectly usable.

*Budgets keep Meta's units.* Fields ending in ``_minor`` hold integers in the
account currency's minor unit exactly as transmitted. They are not converted
here because conversion needs the account's currency, which these payloads do
not carry; the synchronization service performs it once the account is known.
Insights spend is the exception and arrives already in major units, which is why
it is a plain :class:`~decimal.Decimal`.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Action types counted as a conversion, in priority order. Meta reports
# overlapping types for the same event — a single purchase can appear as
# `purchase`, `omni_purchase`, and `offsite_conversion.fb_pixel_purchase` — so
# the first type present is used and the rest ignored. Summing them would
# multiply one sale into three.
DEFAULT_CONVERSION_ACTION_TYPES: Final[tuple[str, ...]] = (
    "purchase",
    "omni_purchase",
    "offsite_conversion.fb_pixel_purchase",
    "onsite_web_purchase",
)


class MetaPayload(BaseModel):
    """Base for every Graph API payload model."""

    model_config = ConfigDict(
        extra="ignore",
        populate_by_name=True,
        frozen=True,
        str_strip_whitespace=True,
    )


class AdAccountPayload(MetaPayload):
    """An ad account as returned by ``/me/adaccounts`` or ``/{account-id}``.

    Attributes:
        remote_id: Meta's account ID, including the ``act_`` prefix.
        account_status: Meta's numeric account state; ``1`` means active.
    """

    remote_id: str = Field(alias="id")
    name: str | None = None
    currency: str | None = None
    timezone_name: str | None = None
    account_status: int | None = None
    business_name: str | None = None
    spend_cap_minor: int | None = Field(default=None, alias="spend_cap")
    amount_spent_minor: int | None = Field(default=None, alias="amount_spent")


class CampaignPayload(MetaPayload):
    """A campaign as returned by ``/{account-id}/campaigns``."""

    remote_id: str = Field(alias="id")
    name: str | None = None
    status: str | None = None
    effective_status: str | None = None
    objective: str | None = None
    buying_type: str | None = None
    bid_strategy: str | None = None
    daily_budget_minor: int | None = Field(default=None, alias="daily_budget")
    lifetime_budget_minor: int | None = Field(default=None, alias="lifetime_budget")
    start_time: datetime | None = None
    stop_time: datetime | None = None
    created_time: datetime | None = None


class AdSetPayload(MetaPayload):
    """An ad set as returned by ``/{account-id}/adsets``."""

    remote_id: str = Field(alias="id")
    campaign_remote_id: str | None = Field(default=None, alias="campaign_id")
    name: str | None = None
    status: str | None = None
    effective_status: str | None = None
    optimization_goal: str | None = None
    billing_event: str | None = None
    daily_budget_minor: int | None = Field(default=None, alias="daily_budget")
    lifetime_budget_minor: int | None = Field(default=None, alias="lifetime_budget")
    bid_amount_minor: int | None = Field(default=None, alias="bid_amount")
    start_time: datetime | None = None
    end_time: datetime | None = None
    created_time: datetime | None = None


class AdPayload(MetaPayload):
    """An ad as returned by ``/{account-id}/ads``.

    Meta nests the creative reference as ``{"creative": {"id": "..."}}``. It is
    flattened during validation so that consumers see a plain identifier rather
    than reaching into a nested mapping that may be absent.
    """

    remote_id: str = Field(alias="id")
    ad_set_remote_id: str | None = Field(default=None, alias="adset_id")
    creative_remote_id: str | None = None
    name: str | None = None
    status: str | None = None
    effective_status: str | None = None
    created_time: datetime | None = None

    @model_validator(mode="before")
    @classmethod
    def _flatten_creative_reference(cls, data: object) -> object:
        """Lift ``creative.id`` to a top-level ``creative_remote_id``."""
        if not isinstance(data, dict):
            return data
        creative = data.get("creative")
        if isinstance(creative, dict) and "id" in creative:
            return {**data, "creative_remote_id": creative["id"]}
        return data


class AdCreativePayload(MetaPayload):
    """A creative as returned by ``/{account-id}/adcreatives``."""

    remote_id: str = Field(alias="id")
    name: str | None = None
    title: str | None = None
    body: str | None = None
    call_to_action_type: str | None = None
    object_type: str | None = None
    thumbnail_url: str | None = None
    image_url: str | None = None
    video_id: str | None = None


class ActionPayload(MetaPayload):
    """One attributed action within an insights row.

    Attributes:
        action_type: Meta's identifier for the event, e.g. ``purchase``.
        value: Count when read from ``actions``, monetary value when read from
            ``action_values``. Meta uses the same shape for both.
    """

    action_type: str
    value: Decimal = Decimal(0)


class InsightsPayload(MetaPayload):
    """One row from the ``/insights`` edge.

    Only measured quantities are modelled. Meta will also compute ``ctr``,
    ``cpc``, and ``cpm`` on request, but those are read here from
    :mod:`app.analytics.metrics` instead, so that a metric has one definition
    rather than two that can disagree when rows are aggregated.

    Attributes:
        spend: Already in major currency units, unlike budgets.
    """

    date_start: date
    date_stop: date
    account_remote_id: str | None = Field(default=None, alias="account_id")
    campaign_remote_id: str | None = Field(default=None, alias="campaign_id")
    ad_set_remote_id: str | None = Field(default=None, alias="adset_id")
    ad_remote_id: str | None = Field(default=None, alias="ad_id")
    campaign_name: str | None = None
    adset_name: str | None = None
    ad_name: str | None = None
    spend: Decimal = Decimal(0)
    impressions: int = 0
    clicks: int = 0
    reach: int = 0
    actions: list[ActionPayload] = Field(default_factory=list)
    action_values: list[ActionPayload] = Field(default_factory=list)

    def conversion_count(
        self,
        action_types: tuple[str, ...] = DEFAULT_CONVERSION_ACTION_TYPES,
    ) -> int:
        """Count conversions using the first matching action type.

        Args:
            action_types: Candidate types in priority order.

        Returns:
            The conversion count, or ``0`` when none of the types are present.
        """
        matched = _first_matching_action(self.actions, action_types)
        return 0 if matched is None else int(matched.value)

    def conversion_value(
        self,
        action_types: tuple[str, ...] = DEFAULT_CONVERSION_ACTION_TYPES,
    ) -> Decimal:
        """Sum the revenue attributed to the first matching action type.

        Args:
            action_types: Candidate types in priority order.

        Returns:
            The attributed value, or ``Decimal(0)`` when absent.
        """
        matched = _first_matching_action(self.action_values, action_types)
        return Decimal(0) if matched is None else matched.value


def _first_matching_action(
    actions: list[ActionPayload],
    action_types: tuple[str, ...],
) -> ActionPayload | None:
    """Return the first action whose type appears earliest in ``action_types``.

    Priority is taken from ``action_types``, not from the order Meta happened to
    return, so the same event is selected consistently across rows.
    """
    by_type = {action.action_type: action for action in actions}
    for action_type in action_types:
        matched = by_type.get(action_type)
        if matched is not None:
            return matched
    return None
