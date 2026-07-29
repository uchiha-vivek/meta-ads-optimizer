"""Rich rendering of service results into terminal tables.

Presentation only. These functions receive objects that services produced and
turn them into tables; they compute nothing, decide nothing, and reach for no
data. If a figure appears here that was not handed in, the calculation is in the
wrong layer.

Formatting rules applied consistently throughout:

*Undefined is ``—``, never ``0``.* A campaign with no clicks has no cost per
click, and printing ``0.00`` would read as "free" rather than "not applicable".

*Numbers are right-aligned and thousands-separated*, because the reason to put
figures in a column is to compare their magnitudes at a glance.

*Deterioration is red, improvement green*, evaluated per metric — a rising cost
per acquisition is red while a rising return on ad spend is green.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Final

from rich.console import Console
from rich.table import Table
from rich.text import Text

from app.analytics.trends import MetricChange
from app.models.ad_account import AdAccount
from app.models.campaign import Campaign
from app.models.enums import RecommendationSeverity
from app.models.recommendation import Recommendation
from app.services.creative_service import CreativeUsage
from app.services.insight_service import PerformanceReport

_UNDEFINED: Final[str] = "—"
_MONEY_QUANTUM: Final[Decimal] = Decimal("0.01")
_RATIO_QUANTUM: Final[Decimal] = Decimal("0.01")
_PERCENT_QUANTUM: Final[Decimal] = Decimal("0.1")

# Longest creative body shown inline before truncation. Ad copy runs to
# paragraphs and would otherwise destroy the table layout.
_BODY_PREVIEW_LENGTH: Final[int] = 60

_SEVERITY_STYLES: Final[dict[RecommendationSeverity, str]] = {
    RecommendationSeverity.CRITICAL: "bold red",
    RecommendationSeverity.WARNING: "yellow",
    RecommendationSeverity.INFO: "cyan",
}


def render_accounts(console: Console, accounts: Sequence[AdAccount]) -> None:
    """Print a table of ad accounts.

    Args:
        console: Destination console.
        accounts: Accounts to display.
    """
    if not accounts:
        console.print("No ad accounts stored. Run `meta accounts --sync` to fetch them.")
        return

    table = Table(title="Ad accounts", header_style="bold", expand=False)
    table.add_column("Account ID", no_wrap=True)
    table.add_column("Name")
    table.add_column("Business")
    table.add_column("Currency", justify="center")
    table.add_column("Timezone")
    table.add_column("Spent", justify="right")

    for account in accounts:
        table.add_row(
            account.remote_id,
            account.name or _UNDEFINED,
            account.business_name or _UNDEFINED,
            account.currency or _UNDEFINED,
            account.timezone_name or _UNDEFINED,
            _money(account.amount_spent, account.currency),
        )

    console.print(table)


def render_campaigns(
    console: Console,
    campaigns: Sequence[Campaign],
    *,
    currency: str | None,
) -> None:
    """Print a table of campaigns.

    Args:
        console: Destination console.
        campaigns: Campaigns to display.
        currency: ISO 4217 code the budgets are expressed in.
    """
    if not campaigns:
        console.print("No campaigns matched. Run with `--sync` to fetch from Meta.")
        return

    table = Table(title="Campaigns", header_style="bold", expand=False)
    table.add_column("Campaign ID", no_wrap=True)
    table.add_column("Name")
    table.add_column("Status", justify="center")
    table.add_column("Objective")
    table.add_column("Daily budget", justify="right")
    table.add_column("Lifetime budget", justify="right")

    for campaign in campaigns:
        table.add_row(
            campaign.remote_id,
            campaign.name or _UNDEFINED,
            _status_text(campaign.status.value, is_delivering=campaign.is_delivering),
            campaign.objective or _UNDEFINED,
            _money(campaign.daily_budget, currency),
            _money(campaign.lifetime_budget, currency),
        )

    console.print(table)


def render_performance(console: Console, report: PerformanceReport) -> None:
    """Print a per-entity performance table with period-over-period movement.

    Args:
        console: Destination console.
        report: The report to display.
    """
    if not report.entries:
        console.print(
            f"No {report.level.value} insights stored for "
            f"{report.since.isoformat()} to {report.until.isoformat()}. "
            f"Run with `--sync` to fetch from Meta."
        )
        return

    title = (
        f"{report.level.value.title()} performance · "
        f"{report.since.isoformat()} to {report.until.isoformat()}"
    )
    table = Table(title=title, header_style="bold", expand=False)
    table.add_column("Entity")
    table.add_column("Spend", justify="right")
    table.add_column("Impr.", justify="right")
    table.add_column("Clicks", justify="right")
    table.add_column("CTR", justify="right")
    table.add_column("CPC", justify="right")
    table.add_column("Conv.", justify="right")
    table.add_column("CPA", justify="right")
    table.add_column("ROAS", justify="right")
    table.add_column("Freq.", justify="right")
    table.add_column("CPA Δ", justify="right")

    for entry in report.entries:
        metrics = entry.current
        comparison = entry.comparison
        table.add_row(
            entry.entity_name or entry.entity_remote_id,
            _money(metrics.spend, report.currency),
            f"{metrics.impressions:,}",
            f"{metrics.clicks:,}",
            _percent(metrics.click_through_rate),
            _money(metrics.cost_per_click, report.currency),
            f"{metrics.conversions:,}",
            _money(metrics.cost_per_acquisition, report.currency),
            _ratio(metrics.return_on_ad_spend),
            _ratio(metrics.frequency),
            _change(comparison.cost_per_acquisition if comparison else None, lower_is_better=True),
        )

    totals = report.totals
    table.add_section()
    table.add_row(
        Text("Total", style="bold"),
        Text(_money(totals.spend, report.currency), style="bold"),
        Text(f"{totals.impressions:,}", style="bold"),
        Text(f"{totals.clicks:,}", style="bold"),
        Text(_percent(totals.click_through_rate), style="bold"),
        Text(_money(totals.cost_per_click, report.currency), style="bold"),
        Text(f"{totals.conversions:,}", style="bold"),
        Text(_money(totals.cost_per_acquisition, report.currency), style="bold"),
        Text(_ratio(totals.return_on_ad_spend), style="bold"),
        _UNDEFINED,
        _UNDEFINED,
    )

    console.print(table)


def render_recommendations(
    console: Console,
    recommendations: Sequence[Recommendation],
    *,
    show_rationale: bool = False,
) -> None:
    """Print a table of recommendations, most urgent first.

    Args:
        console: Destination console.
        recommendations: Recommendations to display.
        show_rationale: Include the full reasoning for each finding. Off by
            default because the rationale is a paragraph and the table is meant
            to be scannable.
    """
    if not recommendations:
        console.print("No recommendations. Every evaluated entity is within policy.")
        return

    table = Table(title="Optimization recommendations", header_style="bold", expand=False)
    table.add_column("ID", justify="right", no_wrap=True)
    table.add_column("Severity", justify="center")
    table.add_column("Entity")
    table.add_column("Finding")
    table.add_column("Action")
    table.add_column("Auto", justify="center")

    for recommendation in recommendations:
        style = _SEVERITY_STYLES.get(recommendation.severity, "")
        table.add_row(
            str(recommendation.id),
            Text(recommendation.severity.value.upper(), style=style),
            recommendation.entity_name or recommendation.entity_remote_id,
            recommendation.title,
            recommendation.action.value.replace("_", " "),
            "yes" if recommendation.action.is_automatable else "no",
        )

    console.print(table)

    if show_rationale:
        for recommendation in recommendations:
            style = _SEVERITY_STYLES.get(recommendation.severity, "")
            console.print()
            console.print(Text(f"[{recommendation.id}] {recommendation.title}", style=style))
            console.print(recommendation.rationale)


def render_creatives(console: Console, usages: Sequence[CreativeUsage]) -> None:
    """Print a table of creatives with their deployment counts.

    Args:
        console: Destination console.
        usages: Creatives and their usage counts.
    """
    if not usages:
        console.print("No creatives stored. Run with `--sync` to fetch from Meta.")
        return

    table = Table(title="Ad creatives", header_style="bold", expand=False)
    table.add_column("Creative ID", no_wrap=True)
    table.add_column("Name")
    table.add_column("Type", justify="center")
    table.add_column("Call to action")
    table.add_column("Body")
    table.add_column("Ads", justify="right")
    table.add_column("Active", justify="right")

    for usage in usages:
        creative = usage.creative
        table.add_row(
            creative.remote_id,
            creative.name or _UNDEFINED,
            "video" if creative.is_video else (creative.object_type or _UNDEFINED),
            creative.call_to_action_type or _UNDEFINED,
            _truncate(creative.body),
            str(usage.ad_count),
            Text(str(usage.active_ad_count), style="green" if usage.is_in_use else "dim"),
        )

    console.print(table)


def _money(value: Decimal | None, currency: str | None) -> str:
    """Render a monetary amount, or an em dash when undefined."""
    if value is None:
        return _UNDEFINED
    amount = f"{value.quantize(_MONEY_QUANTUM):,}"
    return f"{amount} {currency}" if currency else amount


def _percent(value: Decimal | None) -> str:
    """Render a percentage to one decimal place, or an em dash when undefined."""
    if value is None:
        return _UNDEFINED
    return f"{value.quantize(_PERCENT_QUANTUM)}%"


def _ratio(value: Decimal | None) -> str:
    """Render a bare ratio to two decimal places, or an em dash when undefined."""
    if value is None:
        return _UNDEFINED
    return str(value.quantize(_RATIO_QUANTUM))


def _change(change: MetricChange | None, *, lower_is_better: bool) -> Text:
    """Render a period-over-period movement, coloured by whether it is good news.

    Args:
        change: The movement, or ``None`` when no comparison exists.
        lower_is_better: ``True`` for cost metrics, where a rise is bad;
            ``False`` for return metrics, where a rise is good.

    Returns:
        Styled text carrying a sign and a percentage.
    """
    if change is None:
        return Text(_UNDEFINED)
    percent_change = change.percent_change
    if percent_change is None:
        return Text(_UNDEFINED)

    rounded = percent_change.quantize(_PERCENT_QUANTUM)
    is_bad_news = (rounded > 0) if lower_is_better else (rounded < 0)
    style = "red" if is_bad_news else "green"
    sign = "+" if rounded > 0 else ""
    return Text(f"{sign}{rounded}%", style=style)


def _status_text(status: str, *, is_delivering: bool) -> Text:
    """Render a delivery status, highlighting entities that are spending."""
    return Text(status, style="green" if is_delivering else "dim")


def _truncate(value: str | None) -> str:
    """Shorten long ad copy so it cannot destroy the table layout."""
    if not value:
        return _UNDEFINED
    collapsed = " ".join(value.split())
    if len(collapsed) <= _BODY_PREVIEW_LENGTH:
        return collapsed
    return f"{collapsed[: _BODY_PREVIEW_LENGTH - 1]}…"
