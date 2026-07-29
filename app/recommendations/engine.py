"""The recommendation engine: runs a rule set over entity contexts.

The engine holds no rules of its own. Rules and thresholds are injected, which
is what allows the LLM-backed engine described in the project brief to be added
later by passing different rule instances — the engine, the service that calls
it, and the CLI all stay exactly as they are, because they depend on the
:class:`~app.recommendations.rules.Rule` protocol rather than on any concrete
rule.

One rule raising must not lose the findings of the others. A rule that fails is
logged and skipped, and evaluation continues; an account's remaining fifty
campaigns are still worth reporting on.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence

from app.recommendations.context import RecommendationContext, RecommendationProposal
from app.recommendations.rules import Rule, RuleThresholds, default_rules

_logger = logging.getLogger(__name__)


class RecommendationEngine:
    """Applies a set of rules to entity contexts and collects the findings."""

    def __init__(
        self,
        *,
        rules: Sequence[Rule] | None = None,
        thresholds: RuleThresholds | None = None,
    ) -> None:
        self._rules: tuple[Rule, ...] = tuple(rules) if rules is not None else default_rules()
        self._thresholds = thresholds if thresholds is not None else RuleThresholds()

    @property
    def rule_codes(self) -> tuple[str, ...]:
        """Identifiers of every rule this engine will apply."""
        return tuple(rule.code for rule in self._rules)

    @property
    def thresholds(self) -> RuleThresholds:
        """The policy this engine judges against."""
        return self._thresholds

    def evaluate(self, context: RecommendationContext) -> list[RecommendationProposal]:
        """Apply every rule to one entity.

        Args:
            context: Everything known about the entity.

        Returns:
            The findings, most urgent first. Empty when no rule had anything to
            say, which is the expected result for a healthy entity.
        """
        proposals: list[RecommendationProposal] = []
        for rule in self._rules:
            proposal = self._evaluate_rule(rule, context)
            if proposal is not None:
                proposals.append(proposal)

        return sorted(proposals, key=lambda proposal: proposal.severity.rank, reverse=True)

    def evaluate_all(
        self,
        contexts: Iterable[RecommendationContext],
    ) -> list[tuple[RecommendationContext, RecommendationProposal]]:
        """Apply every rule to many entities.

        Each finding is paired with the context that produced it, because the
        service needs the entity's identity to persist the recommendation and
        the proposal alone does not carry it.

        Args:
            contexts: Entities to evaluate.

        Returns:
            Every ``(context, proposal)`` pair, most urgent first across all
            entities, so the account-wide view leads with what matters most.
        """
        paired: list[tuple[RecommendationContext, RecommendationProposal]] = []
        for context in contexts:
            paired.extend((context, proposal) for proposal in self.evaluate(context))

        return sorted(paired, key=lambda pair: pair[1].severity.rank, reverse=True)

    def _evaluate_rule(
        self,
        rule: Rule,
        context: RecommendationContext,
    ) -> RecommendationProposal | None:
        """Run one rule, containing any failure to that rule alone.

        A rule is third-party-shaped code: it may be contributed, generated, or
        LLM-backed. Letting one raise and abandon the evaluation would discard
        every finding from every other rule across every other entity, which is
        a far worse outcome than one rule's silence.
        """
        try:
            return rule.evaluate(context, self._thresholds)
        except Exception:
            _logger.exception(
                "Rule raised while evaluating an entity and was skipped",
                extra={
                    "rule_code": rule.code,
                    "entity_remote_id": context.entity_remote_id,
                    "level": context.level.value,
                },
            )
            return None
