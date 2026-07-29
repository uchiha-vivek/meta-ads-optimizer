"""Reusable HTTP client for the Meta Marketing API.

Owns everything about talking to Meta and nothing about what the data means:
authentication, timeouts, retries with exponential backoff, rate limit
observance, pagination, error translation, and validation into the typed
payloads in :mod:`app.api.schemas`. Services consume those payloads and never
see a status code.

Three decisions shape this module.

*Time is injected.* ``sleeper`` defaults to :func:`time.sleep` but is a
constructor argument, so tests exercise the full retry and backoff path in
microseconds rather than actually waiting out the delays.

*Pagination uses explicit cursors.* Meta supplies a ``paging.next`` URL, but that
URL echoes back the query parameters originally sent, including
``appsecret_proof``. Following it and re-appending our own parameters would
transmit the proof twice. Passing the ``after`` cursor into a freshly built
request keeps one authoritative parameter set.

*Not every failure is retried.* An expired token fails identically on every
attempt, so retrying it only delays the message the operator needs while
consuming the rate limit budget. Retries are confined to failures that are
plausibly transient: connection errors, timeouts, ``429``, and ``5xx``.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Iterator, Mapping
from datetime import date
from types import TracebackType
from typing import Any, Final

import requests

from app.api.rate_limit import parse_rate_limit_headers
from app.api.schemas import (
    AdAccountPayload,
    AdCreativePayload,
    AdPayload,
    AdSetPayload,
    CampaignPayload,
    InsightsPayload,
)
from app.auth.credentials import MetaCredentials
from app.config.settings import MetaApiSettings
from app.utils.exceptions import (
    MetaApiAuthenticationError,
    MetaApiError,
    MetaApiPermissionError,
    MetaApiRateLimitError,
    MetaApiResponseError,
    MetaApiTransportError,
)

_logger = logging.getLogger(__name__)

# Meta status tokens. Declared here rather than imported from the domain enums
# so that the API layer stays independent of the persistence vocabulary.
META_STATUS_ACTIVE: Final[str] = "ACTIVE"
META_STATUS_PAUSED: Final[str] = "PAUSED"

# Objects per page. Meta caps page size per edge and silently reduces oversized
# requests; 100 is accepted everywhere used here.
_DEFAULT_PAGE_SIZE: Final[int] = 100

# Guards against a malformed cursor sending the loop around forever. At 100 per
# page this allows 100k objects, well beyond any real ad account.
_MAX_PAGES: Final[int] = 1_000

_HTTP_TOO_MANY_REQUESTS: Final[int] = 429
_HTTP_SERVER_ERROR_FLOOR: Final[int] = 500
_HTTP_UNAUTHORIZED: Final[int] = 401
_HTTP_FORBIDDEN: Final[int] = 403

# Meta error codes indicating the token itself is unusable.
_AUTHENTICATION_ERROR_CODES: Final[frozenset[int]] = frozenset({102, 190, 463, 467})

# Meta error codes indicating the token is valid but lacks the permission.
_PERMISSION_ERROR_CODES: Final[frozenset[int]] = frozenset({3, 10, 200, 294, 299})

# Meta error codes indicating throttling. 4/17/32/613 are the classic app, user,
# page, and custom limits; the 80000 range is business-use-case throttling.
_RATE_LIMIT_ERROR_CODES: Final[frozenset[int]] = frozenset(
    {4, 17, 32, 613, 80000, 80001, 80002, 80003, 80004, 80005, 80006, 80008, 80009, 80014}
)

_RETRY_BACKOFF_BASE: Final[int] = 2

_ACCOUNT_FIELDS: Final[tuple[str, ...]] = (
    "id",
    "name",
    "currency",
    "timezone_name",
    "account_status",
    "business_name",
    "spend_cap",
    "amount_spent",
)

_CAMPAIGN_FIELDS: Final[tuple[str, ...]] = (
    "id",
    "name",
    "status",
    "effective_status",
    "objective",
    "buying_type",
    "bid_strategy",
    "daily_budget",
    "lifetime_budget",
    "start_time",
    "stop_time",
    "created_time",
)

_AD_SET_FIELDS: Final[tuple[str, ...]] = (
    "id",
    "name",
    "campaign_id",
    "status",
    "effective_status",
    "optimization_goal",
    "billing_event",
    "daily_budget",
    "lifetime_budget",
    "bid_amount",
    "start_time",
    "end_time",
    "created_time",
)

_AD_FIELDS: Final[tuple[str, ...]] = (
    "id",
    "name",
    "adset_id",
    "status",
    "effective_status",
    "created_time",
    "creative{id}",
)

_AD_CREATIVE_FIELDS: Final[tuple[str, ...]] = (
    "id",
    "name",
    "title",
    "body",
    "call_to_action_type",
    "object_type",
    "thumbnail_url",
    "image_url",
    "video_id",
)

_INSIGHTS_BASE_FIELDS: Final[tuple[str, ...]] = (
    "date_start",
    "date_stop",
    "spend",
    "impressions",
    "clicks",
    "reach",
    "actions",
    "action_values",
)

# Identifier fields available at each level. Requesting a field the level cannot
# produce is rejected by Meta, so the set is chosen per level rather than
# requesting everything and hoping.
_INSIGHTS_FIELDS_BY_LEVEL: Final[dict[str, tuple[str, ...]]] = {
    "account": ("account_id",),
    "campaign": ("campaign_id", "campaign_name"),
    "adset": ("campaign_id", "adset_id", "adset_name"),
    "ad": ("campaign_id", "adset_id", "ad_id", "ad_name"),
}


class MetaMarketingClient:
    """Sends authenticated, retried, rate-limit-aware requests to Meta.

    Stateless with respect to the domain: it fetches and parses, and holds no
    opinion about what should be done with the result. That is what allows a
    single instance to serve every service.

    The underlying :class:`requests.Session` is reused across calls so that TLS
    handshakes and connection setup are not repeated for each of the hundreds of
    requests a full account sync issues.
    """

    def __init__(
        self,
        *,
        settings: MetaApiSettings,
        credentials: MetaCredentials,
        http_session: requests.Session | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._settings = settings
        self._credentials = credentials
        self._http_session = http_session if http_session is not None else requests.Session()
        self._owns_http_session = http_session is None
        self._sleeper = sleeper

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        """Release the underlying HTTP connection pool.

        Only closes a session this client created. A session passed in by a
        caller belongs to that caller, which may still be using it.
        """
        if self._owns_http_session:
            self._http_session.close()

    def __enter__(self) -> MetaMarketingClient:
        """Enter a context manager that closes the client on exit."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the client when leaving the context."""
        self.close()

    # -- ad accounts -------------------------------------------------------

    def list_ad_accounts(self) -> list[AdAccountPayload]:
        """List the ad accounts the token can access.

        Returns:
            Every accessible account.

        Raises:
            MetaApiError: If the request fails after retries.
        """
        parameters = {"fields": ",".join(_ACCOUNT_FIELDS)}
        return [
            AdAccountPayload.model_validate(item)
            for item in self._iterate_edge("me/adaccounts", parameters)
        ]

    def get_ad_account(self, account_remote_id: str) -> AdAccountPayload:
        """Fetch one ad account.

        Args:
            account_remote_id: Meta account ID, including the ``act_`` prefix.

        Returns:
            The account.

        Raises:
            MetaApiError: If the request fails after retries.
        """
        payload = self._get(account_remote_id, {"fields": ",".join(_ACCOUNT_FIELDS)})
        return AdAccountPayload.model_validate(payload)

    # -- campaign structure ------------------------------------------------

    def list_campaigns(self, account_remote_id: str) -> list[CampaignPayload]:
        """List an account's campaigns.

        Args:
            account_remote_id: Meta account ID, including the ``act_`` prefix.

        Returns:
            Every campaign in the account, including paused and archived ones.

        Raises:
            MetaApiError: If the request fails after retries.
        """
        parameters = {"fields": ",".join(_CAMPAIGN_FIELDS)}
        return [
            CampaignPayload.model_validate(item)
            for item in self._iterate_edge(f"{account_remote_id}/campaigns", parameters)
        ]

    def list_ad_sets(self, account_remote_id: str) -> list[AdSetPayload]:
        """List an account's ad sets.

        Read at account level rather than per campaign: one paged request
        returns every ad set, where per-campaign requests would issue one call
        per campaign and exhaust the rate limit budget on a large account.

        Args:
            account_remote_id: Meta account ID, including the ``act_`` prefix.

        Returns:
            Every ad set in the account.

        Raises:
            MetaApiError: If the request fails after retries.
        """
        parameters = {"fields": ",".join(_AD_SET_FIELDS)}
        return [
            AdSetPayload.model_validate(item)
            for item in self._iterate_edge(f"{account_remote_id}/adsets", parameters)
        ]

    def list_ads(self, account_remote_id: str) -> list[AdPayload]:
        """List an account's ads.

        Args:
            account_remote_id: Meta account ID, including the ``act_`` prefix.

        Returns:
            Every ad in the account.

        Raises:
            MetaApiError: If the request fails after retries.
        """
        parameters = {"fields": ",".join(_AD_FIELDS)}
        return [
            AdPayload.model_validate(item)
            for item in self._iterate_edge(f"{account_remote_id}/ads", parameters)
        ]

    def list_ad_creatives(self, account_remote_id: str) -> list[AdCreativePayload]:
        """List an account's creative library.

        Args:
            account_remote_id: Meta account ID, including the ``act_`` prefix.

        Returns:
            Every creative in the account.

        Raises:
            MetaApiError: If the request fails after retries.
        """
        parameters = {"fields": ",".join(_AD_CREATIVE_FIELDS)}
        return [
            AdCreativePayload.model_validate(item)
            for item in self._iterate_edge(f"{account_remote_id}/adcreatives", parameters)
        ]

    # -- insights ----------------------------------------------------------

    def fetch_insights(
        self,
        *,
        entity_remote_id: str,
        level: str,
        since: date,
        until: date,
        daily_breakdown: bool = True,
    ) -> list[InsightsPayload]:
        """Fetch performance insights for an entity over a date range.

        Args:
            entity_remote_id: Meta ID of the account, campaign, ad set, or ad to
                report on.
            level: Aggregation level; one of ``account``, ``campaign``,
                ``adset``, ``ad``.
            since: First day of the range, inclusive, in the account timezone.
            until: Last day of the range, inclusive.
            daily_breakdown: Return one row per day rather than a single
                aggregated row. Daily rows are what make trend comparison
                possible, so this defaults to on.

        Returns:
            One row per entity per day, or one row per entity when
            ``daily_breakdown`` is off.

        Raises:
            MetaApiError: If the request fails after retries.
            ValueError: If ``level`` is not a level Meta accepts.
        """
        level_fields = _INSIGHTS_FIELDS_BY_LEVEL.get(level)
        if level_fields is None:
            message = (
                f"Unsupported insights level {level!r}; "
                f"expected one of {sorted(_INSIGHTS_FIELDS_BY_LEVEL)}"
            )
            raise ValueError(message)

        parameters: dict[str, str] = {
            "level": level,
            "fields": ",".join((*_INSIGHTS_BASE_FIELDS, *level_fields)),
            # Meta expects this as a JSON object, not as two separate parameters.
            "time_range": json.dumps({"since": since.isoformat(), "until": until.isoformat()}),
        }
        if daily_breakdown:
            parameters["time_increment"] = "1"

        return [
            InsightsPayload.model_validate(item)
            for item in self._iterate_edge(f"{entity_remote_id}/insights", parameters)
        ]

    # -- mutations ---------------------------------------------------------

    def update_daily_budget(self, entity_remote_id: str, *, daily_budget_minor: int) -> None:
        """Set the daily budget of a campaign or ad set.

        Args:
            entity_remote_id: Meta ID of the campaign or ad set.
            daily_budget_minor: New budget in the account currency's minor unit.
                Meta rejects non-integer budgets, which is why the conversion
                from major units happens before this call.

        Raises:
            MetaApiError: If the update fails.
        """
        self._post(entity_remote_id, {"daily_budget": str(daily_budget_minor)})
        _logger.info(
            "Updated daily budget",
            extra={"entity_remote_id": entity_remote_id, "daily_budget_minor": daily_budget_minor},
        )

    def pause_entity(self, entity_remote_id: str) -> None:
        """Pause a campaign, ad set, or ad.

        Args:
            entity_remote_id: Meta ID of the entity to pause.

        Raises:
            MetaApiError: If the update fails.
        """
        self._post(entity_remote_id, {"status": META_STATUS_PAUSED})
        _logger.info("Paused entity", extra={"entity_remote_id": entity_remote_id})

    def resume_entity(self, entity_remote_id: str) -> None:
        """Reactivate a paused campaign, ad set, or ad.

        Args:
            entity_remote_id: Meta ID of the entity to reactivate.

        Raises:
            MetaApiError: If the update fails.
        """
        self._post(entity_remote_id, {"status": META_STATUS_ACTIVE})
        _logger.info("Resumed entity", extra={"entity_remote_id": entity_remote_id})

    # -- transport ---------------------------------------------------------

    def _get(self, path: str, parameters: Mapping[str, str]) -> dict[str, Any]:
        """Issue a GET against a Graph API path."""
        return self._request("GET", self._build_url(path), parameters=parameters)

    def _post(self, path: str, form_fields: Mapping[str, str]) -> dict[str, Any]:
        """Issue a POST against a Graph API path.

        Meta's write endpoints take form-encoded fields, not a JSON body.
        """
        return self._request("POST", self._build_url(path), form_fields=form_fields)

    def _iterate_edge(
        self,
        path: str,
        parameters: Mapping[str, str],
    ) -> Iterator[dict[str, Any]]:
        """Yield every object from a paged edge, following ``after`` cursors.

        Args:
            path: Graph API path of the edge, without the version prefix.
            parameters: Query parameters shared by every page.

        Yields:
            Each object in the edge, in the order Meta returns them.

        Raises:
            MetaApiResponseError: If a page's ``data`` is not a list, or if
                pagination exceeds the page ceiling, which indicates a cursor
                that never advances.
        """
        url = self._build_url(path)
        page_parameters: dict[str, str] = {**parameters, "limit": str(_DEFAULT_PAGE_SIZE)}

        for page_number in range(1, _MAX_PAGES + 1):
            payload = self._request("GET", url, parameters=page_parameters)
            data = payload.get("data", [])
            if not isinstance(data, list):
                raise MetaApiResponseError(
                    "Graph API edge returned a non-list 'data' field",
                    context={"path": path, "page": page_number},
                )

            for item in data:
                if isinstance(item, dict):
                    yield item

            paging = payload.get("paging")
            if not isinstance(paging, dict) or "next" not in paging:
                return

            cursors = paging.get("cursors")
            after_cursor = cursors.get("after") if isinstance(cursors, dict) else None
            if not isinstance(after_cursor, str) or not after_cursor:
                return
            page_parameters = {**page_parameters, "after": after_cursor}

        raise MetaApiResponseError(
            "Pagination exceeded the maximum page count; the cursor is not advancing",
            context={"path": path, "max_pages": _MAX_PAGES},
        )

    def _build_url(self, path: str) -> str:
        """Join a Graph API path onto the configured versioned root."""
        return f"{self._settings.graph_url}/{path.lstrip('/')}"

    def _request(
        self,
        method: str,
        url: str,
        *,
        parameters: Mapping[str, str] | None = None,
        form_fields: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Send one request, retrying transient failures.

        Args:
            method: HTTP method.
            url: Fully qualified request URL.
            parameters: Query parameters, before the app secret proof is added.
            form_fields: Form-encoded body fields, for writes.

        Returns:
            The decoded JSON object.

        Raises:
            MetaApiAuthenticationError: If the token is rejected.
            MetaApiPermissionError: If the token lacks the required permission.
            MetaApiRateLimitError: If throttled and retries are exhausted.
            MetaApiTransportError: If no response arrived and retries are
                exhausted.
            MetaApiError: For any other API-reported failure.
            MetaApiResponseError: If a successful response is not a JSON object.
        """
        query_parameters = {**(parameters or {}), **self._credentials.proof_parameters()}
        headers = self._credentials.authorization_headers()
        total_attempts = self._settings.max_retries + 1

        for attempt in range(1, total_attempts + 1):
            is_final_attempt = attempt == total_attempts
            try:
                response = self._http_session.request(
                    method,
                    url,
                    params=query_parameters,
                    data=dict(form_fields) if form_fields else None,
                    headers=headers,
                    timeout=self._settings.request_timeout_seconds,
                )
            except requests.RequestException as exc:
                transport_error = MetaApiTransportError(
                    "Request to the Meta Marketing API could not be completed",
                    context={"url": url, "method": method, "attempt": attempt, "reason": str(exc)},
                )
                if is_final_attempt:
                    _logger.error("Meta API transport failure, retries exhausted", exc_info=True)
                    raise transport_error from exc
                self._wait_before_retry(attempt, reason="transport error")
                continue

            self._observe_rate_limit(response)

            if response.ok:
                return self._decode_json_object(response, url)

            error = self._build_api_error(response, url=url, method=method)
            if is_final_attempt or not _is_retryable(error, response.status_code):
                _logger.error(
                    "Meta API request failed",
                    extra={
                        "url": url,
                        "method": method,
                        "status_code": response.status_code,
                        "error_code": error.error_code,
                        "fbtrace_id": error.fbtrace_id,
                    },
                )
                raise error
            self._wait_before_retry(attempt, reason=f"HTTP {response.status_code}", error=error)

        raise MetaApiTransportError(
            "Retry loop completed without producing a response",
            context={"url": url, "method": method, "attempts": total_attempts},
        )

    def _wait_before_retry(
        self,
        attempt: int,
        *,
        reason: str,
        error: MetaApiError | None = None,
    ) -> None:
        """Sleep before the next attempt, using exponential backoff.

        Throttling is treated differently from other transient failures: Meta's
        penalty window is measured in minutes, so backing off for a second
        merely burns an attempt. The configured rate limit pause is used
        instead, or Meta's own estimate when it supplies one.
        """
        if isinstance(error, MetaApiRateLimitError):
            delay = error.retry_after_seconds or self._settings.rate_limit_pause_seconds
        else:
            delay = self._settings.retry_backoff_seconds * (_RETRY_BACKOFF_BASE ** (attempt - 1))

        _logger.warning(
            "Retrying Meta API request after %s",
            reason,
            extra={"attempt": attempt, "delay_seconds": delay, "reason": reason},
        )
        self._sleeper(delay)

    def _observe_rate_limit(self, response: requests.Response) -> None:
        """Pause proactively when Meta reports the budget is nearly spent."""
        usage = parse_rate_limit_headers(response.headers)
        if usage is None or not usage.should_throttle():
            return

        delay = usage.estimated_regain_seconds or self._settings.rate_limit_pause_seconds
        _logger.warning(
            "Approaching Meta rate limit; pausing before further requests",
            extra={"usage_percent": usage.worst_percent, "delay_seconds": delay},
        )
        self._sleeper(delay)

    def _decode_json_object(self, response: requests.Response, url: str) -> dict[str, Any]:
        """Decode a successful response into a JSON object."""
        try:
            decoded = response.json()
        except ValueError as exc:
            raise MetaApiResponseError(
                "Meta returned a successful response that is not valid JSON",
                status_code=response.status_code,
                context={"url": url},
            ) from exc

        if not isinstance(decoded, dict):
            raise MetaApiResponseError(
                "Meta returned a JSON value that is not an object",
                status_code=response.status_code,
                context={"url": url, "type": type(decoded).__name__},
            )
        return decoded

    def _build_api_error(
        self, response: requests.Response, *, url: str, method: str
    ) -> MetaApiError:
        """Translate an error response into the most specific exception type."""
        error_body = _extract_error_body(response)
        message = str(
            error_body.get("message") or f"Meta API request failed ({response.status_code})"
        )
        error_code = _coerce_optional_int(error_body.get("code"))
        error_subcode = _coerce_optional_int(error_body.get("error_subcode"))
        fbtrace_id = error_body.get("fbtrace_id")
        context = {"url": url, "method": method}

        shared: dict[str, Any] = {
            "status_code": response.status_code,
            "error_code": error_code,
            "error_subcode": error_subcode,
            "fbtrace_id": str(fbtrace_id) if fbtrace_id is not None else None,
            "context": context,
        }

        if response.status_code == _HTTP_TOO_MANY_REQUESTS or (
            error_code is not None and error_code in _RATE_LIMIT_ERROR_CODES
        ):
            usage = parse_rate_limit_headers(response.headers)
            return MetaApiRateLimitError(
                message,
                retry_after_seconds=usage.estimated_regain_seconds if usage else None,
                **shared,
            )
        if response.status_code == _HTTP_UNAUTHORIZED or (
            error_code is not None and error_code in _AUTHENTICATION_ERROR_CODES
        ):
            return MetaApiAuthenticationError(message, **shared)
        if response.status_code == _HTTP_FORBIDDEN or (
            error_code is not None and error_code in _PERMISSION_ERROR_CODES
        ):
            return MetaApiPermissionError(message, **shared)
        return MetaApiError(message, **shared)


def _is_retryable(error: MetaApiError, status_code: int) -> bool:
    """Whether a failed request is worth attempting again.

    Rate limits and server errors are transient. Authentication and permission
    failures are not: the same token will be rejected the same way every time,
    and retrying delays the operator's error while spending rate limit budget.
    """
    if isinstance(error, MetaApiAuthenticationError | MetaApiPermissionError):
        return False
    if isinstance(error, MetaApiRateLimitError):
        return True
    return status_code >= _HTTP_SERVER_ERROR_FLOOR


def _extract_error_body(response: requests.Response) -> dict[str, Any]:
    """Pull Meta's ``error`` object out of a failure response.

    Meta occasionally answers with HTML — a gateway error page, for instance —
    so a body that will not decode yields an empty mapping rather than raising
    and masking the status code that actually explains the failure.
    """
    try:
        decoded = response.json()
    except ValueError:
        return {}
    if not isinstance(decoded, dict):
        return {}
    error_body = decoded.get("error")
    return error_body if isinstance(error_body, dict) else {}


def _coerce_optional_int(value: object) -> int | None:
    """Coerce a Meta error field to ``int``, tolerating strings and absence."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None
