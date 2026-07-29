"""Tests for CLI option parsing, error translation, and rendering."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
import typer
from rich.console import Console
from typer.testing import CliRunner

from app.analytics.aggregation import EntityWindowMetrics
from app.analytics.metrics import PerformanceMetrics
from app.cli.context import ApplicationContext, application_context
from app.cli.error_handling import (
    EXIT_AUTHENTICATION_ERROR,
    EXIT_CONFIGURATION_ERROR,
    EXIT_DATABASE_ERROR,
    EXIT_GENERAL_ERROR,
    EXIT_PERMISSION_ERROR,
    EXIT_RATE_LIMITED,
    handle_domain_errors,
    profile_for,
)
from app.cli.main import app
from app.cli.options import (
    REPORTING_LAG_DAYS,
    parse_level,
    parse_statuses,
    resolve_account_id,
    resolve_window,
)
from app.cli.rendering import (
    render_accounts,
    render_campaigns,
    render_creatives,
    render_performance,
    render_recommendations,
)
from app.models.enums import EntityStatus, InsightLevel
from app.services.creative_service import CreativeUsage
from app.services.insight_service import PerformanceReport
from app.utils.exceptions import (
    ConfigurationError,
    EntityNotFoundError,
    MetaApiAuthenticationError,
    MetaApiError,
    MetaApiPermissionError,
    MetaApiRateLimitError,
    MetaOptimizerError,
    RepositoryError,
)
from tests.conftest import build_account, build_campaign, build_creative

# ---------------------------------------------------------------------------
# Option parsing
# ---------------------------------------------------------------------------


def test_window_ends_yesterday_because_today_is_still_moving(today: date) -> None:
    since, until = resolve_window(7, today=today)

    # Meta restates the current day as attribution windows close, so a figure
    # read today would change tomorrow.
    assert until == date(2026, 6, 14)
    assert REPORTING_LAG_DAYS == 1
    assert since == date(2026, 6, 8)
    assert (until - since).days == 6


def test_single_day_window_is_one_day_long(today: date) -> None:
    since, until = resolve_window(1, today=today)

    assert since == until == date(2026, 6, 14)


@pytest.mark.parametrize("days", [0, -1])
def test_non_positive_windows_are_rejected(days: int, today: date) -> None:
    with pytest.raises(typer.BadParameter):
        resolve_window(days, today=today)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("campaign", InsightLevel.CAMPAIGN),
        ("ADSET", InsightLevel.ADSET),
        ("  ad  ", InsightLevel.AD),
        ("account", InsightLevel.ACCOUNT),
    ],
)
def test_levels_are_parsed_case_insensitively(raw: str, expected: InsightLevel) -> None:
    assert parse_level(raw) is expected


def test_unknown_level_lists_the_valid_choices() -> None:
    with pytest.raises(typer.BadParameter) as failure:
        parse_level("galaxy")

    assert "campaign" in str(failure.value)


def test_statuses_are_parsed_from_a_comma_separated_list() -> None:
    assert parse_statuses("active,paused") == [EntityStatus.ACTIVE, EntityStatus.PAUSED]
    assert parse_statuses(" ACTIVE , archived ") == [
        EntityStatus.ACTIVE,
        EntityStatus.ARCHIVED,
    ]


@pytest.mark.parametrize("empty", [None, "", "   ", ","])
def test_absent_status_filter_means_no_filter(empty: str | None) -> None:
    assert parse_statuses(empty) is None


def test_unknown_status_lists_the_valid_choices() -> None:
    with pytest.raises(typer.BadParameter) as failure:
        parse_statuses("active,exploded")

    assert "paused" in str(failure.value)


def test_account_id_falls_back_to_configuration() -> None:
    context = MagicMock(spec=ApplicationContext)
    context.settings.meta.ad_account_id = "act_from_env"

    assert resolve_account_id(context, None) == "act_from_env"
    assert resolve_account_id(context, "act_explicit") == "act_explicit"


# ---------------------------------------------------------------------------
# Error translation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "expected_exit_code"),
    [
        (ConfigurationError("bad config"), EXIT_CONFIGURATION_ERROR),
        (MetaApiAuthenticationError("expired"), EXIT_AUTHENTICATION_ERROR),
        (MetaApiPermissionError("forbidden"), EXIT_PERMISSION_ERROR),
        (MetaApiRateLimitError("throttled"), EXIT_RATE_LIMITED),
        (MetaApiError("generic"), EXIT_GENERAL_ERROR),
        (RepositoryError("db down"), EXIT_DATABASE_ERROR),
        (EntityNotFoundError("missing"), EXIT_DATABASE_ERROR),
        (MetaOptimizerError("something else"), EXIT_GENERAL_ERROR),
    ],
)
def test_each_failure_class_has_its_own_exit_code(
    error: MetaOptimizerError,
    expected_exit_code: int,
) -> None:
    # A scheduler must be able to tell "retry in ten minutes" from "page someone".
    assert profile_for(error).exit_code == expected_exit_code


def test_rate_limits_are_matched_before_the_generic_api_error() -> None:
    # Ordering in the profile table matters: the subclass must win.
    assert profile_for(MetaApiRateLimitError("throttled")).title == "Rate limited by Meta"


def test_handled_errors_exit_with_a_panel_rather_than_a_traceback() -> None:
    console = Console(record=True, width=100)

    with pytest.raises(typer.Exit) as exit_info, handle_domain_errors(console):
        raise MetaApiAuthenticationError(
            "Error validating access token",
            fbtrace_id="TRACE123",
        )

    assert exit_info.value.exit_code == EXIT_AUTHENTICATION_ERROR
    output = console.export_text()
    assert "Authentication failed" in output
    assert "ads_read" not in output  # remedy for permissions, not authentication
    assert "system user token" in output
    # Meta support cannot investigate without the trace ID.
    assert "TRACE123" in output


def test_unknown_exceptions_are_not_disguised_as_handled_failures() -> None:
    console = Console(record=True)

    # A bug must keep its traceback rather than becoming a tidy exit code.
    with pytest.raises(ZeroDivisionError), handle_domain_errors(console):
        _ = 1 / 0


def test_a_successful_body_produces_no_output() -> None:
    console = Console(record=True)

    with handle_domain_errors(console):
        pass

    assert console.export_text().strip() == ""


def test_missing_application_context_is_a_wiring_error() -> None:
    ctx = MagicMock()
    ctx.obj = None

    with pytest.raises(TypeError, match="root CLI callback"):
        application_context(ctx)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_empty_results_explain_what_to_do_next() -> None:
    console = Console(record=True, width=120)

    render_accounts(console, [])
    render_campaigns(console, [], currency="USD")
    render_creatives(console, [])
    render_recommendations(console, [])

    output = console.export_text()
    assert "--sync" in output
    assert "No recommendations" in output


def test_accounts_render_as_a_table() -> None:
    console = Console(record=True, width=160)
    account = build_account(remote_id="act_42", name="Acme", currency="USD")

    render_accounts(console, [account])

    output = console.export_text()
    assert "act_42" in output
    assert "Acme" in output


def test_campaigns_render_budgets_with_the_currency() -> None:
    console = Console(record=True, width=160)
    campaign = build_campaign(1, remote_id="c1", name="Spring", daily_budget=Decimal("100.00"))

    render_campaigns(console, [campaign], currency="USD")

    output = console.export_text()
    assert "Spring" in output
    assert "USD" in output


def test_undefined_metrics_render_as_a_dash_not_a_zero() -> None:
    console = Console(record=True, width=200)
    report = PerformanceReport(
        account_remote_id="act_1",
        currency="USD",
        level=InsightLevel.CAMPAIGN,
        since=date(2026, 6, 8),
        until=date(2026, 6, 14),
        entries=[
            EntityWindowMetrics(
                entity_remote_id="c1",
                entity_name="No conversions",
                current=PerformanceMetrics(spend=Decimal(100), impressions=1_000, conversions=0),
                previous=PerformanceMetrics(),
                has_history=False,
            )
        ],
    )

    render_performance(console, report)

    output = console.export_text()
    assert "No conversions" in output
    # Printing 0.00 for an undefined cost per acquisition would read as "free".
    assert "—" in output


def test_performance_table_includes_a_totals_row() -> None:
    console = Console(record=True, width=200)
    report = PerformanceReport(
        account_remote_id="act_1",
        currency="USD",
        level=InsightLevel.CAMPAIGN,
        since=date(2026, 6, 8),
        until=date(2026, 6, 14),
        entries=[
            EntityWindowMetrics(
                entity_remote_id=f"c{index}",
                entity_name=f"Campaign {index}",
                current=PerformanceMetrics(
                    spend=Decimal(100), impressions=1_000, clicks=50, conversions=5
                ),
                previous=PerformanceMetrics(),
                has_history=False,
            )
            for index in range(2)
        ],
    )

    render_performance(console, report)

    assert "Total" in console.export_text()


def test_creative_bodies_are_truncated_to_protect_the_layout() -> None:
    console = Console(record=True, width=200)
    usage = CreativeUsage(
        creative=build_creative(1, body="word " * 80),
        ad_count=3,
        active_ad_count=1,
    )

    render_creatives(console, [usage])

    # Ad copy runs to paragraphs; untruncated it would destroy the table.
    assert "…" in console.export_text()


# ---------------------------------------------------------------------------
# Application wiring
# ---------------------------------------------------------------------------


def test_help_lists_every_command_without_needing_configuration() -> None:
    # --help is handled before the root callback, so asking for help must never
    # require a valid Meta token or a reachable database.
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0
    for command in ("accounts", "campaigns", "insights", "optimize", "creatives"):
        assert command in result.output


@pytest.mark.parametrize(
    "command",
    ["accounts", "campaigns", "insights", "optimize", "creatives"],
)
def test_each_command_documents_itself(command: str) -> None:
    result = CliRunner().invoke(app, [command, "--help"])

    assert result.exit_code == 0
    assert result.output.strip()
