"""Tests for the Meta Marketing API client, against mocked HTTP.

Every test here intercepts requests rather than issuing them. The client's sleep
function is replaced by a recorder, so the full retry and backoff path is
exercised in microseconds and the delays it *would* have taken are asserted on
directly.
"""

from __future__ import annotations

import json
from datetime import date
from urllib.parse import parse_qs, urlparse

import pytest
import requests
import responses

from app.api.client import MetaMarketingClient
from app.api.rate_limit import APP_USAGE_HEADER
from app.utils.exceptions import (
    MetaApiAuthenticationError,
    MetaApiError,
    MetaApiPermissionError,
    MetaApiRateLimitError,
    MetaApiResponseError,
    MetaApiTransportError,
)
from tests.conftest import TEST_ACCOUNT_ID, graph_url


def _query_of(call_index: int, mock: responses.RequestsMock) -> dict[str, list[str]]:
    """Return the parsed query string of a recorded request."""
    url = mock.calls[call_index].request.url or ""
    return parse_qs(urlparse(url).query)


def _form_body_of(call_index: int, mock: responses.RequestsMock) -> dict[str, list[str]]:
    """Return the parsed form-encoded body of a recorded request.

    `requests` types a request body as a union covering streams and iterables;
    every write in this client sends form fields, so it is narrowed here rather
    than at each of the three call sites.
    """
    body = mock.calls[call_index].request.body or ""
    if isinstance(body, bytes):
        body = body.decode()
    return parse_qs(str(body))


# ---------------------------------------------------------------------------
# Authentication and request shape
# ---------------------------------------------------------------------------


def test_requests_carry_a_bearer_token_and_app_secret_proof(
    client: MetaMarketingClient,
    mocked_responses: responses.RequestsMock,
) -> None:
    mocked_responses.add(
        responses.GET,
        graph_url(f"{TEST_ACCOUNT_ID}/campaigns"),
        json={"data": []},
        status=200,
    )

    client.list_campaigns(TEST_ACCOUNT_ID)

    request = mocked_responses.calls[0].request
    assert request.headers["Authorization"] == "Bearer test-access-token"
    assert "appsecret_proof" in _query_of(0, mocked_responses)
    # The token must never appear in the URL, where logs would capture it.
    assert "test-access-token" not in (request.url or "")


def test_requested_fields_are_explicit(
    client: MetaMarketingClient,
    mocked_responses: responses.RequestsMock,
) -> None:
    mocked_responses.add(
        responses.GET,
        graph_url(f"{TEST_ACCOUNT_ID}/campaigns"),
        json={"data": []},
        status=200,
    )

    client.list_campaigns(TEST_ACCOUNT_ID)

    fields = _query_of(0, mocked_responses)["fields"][0].split(",")
    assert "daily_budget" in fields
    assert "effective_status" in fields


def test_successful_response_is_validated_into_payloads(
    client: MetaMarketingClient,
    mocked_responses: responses.RequestsMock,
) -> None:
    mocked_responses.add(
        responses.GET,
        graph_url(f"{TEST_ACCOUNT_ID}/campaigns"),
        json={
            "data": [
                {"id": "c1", "name": "Spring Sale", "status": "ACTIVE", "daily_budget": "5000"}
            ]
        },
        status=200,
    )

    campaigns = client.list_campaigns(TEST_ACCOUNT_ID)

    assert len(campaigns) == 1
    assert campaigns[0].remote_id == "c1"
    assert campaigns[0].daily_budget_minor == 5000


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


def test_pagination_follows_the_after_cursor(
    client: MetaMarketingClient,
    mocked_responses: responses.RequestsMock,
) -> None:
    url = graph_url(f"{TEST_ACCOUNT_ID}/campaigns")
    mocked_responses.add(
        responses.GET,
        url,
        json={
            "data": [{"id": "c1"}],
            "paging": {"cursors": {"after": "CURSOR_ONE"}, "next": f"{url}?after=CURSOR_ONE"},
        },
        status=200,
    )
    mocked_responses.add(
        responses.GET,
        url,
        json={"data": [{"id": "c2"}], "paging": {"cursors": {"before": "X"}}},
        status=200,
    )

    campaigns = client.list_campaigns(TEST_ACCOUNT_ID)

    assert [campaign.remote_id for campaign in campaigns] == ["c1", "c2"]
    # The second request must carry our own parameter set plus the cursor,
    # rather than replaying Meta's `next` URL and duplicating appsecret_proof.
    second_query = _query_of(1, mocked_responses)
    assert second_query["after"] == ["CURSOR_ONE"]
    assert len(second_query["appsecret_proof"]) == 1


def test_pagination_stops_when_no_next_link_is_present(
    client: MetaMarketingClient,
    mocked_responses: responses.RequestsMock,
) -> None:
    mocked_responses.add(
        responses.GET,
        graph_url(f"{TEST_ACCOUNT_ID}/campaigns"),
        json={"data": [{"id": "c1"}], "paging": {"cursors": {"after": "CURSOR"}}},
        status=200,
    )

    campaigns = client.list_campaigns(TEST_ACCOUNT_ID)

    assert len(campaigns) == 1
    assert len(mocked_responses.calls) == 1


def test_non_list_data_is_rejected(
    client: MetaMarketingClient,
    mocked_responses: responses.RequestsMock,
) -> None:
    mocked_responses.add(
        responses.GET,
        graph_url(f"{TEST_ACCOUNT_ID}/campaigns"),
        json={"data": {"unexpected": "shape"}},
        status=200,
    )

    with pytest.raises(MetaApiResponseError):
        client.list_campaigns(TEST_ACCOUNT_ID)


# ---------------------------------------------------------------------------
# Retries
# ---------------------------------------------------------------------------


def test_server_errors_are_retried_then_succeed(
    client: MetaMarketingClient,
    mocked_responses: responses.RequestsMock,
    recorded_sleeps: list[float],
) -> None:
    url = graph_url(f"{TEST_ACCOUNT_ID}/campaigns")
    mocked_responses.add(responses.GET, url, json={"error": {"message": "oops"}}, status=500)
    mocked_responses.add(responses.GET, url, json={"data": [{"id": "c1"}]}, status=200)

    campaigns = client.list_campaigns(TEST_ACCOUNT_ID)

    assert len(campaigns) == 1
    assert len(recorded_sleeps) == 1


def test_backoff_grows_exponentially(
    client: MetaMarketingClient,
    mocked_responses: responses.RequestsMock,
    recorded_sleeps: list[float],
) -> None:
    url = graph_url(f"{TEST_ACCOUNT_ID}/campaigns")
    for _ in range(3):
        mocked_responses.add(responses.GET, url, json={"error": {"message": "down"}}, status=503)

    with pytest.raises(MetaApiError):
        client.list_campaigns(TEST_ACCOUNT_ID)

    # max_retries=2 means three attempts and two waits, each double the last.
    assert len(recorded_sleeps) == 2
    assert recorded_sleeps[1] == pytest.approx(recorded_sleeps[0] * 2)


def test_connection_failures_are_retried_and_finally_raise_transport_error(
    client: MetaMarketingClient,
    mocked_responses: responses.RequestsMock,
    recorded_sleeps: list[float],
) -> None:
    url = graph_url(f"{TEST_ACCOUNT_ID}/campaigns")
    for _ in range(3):
        mocked_responses.add(responses.GET, url, body=requests.ConnectionError("no route"))

    with pytest.raises(MetaApiTransportError):
        client.list_campaigns(TEST_ACCOUNT_ID)

    assert len(recorded_sleeps) == 2


def test_authentication_failures_are_not_retried(
    client: MetaMarketingClient,
    mocked_responses: responses.RequestsMock,
    recorded_sleeps: list[float],
) -> None:
    mocked_responses.add(
        responses.GET,
        graph_url(f"{TEST_ACCOUNT_ID}/campaigns"),
        json={
            "error": {
                "message": "Error validating access token",
                "code": 190,
                "fbtrace_id": "AbCdEf",
            }
        },
        status=401,
    )

    with pytest.raises(MetaApiAuthenticationError) as failure:
        client.list_campaigns(TEST_ACCOUNT_ID)

    # Retrying an expired token only delays the message and spends rate budget.
    assert len(mocked_responses.calls) == 1
    assert recorded_sleeps == []
    assert failure.value.fbtrace_id == "AbCdEf"
    assert failure.value.error_code == 190


def test_permission_failures_are_not_retried(
    client: MetaMarketingClient,
    mocked_responses: responses.RequestsMock,
) -> None:
    mocked_responses.add(
        responses.GET,
        graph_url(f"{TEST_ACCOUNT_ID}/campaigns"),
        json={"error": {"message": "Requires ads_management", "code": 200}},
        status=403,
    )

    with pytest.raises(MetaApiPermissionError):
        client.list_campaigns(TEST_ACCOUNT_ID)

    assert len(mocked_responses.calls) == 1


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


def test_throttling_is_retried_using_the_rate_limit_pause(
    client: MetaMarketingClient,
    mocked_responses: responses.RequestsMock,
    recorded_sleeps: list[float],
    meta_settings: object,
) -> None:
    url = graph_url(f"{TEST_ACCOUNT_ID}/campaigns")
    for _ in range(3):
        mocked_responses.add(
            responses.GET,
            url,
            json={"error": {"message": "User request limit reached", "code": 17}},
            status=429,
        )

    with pytest.raises(MetaApiRateLimitError):
        client.list_campaigns(TEST_ACCOUNT_ID)

    # Meta's penalty window is minutes, so throttling uses the configured pause
    # rather than the short exponential backoff used for server errors.
    assert len(recorded_sleeps) == 2
    assert all(delay == pytest.approx(0.001) for delay in recorded_sleeps)


def test_rate_limit_error_codes_are_recognised_without_a_429(
    client: MetaMarketingClient,
    mocked_responses: responses.RequestsMock,
) -> None:
    url = graph_url(f"{TEST_ACCOUNT_ID}/campaigns")
    for _ in range(3):
        mocked_responses.add(
            responses.GET,
            url,
            json={"error": {"message": "Application request limit reached", "code": 4}},
            status=400,
        )

    with pytest.raises(MetaApiRateLimitError):
        client.list_campaigns(TEST_ACCOUNT_ID)


def test_nearing_the_budget_triggers_a_proactive_pause(
    client: MetaMarketingClient,
    mocked_responses: responses.RequestsMock,
    recorded_sleeps: list[float],
) -> None:
    mocked_responses.add(
        responses.GET,
        graph_url(f"{TEST_ACCOUNT_ID}/campaigns"),
        json={"data": []},
        status=200,
        headers={APP_USAGE_HEADER: json.dumps({"call_count": 95})},
    )

    client.list_campaigns(TEST_ACCOUNT_ID)

    # The request succeeded, but the client slows itself before being blocked.
    assert len(recorded_sleeps) == 1


def test_usage_below_the_threshold_does_not_pause(
    client: MetaMarketingClient,
    mocked_responses: responses.RequestsMock,
    recorded_sleeps: list[float],
) -> None:
    mocked_responses.add(
        responses.GET,
        graph_url(f"{TEST_ACCOUNT_ID}/campaigns"),
        json={"data": []},
        status=200,
        headers={APP_USAGE_HEADER: json.dumps({"call_count": 10})},
    )

    client.list_campaigns(TEST_ACCOUNT_ID)

    assert recorded_sleeps == []


# ---------------------------------------------------------------------------
# Malformed responses
# ---------------------------------------------------------------------------


def test_non_json_success_is_reported_as_a_response_error(
    client: MetaMarketingClient,
    mocked_responses: responses.RequestsMock,
) -> None:
    mocked_responses.add(
        responses.GET,
        graph_url(f"{TEST_ACCOUNT_ID}/campaigns"),
        body="<html>gateway</html>",
        status=200,
        content_type="text/html",
    )

    with pytest.raises(MetaApiResponseError):
        client.list_campaigns(TEST_ACCOUNT_ID)


def test_html_error_body_still_produces_a_typed_failure(
    client: MetaMarketingClient,
    mocked_responses: responses.RequestsMock,
) -> None:
    mocked_responses.add(
        responses.GET,
        graph_url(f"{TEST_ACCOUNT_ID}/campaigns"),
        body="<html>Forbidden</html>",
        status=403,
        content_type="text/html",
    )

    # An undecodable body must not mask the status code that explains it.
    with pytest.raises(MetaApiPermissionError):
        client.list_campaigns(TEST_ACCOUNT_ID)


# ---------------------------------------------------------------------------
# Insights
# ---------------------------------------------------------------------------


def test_insights_request_carries_level_time_range_and_daily_increment(
    client: MetaMarketingClient,
    mocked_responses: responses.RequestsMock,
) -> None:
    mocked_responses.add(
        responses.GET,
        graph_url(f"{TEST_ACCOUNT_ID}/insights"),
        json={"data": []},
        status=200,
    )

    client.fetch_insights(
        entity_remote_id=TEST_ACCOUNT_ID,
        level="campaign",
        since=date(2026, 6, 1),
        until=date(2026, 6, 7),
    )

    query = _query_of(0, mocked_responses)
    assert query["level"] == ["campaign"]
    assert query["time_increment"] == ["1"]
    # Meta expects a JSON object here, not two separate parameters.
    assert json.loads(query["time_range"][0]) == {"since": "2026-06-01", "until": "2026-06-07"}
    assert "campaign_id" in query["fields"][0]


def test_insights_fields_are_chosen_per_level(
    client: MetaMarketingClient,
    mocked_responses: responses.RequestsMock,
) -> None:
    mocked_responses.add(
        responses.GET,
        graph_url(f"{TEST_ACCOUNT_ID}/insights"),
        json={"data": []},
        status=200,
    )

    client.fetch_insights(
        entity_remote_id=TEST_ACCOUNT_ID,
        level="account",
        since=date(2026, 6, 1),
        until=date(2026, 6, 7),
    )

    fields = _query_of(0, mocked_responses)["fields"][0]
    # Requesting a field the level cannot produce is rejected by Meta.
    assert "campaign_id" not in fields
    assert "account_id" in fields


def test_unknown_insights_level_is_rejected_before_any_request(
    client: MetaMarketingClient,
) -> None:
    with pytest.raises(ValueError, match="Unsupported insights level"):
        client.fetch_insights(
            entity_remote_id=TEST_ACCOUNT_ID,
            level="galaxy",
            since=date(2026, 6, 1),
            until=date(2026, 6, 7),
        )


def test_insights_rows_are_parsed(
    client: MetaMarketingClient,
    mocked_responses: responses.RequestsMock,
) -> None:
    mocked_responses.add(
        responses.GET,
        graph_url(f"{TEST_ACCOUNT_ID}/insights"),
        json={
            "data": [
                {
                    "date_start": "2026-06-01",
                    "date_stop": "2026-06-01",
                    "campaign_id": "c1",
                    "campaign_name": "Spring",
                    "spend": "42.50",
                    "impressions": "1000",
                    "clicks": "25",
                    "actions": [{"action_type": "purchase", "value": "3"}],
                }
            ]
        },
        status=200,
    )

    rows = client.fetch_insights(
        entity_remote_id=TEST_ACCOUNT_ID,
        level="campaign",
        since=date(2026, 6, 1),
        until=date(2026, 6, 1),
    )

    assert rows[0].campaign_remote_id == "c1"
    assert rows[0].conversion_count() == 3


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------


def test_updating_a_budget_posts_form_fields(
    client: MetaMarketingClient,
    mocked_responses: responses.RequestsMock,
) -> None:
    mocked_responses.add(
        responses.POST,
        graph_url("c1"),
        json={"success": True},
        status=200,
    )

    client.update_daily_budget("c1", daily_budget_minor=12_500)

    body = _form_body_of(0, mocked_responses)
    assert body["daily_budget"] == ["12500"]


def test_pausing_posts_the_paused_status(
    client: MetaMarketingClient,
    mocked_responses: responses.RequestsMock,
) -> None:
    mocked_responses.add(responses.POST, graph_url("c1"), json={"success": True}, status=200)

    client.pause_entity("c1")

    body = _form_body_of(0, mocked_responses)
    assert body["status"] == ["PAUSED"]


def test_resuming_posts_the_active_status(
    client: MetaMarketingClient,
    mocked_responses: responses.RequestsMock,
) -> None:
    mocked_responses.add(responses.POST, graph_url("c1"), json={"success": True}, status=200)

    client.resume_entity("c1")

    body = _form_body_of(0, mocked_responses)
    assert body["status"] == ["ACTIVE"]


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def test_client_does_not_close_a_session_it_was_given(
    meta_settings: object,
    credentials: object,
) -> None:
    http_session = requests.Session()
    api_client = MetaMarketingClient(
        settings=meta_settings,  # type: ignore[arg-type]
        credentials=credentials,  # type: ignore[arg-type]
        http_session=http_session,
    )

    api_client.close()

    # The caller may still be using a session it owns.
    assert http_session.adapters != {}
    http_session.close()
