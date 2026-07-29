"""Tests for the command layer, driving real command bodies against stubs.

These assert the architectural rule the project is built on: a command parses
input, calls exactly one service, and renders the result. Every service here is
a stub, so a command that reached past the service layer — into a repository,
the database, or the API client — would have nothing to reach and would fail.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from unittest.mock import MagicMock

import pytest
from rich.console import Console
from typer.testing import CliRunner, Result

from app.cli.context import ApplicationContext, ApplicationContextProvider
from app.cli.error_handling import EXIT_AUTHENTICATION_ERROR, EXIT_GENERAL_ERROR
from app.cli.main import app
from app.models.enums import EntityStatus, InsightLevel
from app.utils.exceptions import EntityNotFoundError, MetaApiAuthenticationError

CONFIGURED_ACCOUNT_ID = "act_from_configuration"

# Runs the real CLI against a stub context and returns Click's captured result.
CliInvoker = Callable[[list[str]], Result]


@pytest.fixture
def stub_context() -> MagicMock:
    """An application context whose services are stubs and console is recorded."""
    context = MagicMock(spec=ApplicationContext)
    context.console = Console(record=True, width=200)
    context.settings.meta.ad_account_id = CONFIGURED_ACCOUNT_ID
    context.accounts.list_accounts.return_value = []
    context.accounts.get_account.return_value = MagicMock(currency="USD")
    context.campaigns.list_campaigns.return_value = []
    context.creatives.list_creatives.return_value = []
    context.optimization.list_open_recommendations.return_value = []
    context.optimization.generate_recommendations.return_value = MagicMock(
        entities_evaluated=3,
        level=InsightLevel.CAMPAIGN,
        recommendations=[],
    )
    return context


@pytest.fixture
def invoke(stub_context: MagicMock) -> CliInvoker:
    """Return a callable that runs the CLI against the stub context."""
    provider = ApplicationContextProvider(lambda: stub_context)
    runner = CliRunner()

    def run(arguments: list[str]) -> Result:
        return runner.invoke(app, arguments, obj=provider)

    return run


# ---------------------------------------------------------------------------
# meta accounts
# ---------------------------------------------------------------------------


def test_accounts_reads_locally_by_default(invoke: CliInvoker, stub_context: MagicMock) -> None:
    result = invoke(["accounts"])

    assert result.exit_code == 0
    stub_context.accounts.list_accounts.assert_called_once_with(refresh=False)


def test_accounts_sync_flag_requests_a_refresh(invoke: CliInvoker, stub_context: MagicMock) -> None:
    invoke(["accounts", "--sync"])

    stub_context.accounts.list_accounts.assert_called_once_with(refresh=True)


# ---------------------------------------------------------------------------
# meta campaigns
# ---------------------------------------------------------------------------


def test_campaigns_defaults_to_the_configured_account(
    invoke: CliInvoker, stub_context: MagicMock
) -> None:
    invoke(["campaigns"])

    args, kwargs = stub_context.campaigns.list_campaigns.call_args
    assert args[0] == CONFIGURED_ACCOUNT_ID
    assert kwargs["statuses"] is None
    assert kwargs["name_contains"] is None


def test_campaigns_forwards_parsed_filters(invoke: CliInvoker, stub_context: MagicMock) -> None:
    invoke(
        [
            "campaigns",
            "--account-id",
            "act_explicit",
            "--status",
            "active,paused",
            "--name",
            "spring",
            "--sync",
        ]
    )

    args, kwargs = stub_context.campaigns.list_campaigns.call_args
    assert args[0] == "act_explicit"
    # Parsed into domain enums by the CLI, so the service never sees raw strings.
    assert kwargs["statuses"] == [EntityStatus.ACTIVE, EntityStatus.PAUSED]
    assert kwargs["name_contains"] == "spring"
    assert kwargs["refresh"] is True


def test_campaigns_rejects_an_unknown_status(invoke: CliInvoker, stub_context: MagicMock) -> None:
    result = invoke(["campaigns", "--status", "exploded"])

    assert result.exit_code != 0
    stub_context.campaigns.list_campaigns.assert_not_called()


# ---------------------------------------------------------------------------
# meta insights
# ---------------------------------------------------------------------------


def test_insights_resolves_level_and_window(invoke: CliInvoker, stub_context: MagicMock) -> None:
    invoke(["insights", "--level", "adset", "--days", "14"])

    _, kwargs = stub_context.insights.performance_report.call_args
    assert kwargs["level"] is InsightLevel.ADSET
    assert isinstance(kwargs["since"], date)
    # Fourteen days inclusive of both ends.
    assert (kwargs["until"] - kwargs["since"]).days == 13


def test_insights_rejects_an_unknown_level(invoke: CliInvoker, stub_context: MagicMock) -> None:
    result = invoke(["insights", "--level", "galaxy"])

    assert result.exit_code != 0
    stub_context.insights.performance_report.assert_not_called()


def test_insights_rejects_a_non_positive_window(
    invoke: CliInvoker, stub_context: MagicMock
) -> None:
    result = invoke(["insights", "--days", "0"])

    assert result.exit_code != 0
    stub_context.insights.performance_report.assert_not_called()


# ---------------------------------------------------------------------------
# meta creatives
# ---------------------------------------------------------------------------


def test_creatives_forwards_the_in_use_filter(invoke: CliInvoker, stub_context: MagicMock) -> None:
    invoke(["creatives", "--in-use"])

    _, kwargs = stub_context.creatives.list_creatives.call_args
    assert kwargs["in_use_only"] is True
    assert kwargs["refresh"] is False


# ---------------------------------------------------------------------------
# meta optimize
# ---------------------------------------------------------------------------


def test_optimize_generates_without_changing_anything(
    invoke: CliInvoker, stub_context: MagicMock
) -> None:
    result = invoke(["optimize"])

    assert result.exit_code == 0
    stub_context.optimization.generate_recommendations.assert_called_once()
    # Generating must never mutate a live ad account.
    stub_context.optimization.apply_recommendation.assert_not_called()


def test_optimize_open_lists_without_re_evaluating(
    invoke: CliInvoker, stub_context: MagicMock
) -> None:
    invoke(["optimize", "--open"])

    stub_context.optimization.list_open_recommendations.assert_called_once_with(
        CONFIGURED_ACCOUNT_ID
    )
    stub_context.optimization.generate_recommendations.assert_not_called()


def test_optimize_apply_targets_a_single_recommendation(
    invoke: CliInvoker,
    stub_context: MagicMock,
) -> None:
    result = invoke(["optimize", "--apply", "42"])

    assert result.exit_code == 0
    stub_context.optimization.apply_recommendation.assert_called_once_with(42)
    # Applying is an explicit, single-target request, never a side effect of
    # asking the tool a question.
    stub_context.optimization.generate_recommendations.assert_not_called()


def test_optimize_dismiss_records_a_rejection(invoke: CliInvoker, stub_context: MagicMock) -> None:
    invoke(["optimize", "--dismiss", "7"])

    stub_context.optimization.dismiss_recommendation.assert_called_once_with(7)
    stub_context.optimization.apply_recommendation.assert_not_called()


def test_optimize_reports_how_many_entities_were_examined(
    invoke: CliInvoker,
    stub_context: MagicMock,
) -> None:
    invoke(["optimize"])

    # No findings across three campaigns says something different from no
    # findings across none.
    assert "Evaluated 3 campaign(s)" in stub_context.console.export_text()


# ---------------------------------------------------------------------------
# Error handling through a real command
# ---------------------------------------------------------------------------


def test_an_expired_token_exits_with_its_own_code(
    invoke: CliInvoker, stub_context: MagicMock
) -> None:
    stub_context.accounts.list_accounts.side_effect = MetaApiAuthenticationError(
        "Error validating access token"
    )

    result = invoke(["accounts", "--sync"])

    assert result.exit_code == EXIT_AUTHENTICATION_ERROR


def test_an_unsynced_account_is_reported_not_crashed(
    invoke: CliInvoker, stub_context: MagicMock
) -> None:
    stub_context.campaigns.list_campaigns.side_effect = EntityNotFoundError(
        "Ad account is not present locally; run `meta accounts --sync` first"
    )

    result = invoke(["campaigns"])

    assert result.exit_code != 0
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_an_unexpected_failure_keeps_its_traceback(
    invoke: CliInvoker, stub_context: MagicMock
) -> None:
    stub_context.accounts.list_accounts.side_effect = ZeroDivisionError("bug")

    result = invoke(["accounts"])

    # A bug must not be disguised as a handled condition.
    assert result.exit_code != EXIT_GENERAL_ERROR or isinstance(result.exception, ZeroDivisionError)
    assert isinstance(result.exception, ZeroDivisionError)
