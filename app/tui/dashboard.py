"""A Textual dashboard over the optimization services.

The dashboard is read-mostly: it evaluates the account once on entry, then lets
an operator page through campaigns, performance, creatives, and findings, and
act on a finding by pressing a key. Both mutating actions — apply and dismiss —
route through :class:`~app.services.optimization_service.OptimizationService`,
so the same guarantees the CLI has hold here: apply refuses anything the engine
did not mark automatable, and it is the only path that touches the live account.

Service calls run in worker threads. A synchronous database round trip on the
event loop would freeze the interface for its duration; off-loading it keeps the
dashboard responsive and lets a spinner or a busy state show while it works.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.coordinate import Coordinate
from textual.widgets import DataTable, Footer, Header, Static, TabbedContent, TabPane

from app.cli.options import DEFAULT_LOOKBACK_DAYS, resolve_window
from app.models.enums import InsightLevel, RecommendationSeverity

if TYPE_CHECKING:
    from app.cli.context import ApplicationContext
    from app.models.campaign import Campaign
    from app.models.recommendation import Recommendation
    from app.services.creative_service import CreativeUsage
    from app.services.insight_service import PerformanceReport

_SEVERITY_STYLE: dict[RecommendationSeverity, str] = {
    RecommendationSeverity.CRITICAL: "bold red",
    RecommendationSeverity.WARNING: "bold yellow",
    RecommendationSeverity.INFO: "cyan",
}


@dataclass(slots=True)
class _Bundle:
    """One consistent snapshot of everything the dashboard renders.

    Gathered in a single worker pass so the four tabs cannot show figures from
    two different reads of the database.
    """

    account_name: str
    currency: str | None
    amount_spent: Decimal | None
    since: date
    until: date
    recommendations: list[Recommendation]
    campaigns: list[Campaign]
    report: PerformanceReport
    creatives: list[CreativeUsage]


class DashboardApp(App[None]):
    """An interactive dashboard for one ad account.

    Attributes:
        context: The wired services, shared with the CLI.
        account_remote_id: The account the dashboard reports on.
    """

    CSS = """
    Screen {
        layout: vertical;
    }
    #summary {
        height: 3;
        padding: 0 1;
        content-align: left middle;
        background: $panel;
        color: $text;
        border: round $primary;
    }
    TabbedContent {
        height: 1fr;
    }
    DataTable {
        height: 1fr;
    }
    """

    BINDINGS = [
        ("r", "reload", "Reload"),
        ("a", "apply", "Apply finding"),
        ("d", "dismiss", "Dismiss finding"),
        ("q", "quit", "Quit"),
    ]

    TITLE = "meta-optimizer"
    SUB_TITLE = "campaign dashboard"

    def __init__(self, *, context: ApplicationContext, account_remote_id: str) -> None:
        """Store the services and the account to report on.

        Args:
            context: The wired application context, built by the command.
            account_remote_id: Meta account ID, including the ``act_`` prefix.
        """
        super().__init__()
        self.context = context
        self.account_remote_id = account_remote_id
        self._generated = False

    def compose(self) -> ComposeResult:
        """Lay out the header, summary line, tabbed tables, and footer."""
        yield Header(show_clock=True)
        yield Static("Loading…", id="summary")
        with TabbedContent(initial="tab-findings"):
            with TabPane("Findings", id="tab-findings"):
                yield DataTable(id="findings", cursor_type="row", zebra_stripes=True)
            with TabPane("Campaigns", id="tab-campaigns"):
                yield DataTable(id="campaigns", cursor_type="row", zebra_stripes=True)
            with TabPane("Performance", id="tab-performance"):
                yield DataTable(id="performance", cursor_type="row", zebra_stripes=True)
            with TabPane("Creatives", id="tab-creatives"):
                yield DataTable(id="creatives", cursor_type="row", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        """Set up column headers once, then trigger the first load."""
        findings = self.query_one("#findings", DataTable)
        findings.add_columns("ID", "Severity", "Entity", "Action", "Auto", "Finding")
        campaigns = self.query_one("#campaigns", DataTable)
        campaigns.add_columns("Campaign", "Status", "Objective", "Daily budget")
        performance = self.query_one("#performance", DataTable)
        performance.add_columns(
            "Campaign", "Spend", "Impr.", "CTR", "CPA", "ROAS", "Freq.", "Conv."
        )
        creatives = self.query_one("#creatives", DataTable)
        creatives.add_columns("Creative", "Type", "Ads", "Active")
        self.action_reload()

    # --- data loading -------------------------------------------------------

    def action_reload(self) -> None:
        """Reload every table from the database in a worker thread."""
        self.query_one("#summary", Static).update("Loading…")
        self.run_worker(self._load, thread=True, exclusive=True)

    def _load(self) -> None:
        """Gather a snapshot off the event loop, then apply it on the loop."""
        bundle = self._gather()
        self.call_from_thread(self._apply_bundle, bundle)

    def _gather(self) -> _Bundle:
        """Read everything the dashboard shows in one consistent pass.

        The account is evaluated once, the first time this runs, so entering the
        dashboard surfaces findings without a separate ``optimize`` step. Later
        reloads only re-read, so a dismissal is not immediately undone by a
        fresh generation.
        """
        since, until = resolve_window(DEFAULT_LOOKBACK_DAYS)
        optimization = self.context.optimization

        if not self._generated:
            optimization.generate_recommendations(
                self.account_remote_id,
                level=InsightLevel.CAMPAIGN,
                since=since,
                until=until,
            )
            self._generated = True

        account = self.context.accounts.get_account(self.account_remote_id)
        return _Bundle(
            account_name=account.name or self.account_remote_id,
            currency=account.currency,
            amount_spent=account.amount_spent,
            since=since,
            until=until,
            recommendations=optimization.list_open_recommendations(self.account_remote_id),
            campaigns=self.context.campaigns.list_campaigns(self.account_remote_id),
            report=self.context.insights.performance_report(
                self.account_remote_id,
                level=InsightLevel.CAMPAIGN,
                since=since,
                until=until,
            ),
            creatives=self.context.creatives.list_creatives(self.account_remote_id),
        )

    def _apply_bundle(self, bundle: _Bundle) -> None:
        """Render a gathered snapshot into the summary line and every table."""
        spent = _money(bundle.amount_spent, bundle.currency)
        open_count = len(bundle.recommendations)
        critical = sum(
            1 for r in bundle.recommendations if r.severity is RecommendationSeverity.CRITICAL
        )
        self.query_one("#summary", Static).update(
            f"[b]{bundle.account_name}[/b]"
            f"  ·  spent {spent}"
            f"  ·  {bundle.since.isoformat()} → {bundle.until.isoformat()}"
            f"  ·  [b]{open_count}[/b] open  ·  [red]{critical} critical[/red]"
        )

        self._fill_findings(bundle.recommendations)
        self._fill_campaigns(bundle.campaigns, bundle.currency)
        self._fill_performance(bundle.report)
        self._fill_creatives(bundle.creatives)

    def _fill_findings(self, recommendations: list[Recommendation]) -> None:
        table = self.query_one("#findings", DataTable)
        table.clear()
        for rec in recommendations:
            severity = Text(rec.severity.value.upper(), style=_SEVERITY_STYLE[rec.severity])
            automatable = rec.action.is_automatable and bool(rec.suggested_change)
            table.add_row(
                str(rec.id),
                severity,
                rec.entity_name or rec.entity_remote_id,
                rec.action.value.replace("_", " "),
                Text("yes", style="green") if automatable else Text("no", style="dim"),
                rec.title,
                key=str(rec.id),
            )

    def _fill_campaigns(self, campaigns: list[Campaign], currency: str | None) -> None:
        table = self.query_one("#campaigns", DataTable)
        table.clear()
        for campaign in campaigns:
            table.add_row(
                campaign.name or campaign.remote_id,
                campaign.status.value,
                campaign.objective or "—",
                _money(campaign.daily_budget, currency),
            )

    def _fill_performance(self, report: PerformanceReport) -> None:
        table = self.query_one("#performance", DataTable)
        table.clear()
        for entry in report.entries:
            metrics = entry.current
            table.add_row(
                entry.entity_name or entry.entity_remote_id,
                _money(metrics.spend, report.currency),
                f"{metrics.impressions:,}",
                _ratio(metrics.click_through_rate, suffix="%"),
                _money(metrics.cost_per_acquisition, report.currency),
                _ratio(metrics.return_on_ad_spend, suffix="x"),
                _ratio(metrics.frequency, suffix="x"),
                f"{metrics.conversions:,}",
            )

    def _fill_creatives(self, creatives: list[CreativeUsage]) -> None:
        table = self.query_one("#creatives", DataTable)
        table.clear()
        for usage in creatives:
            table.add_row(
                usage.creative.name or usage.creative.remote_id,
                usage.creative.object_type or "—",
                str(usage.ad_count),
                str(usage.active_ad_count),
            )

    # --- actions ------------------------------------------------------------

    def _selected_recommendation_id(self) -> int | None:
        """Return the recommendation ID under the cursor on the findings tab.

        Returns:
            The ID, or ``None`` when the findings tab is not active or empty.
        """
        tabs = self.query_one(TabbedContent)
        if tabs.active != "tab-findings":
            self.notify("Switch to the Findings tab to act on a finding.", severity="warning")
            return None
        table = self.query_one("#findings", DataTable)
        if table.row_count == 0:
            self.notify("No open findings.", severity="warning")
            return None
        cell_key = table.coordinate_to_cell_key(Coordinate(table.cursor_row, 0))
        row_key = cell_key.row_key.value
        return int(row_key) if row_key is not None else None

    def action_dismiss(self) -> None:
        """Dismiss the highlighted finding. Database-only; always reversible by reload."""
        recommendation_id = self._selected_recommendation_id()
        if recommendation_id is None:
            return
        self.run_worker(
            lambda: self._mutate("dismiss", recommendation_id),
            thread=True,
        )

    def action_apply(self) -> None:
        """Apply the highlighted finding against the live account.

        This is the one action that calls Meta. It will fail cleanly — surfaced
        as an error toast — when the finding is advisory or the token is
        missing, which is exactly the CLI's behaviour, shown in the UI.
        """
        recommendation_id = self._selected_recommendation_id()
        if recommendation_id is None:
            return
        self.run_worker(
            lambda: self._mutate("apply", recommendation_id),
            thread=True,
        )

    def _mutate(self, operation: str, recommendation_id: int) -> None:
        """Run apply/dismiss in a worker, reporting the outcome as a toast."""
        try:
            if operation == "apply":
                self.context.optimization.apply_recommendation(recommendation_id)
                message = f"Applied finding #{recommendation_id}."
            else:
                self.context.optimization.dismiss_recommendation(recommendation_id)
                message = f"Dismissed finding #{recommendation_id}."
        except Exception as exc:  # noqa: BLE001 - surfaced to the user, not swallowed
            self.call_from_thread(
                self.notify,
                f"Could not {operation} #{recommendation_id}: {exc}",
                severity="error",
            )
            return
        self.call_from_thread(self.notify, message)
        self.call_from_thread(self.action_reload)


def _money(value: Decimal | None, currency: str | None) -> str:
    """Render a monetary amount to two decimal places with its currency code."""
    if value is None:
        return "—"
    amount = value.quantize(Decimal("0.01"))
    return f"{amount:,} {currency}" if currency else f"{amount:,}"


def _ratio(value: Decimal | None, *, suffix: str) -> str:
    """Render a bare ratio to two decimal places with a trailing unit."""
    if value is None:
        return "—"
    return f"{value.quantize(Decimal('0.01'))}{suffix}"
