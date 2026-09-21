from datetime import UTC, datetime, timedelta

from k5vision.autonomy_policy import (
    GrantLedger,
    PolicyOutcome,
    PolicyRule,
    evaluate_proposal,
)
from k5vision.neural_contracts import (
    ActionProposal,
    AutonomyMode,
    ExecutionGrant,
)


def now() -> datetime:
    return datetime.now(UTC)


def proposal(*, confidence: float = 0.9) -> ActionProposal:
    created = now()
    return ActionProposal(
        proposal_id="proposal-1",
        created_at=created,
        expires_at=created + timedelta(seconds=30),
        action_type="generic-action",
        target_ref="target-1",
        rationale="bounded test proposal",
        confidence=confidence,
        producer_version="0.1.0",
    )


def rule() -> PolicyRule:
    return PolicyRule(
        rule_id="rule-1",
        action_type="generic-action",
        allowed_targets=frozenset({"target-1"}),
        min_confidence=0.8,
        allowed_modes=frozenset({AutonomyMode.AUTO, AutonomyMode.EMERGENCY}),
    )


def grant_for(item: ActionProposal, *, grant_id: str = "grant-1") -> ExecutionGrant:
    issued = item.created_at
    return ExecutionGrant(
        grant_id=grant_id,
        proposal_id=item.proposal_id,
        proposal_digest=item.canonical_digest(),
        issued_at=issued,
        expires_at=issued + timedelta(seconds=10),
        action_type=item.action_type,
        target_ref=item.target_ref,
        policy_version="policy-1",
        autonomy_mode=AutonomyMode.AUTO,
        issued_by="k5-policy-engine",
    )


def test_auto_mode_executes_only_on_explicit_policy_match() -> None:
    result = evaluate_proposal(
        proposal(),
        mode=AutonomyMode.AUTO,
        now=now(),
        rules=(rule(),),
    )
    assert result.outcome is PolicyOutcome.ALLOW_AUTO
    assert result.rule_id == "rule-1"


def test_manual_mode_requires_operator() -> None:
    result = evaluate_proposal(
        proposal(),
        mode=AutonomyMode.MANUAL,
        now=now(),
        rules=(rule(),),
    )
    assert result.outcome is PolicyOutcome.REQUIRE_OPERATOR


def test_low_confidence_fails_closed() -> None:
    result = evaluate_proposal(
        proposal(confidence=0.4),
        mode=AutonomyMode.AUTO,
        now=now(),
        rules=(rule(),),
    )
    assert result.outcome is PolicyOutcome.DENY


def test_expired_proposal_is_denied() -> None:
    item = proposal()
    result = evaluate_proposal(
        item,
        mode=AutonomyMode.AUTO,
        now=item.expires_at,
        rules=(rule(),),
    )
    assert result.outcome is PolicyOutcome.DENY
    assert result.reason == "proposal-expired"


def test_execution_grant_is_single_use() -> None:
    item = proposal()
    grant = grant_for(item)
    ledger = GrantLedger()
    assert ledger.consume(
        grant,
        proposal=item,
        now=item.created_at + timedelta(seconds=1),
    ) is True
    assert ledger.consume(
        grant,
        proposal=item,
        now=item.created_at + timedelta(seconds=2),
    ) is False


def test_execution_grant_rejects_mismatched_proposal() -> None:
    item = proposal()
    grant = grant_for(item)
    item.constraints["tampered"] = True
    ledger = GrantLedger()
    assert ledger.consume(
        grant,
        proposal=item,
        now=item.created_at + timedelta(seconds=1),
    ) is False


def test_expired_execution_grant_is_rejected() -> None:
    item = proposal()
    issued = item.created_at
    grant = ExecutionGrant(
        grant_id="grant-2",
        proposal_id=item.proposal_id,
        proposal_digest=item.canonical_digest(),
        issued_at=issued,
        expires_at=issued + timedelta(seconds=1),
        action_type=item.action_type,
        target_ref=item.target_ref,
        policy_version="policy-1",
        autonomy_mode=AutonomyMode.AUTO,
        issued_by="k5-policy-engine",
    )
    ledger = GrantLedger()
    assert ledger.consume(
        grant,
        proposal=item,
        now=issued + timedelta(seconds=2),
    ) is False
