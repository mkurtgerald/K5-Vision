"""Deterministic policy primitives for K5 autonomous execution."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from collections.abc import Iterable

from .neural_contracts import ActionProposal, AutonomyMode, ExecutionGrant


class PolicyOutcome(StrEnum):
    DENY = "deny"
    REQUIRE_OPERATOR = "require-operator"
    ALLOW_AUTO = "allow-auto"


@dataclass(frozen=True)
class PolicyRule:
    rule_id: str
    action_type: str
    allowed_targets: frozenset[str]
    min_confidence: float
    allowed_modes: frozenset[AutonomyMode]

    def matches(self, proposal: ActionProposal, mode: AutonomyMode) -> bool:
        return (
            proposal.action_type == self.action_type
            and proposal.target_ref in self.allowed_targets
            and proposal.confidence >= self.min_confidence
            and mode in self.allowed_modes
        )


@dataclass(frozen=True)
class PolicyEvaluation:
    outcome: PolicyOutcome
    rule_id: str | None
    reason: str


def evaluate_proposal(
    proposal: ActionProposal,
    *,
    mode: AutonomyMode,
    now: datetime,
    rules: Iterable[PolicyRule],
) -> PolicyEvaluation:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("timezone-aware timestamp required")

    if proposal.expires_at <= now:
        return PolicyEvaluation(PolicyOutcome.DENY, None, "proposal-expired")

    if mode is AutonomyMode.MANUAL:
        return PolicyEvaluation(PolicyOutcome.REQUIRE_OPERATOR, None, "manual-mode")

    for rule in rules:
        if rule.matches(proposal, mode):
            if mode in {AutonomyMode.AUTO, AutonomyMode.EMERGENCY}:
                return PolicyEvaluation(PolicyOutcome.ALLOW_AUTO, rule.rule_id, "policy-match")
            return PolicyEvaluation(
                PolicyOutcome.REQUIRE_OPERATOR,
                rule.rule_id,
                "assisted-mode",
            )

    return PolicyEvaluation(PolicyOutcome.DENY, None, "no-policy-match")


class GrantLedger:
    """In-memory single-use replay guard for execution grants.

    Durable implementations can replace this object without changing the grant
    contract or consumption semantics.
    """

    def __init__(self) -> None:
        self._consumed: set[str] = set()

    def consume(self, grant: ExecutionGrant, *, now: datetime) -> bool:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("timezone-aware timestamp required")
        if grant.expires_at <= now:
            return False
        if grant.grant_id in self._consumed:
            return False
        self._consumed.add(grant.grant_id)
        return True
