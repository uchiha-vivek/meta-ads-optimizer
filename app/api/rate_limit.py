"""Interpretation of Meta's rate limit headers.

Meta reports throttling budgets in response headers rather than only rejecting
requests once exhausted. That advance warning is what makes it possible to slow
down *before* being blocked, which matters because Meta's penalty for exhausting
a budget is measured in minutes: once blocked, no amount of retrying helps and
the whole sync stalls.

Two headers are read. ``X-App-Usage`` reports the app-wide budget, and
``X-Business-Use-Case-Usage`` reports per-ad-account budgets and is the one that
usually binds first during a sync. Both carry JSON, and both are advisory — Meta
omits them freely, so absence is normal and never treated as an error.

This module only parses and interprets. Deciding to pause belongs to the client,
which owns the clock.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

_logger = logging.getLogger(__name__)

APP_USAGE_HEADER: Final[str] = "X-App-Usage"
BUSINESS_USE_CASE_USAGE_HEADER: Final[str] = "X-Business-Use-Case-Usage"

# Percentage of a budget at which the client slows down of its own accord.
# Chosen below 100 because usage is reported for the request that just
# completed: at 100 the next request is already refused.
DEFAULT_THROTTLE_THRESHOLD_PERCENT: Final[int] = 90

_MAX_PERCENT: Final[int] = 100
_MINUTES_TO_SECONDS: Final[int] = 60


@dataclass(frozen=True, slots=True)
class RateLimitUsage:
    """A snapshot of how much of a Meta rate limit budget is consumed.

    Meta meters three independent budgets and blocks when any one is exhausted,
    so the binding constraint is the worst of the three, not their average.

    Attributes:
        call_count_percent: Share of the permitted request count consumed.
        total_cputime_percent: Share of the permitted CPU time consumed.
        total_time_percent: Share of the permitted wall-clock time consumed.
        estimated_regain_seconds: Meta's estimate of how long until access is
            restored, converted from the minutes it reports. Zero when not
            currently blocked.
    """

    call_count_percent: int
    total_cputime_percent: int
    total_time_percent: int
    estimated_regain_seconds: float

    @property
    def worst_percent(self) -> int:
        """The most consumed of the three budgets."""
        return max(self.call_count_percent, self.total_cputime_percent, self.total_time_percent)

    @property
    def is_exhausted(self) -> bool:
        """Whether any budget has reached its limit."""
        return self.worst_percent >= _MAX_PERCENT

    def should_throttle(self, threshold_percent: int = DEFAULT_THROTTLE_THRESHOLD_PERCENT) -> bool:
        """Whether the caller should slow down before the next request.

        Args:
            threshold_percent: Consumption at or above which to pause.

        Returns:
            ``True`` when the worst budget has reached the threshold.
        """
        return self.worst_percent >= threshold_percent


def parse_rate_limit_headers(headers: Mapping[str, str]) -> RateLimitUsage | None:
    """Extract the most constraining usage figures from response headers.

    Both headers are considered and the higher consumption wins, because being
    under the app-wide budget is no help when the per-account budget is spent.

    Malformed JSON is logged and ignored rather than raised: a header the
    application cannot parse is not a reason to fail a request that Meta already
    answered successfully.

    Args:
        headers: Response headers, matched case-insensitively.

    Returns:
        The binding usage snapshot, or ``None`` when neither header is present
        or parseable.
    """
    normalized = {key.lower(): value for key, value in headers.items()}

    candidates: list[RateLimitUsage] = []

    app_usage = _parse_app_usage(normalized.get(APP_USAGE_HEADER.lower()))
    if app_usage is not None:
        candidates.append(app_usage)

    business_usage = _parse_business_use_case_usage(
        normalized.get(BUSINESS_USE_CASE_USAGE_HEADER.lower())
    )
    candidates.extend(business_usage)

    if not candidates:
        return None
    return max(candidates, key=lambda usage: usage.worst_percent)


def _parse_app_usage(raw_value: str | None) -> RateLimitUsage | None:
    """Parse the ``X-App-Usage`` header, which carries a single object."""
    decoded = _decode_json_header(raw_value, APP_USAGE_HEADER)
    if not isinstance(decoded, dict):
        return None
    return _usage_from_mapping(decoded)


def _parse_business_use_case_usage(raw_value: str | None) -> list[RateLimitUsage]:
    """Parse ``X-Business-Use-Case-Usage``, keyed by account with list values."""
    decoded = _decode_json_header(raw_value, BUSINESS_USE_CASE_USAGE_HEADER)
    if not isinstance(decoded, dict):
        return []

    usages: list[RateLimitUsage] = []
    for entries in decoded.values():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, dict):
                usages.append(_usage_from_mapping(entry))
    return usages


def _decode_json_header(raw_value: str | None, header_name: str) -> object:
    """Decode a JSON-valued header, returning ``None`` when unusable."""
    if not raw_value:
        return None
    try:
        return json.loads(raw_value)
    except (TypeError, ValueError):
        _logger.warning(
            "Ignoring unparseable rate limit header",
            extra={"header": header_name, "raw_value": raw_value},
        )
        return None


def _usage_from_mapping(payload: Mapping[str, Any]) -> RateLimitUsage:
    """Build a usage snapshot from one decoded header object."""
    return RateLimitUsage(
        call_count_percent=_coerce_percent(payload.get("call_count")),
        total_cputime_percent=_coerce_percent(payload.get("total_cputime")),
        total_time_percent=_coerce_percent(payload.get("total_time")),
        estimated_regain_seconds=_coerce_percent(payload.get("estimated_time_to_regain_access"))
        * _MINUTES_TO_SECONDS,
    )


def _coerce_percent(value: object) -> int:
    """Coerce a header field to a non-negative integer, defaulting to zero.

    Meta types these fields inconsistently across use cases, sending integers in
    some responses and numeric strings in others.
    """
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(value, 0)
    if isinstance(value, float):
        return max(int(value), 0)
    if isinstance(value, str):
        try:
            return max(int(float(value)), 0)
        except ValueError:
            return 0
    return 0
