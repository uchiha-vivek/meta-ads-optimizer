"""Tests for interpreting Meta's rate limit headers."""

from __future__ import annotations

import json

from app.api.rate_limit import (
    APP_USAGE_HEADER,
    BUSINESS_USE_CASE_USAGE_HEADER,
    RateLimitUsage,
    parse_rate_limit_headers,
)


def test_absent_headers_yield_no_usage() -> None:
    # Meta omits these freely; absence is normal and must not be an error.
    assert parse_rate_limit_headers({}) is None


def test_app_usage_header_is_parsed() -> None:
    headers = {
        APP_USAGE_HEADER: json.dumps({"call_count": 25, "total_cputime": 10, "total_time": 30})
    }

    usage = parse_rate_limit_headers(headers)

    assert usage is not None
    assert usage.call_count_percent == 25
    assert usage.total_time_percent == 30
    assert usage.worst_percent == 30


def test_business_use_case_header_is_parsed_from_its_nested_shape() -> None:
    headers = {
        BUSINESS_USE_CASE_USAGE_HEADER: json.dumps(
            {
                "1234567890": [
                    {
                        "type": "ads_management",
                        "call_count": 95,
                        "total_cputime": 20,
                        "total_time": 40,
                        "estimated_time_to_regain_access": 5,
                    }
                ]
            }
        )
    }

    usage = parse_rate_limit_headers(headers)

    assert usage is not None
    assert usage.call_count_percent == 95
    # Meta reports minutes; the client works in seconds.
    assert usage.estimated_regain_seconds == 300


def test_the_most_constrained_budget_wins() -> None:
    headers = {
        APP_USAGE_HEADER: json.dumps({"call_count": 10}),
        BUSINESS_USE_CASE_USAGE_HEADER: json.dumps({"acct": [{"call_count": 99}]}),
    }

    usage = parse_rate_limit_headers(headers)

    assert usage is not None
    # Being under the app-wide budget is no help when the account budget is spent.
    assert usage.worst_percent == 99


def test_headers_are_matched_case_insensitively() -> None:
    headers = {"x-app-usage": json.dumps({"call_count": 42})}

    usage = parse_rate_limit_headers(headers)

    assert usage is not None
    assert usage.call_count_percent == 42


def test_malformed_json_is_ignored_rather_than_raised() -> None:
    # A header we cannot parse is no reason to fail a request Meta answered.
    assert parse_rate_limit_headers({APP_USAGE_HEADER: "not json at all"}) is None


def test_string_valued_fields_are_coerced() -> None:
    headers = {APP_USAGE_HEADER: json.dumps({"call_count": "75", "total_time": "12.5"})}

    usage = parse_rate_limit_headers(headers)

    assert usage is not None
    assert usage.call_count_percent == 75
    assert usage.total_time_percent == 12


def test_should_throttle_respects_the_threshold() -> None:
    usage = RateLimitUsage(
        call_count_percent=91,
        total_cputime_percent=0,
        total_time_percent=0,
        estimated_regain_seconds=0,
    )

    assert usage.should_throttle() is True
    assert usage.should_throttle(threshold_percent=95) is False
    assert usage.is_exhausted is False


def test_exhaustion_is_detected_at_one_hundred_percent() -> None:
    usage = RateLimitUsage(
        call_count_percent=100,
        total_cputime_percent=0,
        total_time_percent=0,
        estimated_regain_seconds=60,
    )

    assert usage.is_exhausted is True
