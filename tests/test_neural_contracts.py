from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from k5vision.neural_contracts import (
    ActionProposal,
    ActionReceipt,
    AuthorityDecision,
    AuthorityLevel,
    AutonomyMode,
    CapabilityHandshake,
    ExecutionGrant,
    ExecutionOutcome,
    ObservationEnvelope,
    RuntimeMode,
)


def now() -> datetime:
    return datetime.now(UTC)


def test_observation_contract_accepts_transport_neutral_payload() -> None:
    item = ObservationEnvelope(
        observation_id="obs-1",
        source_id="source-7",
        observed_at=now(),
        kind="generic-event",
        attributes={"zone": "north", "count": 2},
        confidence=0.94,
        provenance=("sensor-1",),
    )
    assert item.schema_version == "1.0"
    assert item.attributes["count"] == 2


def test_extension_payload_is_detached_from_original_input() -> None:
    source = {"nested": {"items": [1, 2]}}
    item = ObservationEnvelope(
        observation_id="obs-copy",
        source_id="source-7",
        observed_at=now(),
        kind="generic-event",
        attributes=source,
    )
    source["nested"]["items"].append(3)
    assert item.attributes == {"nested": {"items": [1, 2]}}


def test_extension_payload_fails_closed_when_not_bounded_json() -> None:
    with pytest.raises(ValidationError):
        ObservationEnvelope(
            observation_id="obs-object",
            source_id="source-7",
            observed_at=now(),
            kind="generic-event",
            attributes={"bad": object()},
        )

    with pytest.raises(ValidationError):
        ObservationEnvelope(
            observation_id="obs-large",
            source_id="source-7",
            observed_at=now(),
            kind="generic-event",
            attributes={"large": "x" * 20_000},
        )


def test_proposal_has_no_execution_authority() -> None:
    created = now()
    proposal = ActionProposal(
        proposal_id="proposal-1",
        created_at=created,
        expires_at=created + timedelta(seconds=30),
        action_type="generic-action",
        target_ref="target-1",
        rationale="bounded synthetic rationale",
        confidence=0.8,
        requested_authority=AuthorityLevel.BOUNDED,
        producer_version="0.1.0",
    )
    assert not hasattr(proposal, "execute")
    assert not hasattr(proposal, "execution_token")


def test_proposal_expiration_must_follow_creation() -> None:
    created = now()
    with pytest.raises(ValidationError):
        ActionProposal(
            proposal_id="proposal-1",
            created_at=created,
            expires_at=created,
            action_type="generic-action",
            target_ref="target-1",
            rationale="invalid window",
            confidence=0.8,
            producer_version="0.1.0",
        )


def test_naive_timestamps_fail_closed() -> None:
    with pytest.raises(ValidationError):
        CapabilityHandshake(
            generated_at=datetime.now(),
            producer_version="0.1.0",
            runtime_mode=RuntimeMode.LOCAL_SERVICE,
        )


def test_naive_wire_timestamp_fails_closed() -> None:
    with pytest.raises(ValidationError):
        ObservationEnvelope.model_validate(
            {
                "observation_id": "obs-wire-naive",
                "source_id": "source-1",
                "observed_at": "2026-09-21T12:00:00",
                "kind": "generic-event",
            }
        )


def test_offset_wire_timestamp_round_trips_as_aware() -> None:
    item = ObservationEnvelope.model_validate(
        {
            "observation_id": "obs-wire-aware",
            "source_id": "source-1",
            "observed_at": "2026-09-21T08:00:00-04:00",
            "kind": "generic-event",
        }
    )
    restored = ObservationEnvelope.model_validate_json(item.model_dump_json())
    assert restored.observed_at.utcoffset() is not None
    assert restored.observed_at == item.observed_at


def test_mixed_awareness_window_fails_with_validation_error() -> None:
    with pytest.raises(ValidationError):
        ActionProposal.model_validate(
            {
                "proposal_id": "proposal-wire-naive",
                "created_at": "2026-09-21T12:00:00Z",
                "expires_at": "2026-09-21T12:00:30",
                "action_type": "generic-action",
                "target_ref": "target-1",
                "rationale": "invalid mixed-awareness wire window",
                "confidence": 0.8,
                "producer_version": "0.1.0",
            }
        )


def test_equivalent_proposals_have_same_canonical_digest() -> None:
    created = now()
    common = {
        "proposal_id": "proposal-digest",
        "created_at": created,
        "expires_at": created + timedelta(seconds=30),
        "action_type": "generic-action",
        "target_ref": "target-1",
        "rationale": "canonical binding",
        "confidence": 0.8,
        "producer_version": "0.1.0",
    }
    first = ActionProposal(**common, constraints={"b": 2, "a": {"x": 1}})
    second = ActionProposal(**common, constraints={"a": {"x": 1}, "b": 2})
    assert first.canonical_digest() == second.canonical_digest()


def test_execution_grant_detects_post_binding_proposal_mutation() -> None:
    created = now()
    source_constraints = {"zone": {"ids": ["north"]}}
    proposal = ActionProposal(
        proposal_id="proposal-bound",
        created_at=created,
        expires_at=created + timedelta(seconds=30),
        action_type="generic-action",
        target_ref="target-1",
        rationale="canonical binding",
        confidence=0.8,
        producer_version="0.1.0",
        constraints=source_constraints,
    )
    bound_digest = proposal.canonical_digest()
    source_constraints["zone"]["ids"].append("south")
    assert proposal.canonical_digest() == bound_digest

    grant = ExecutionGrant(
        grant_id="grant-bound",
        proposal_id=proposal.proposal_id,
        proposal_digest=bound_digest,
        issued_at=created,
        expires_at=created + timedelta(seconds=10),
        action_type=proposal.action_type,
        target_ref=proposal.target_ref,
        policy_version="policy-7",
        autonomy_mode=AutonomyMode.AUTO,
        issued_by="k5-policy-engine",
    )
    assert grant.binds_proposal(proposal) is True

    proposal.constraints["zone"]["ids"].append("tampered")
    assert grant.binds_proposal(proposal) is False


def test_autonomous_execution_uses_scoped_single_use_grant() -> None:
    issued = now()
    proposal = ActionProposal(
        proposal_id="proposal-1",
        created_at=issued,
        expires_at=issued + timedelta(seconds=30),
        action_type="generic-action",
        target_ref="target-1",
        rationale="grant binding",
        confidence=0.8,
        producer_version="0.1.0",
    )
    grant = ExecutionGrant(
        grant_id="grant-1",
        proposal_id=proposal.proposal_id,
        proposal_digest=proposal.canonical_digest(),
        issued_at=issued,
        expires_at=issued + timedelta(seconds=10),
        action_type=proposal.action_type,
        target_ref=proposal.target_ref,
        policy_version="policy-7",
        autonomy_mode=AutonomyMode.AUTO,
        issued_by="k5-policy-engine",
    )
    assert grant.single_use is True
    assert grant.autonomy_mode is AutonomyMode.AUTO
    assert grant.target_ref == "target-1"
    assert grant.binds_proposal(proposal) is True


def test_execution_grant_fails_closed_on_invalid_expiry() -> None:
    issued = now()
    proposal = ActionProposal(
        proposal_id="proposal-1",
        created_at=issued,
        expires_at=issued + timedelta(seconds=30),
        action_type="generic-action",
        target_ref="target-1",
        rationale="grant binding",
        confidence=0.8,
        producer_version="0.1.0",
    )
    with pytest.raises(ValidationError):
        ExecutionGrant(
            grant_id="grant-1",
            proposal_id=proposal.proposal_id,
            proposal_digest=proposal.canonical_digest(),
            issued_at=issued,
            expires_at=issued,
            action_type=proposal.action_type,
            target_ref=proposal.target_ref,
            policy_version="policy-7",
            autonomy_mode=AutonomyMode.EMERGENCY,
            issued_by="k5-policy-engine",
        )


def test_authority_decision_records_auto_mode_without_grant_token() -> None:
    decision = AuthorityDecision(
        proposal_id="proposal-2",
        decided_at=now(),
        decision="allow",
        policy_version="policy-8",
        decided_by="k5-policy-engine",
        autonomy_mode=AutonomyMode.AUTO,
    )
    assert decision.decision == "allow"
    assert decision.autonomy_mode is AutonomyMode.AUTO
    assert not hasattr(decision, "execution_token")


def test_action_receipt_closes_execution_loop() -> None:
    receipt = ActionReceipt(
        receipt_id="receipt-1",
        proposal_id="proposal-2",
        completed_at=now(),
        decision="allow",
        outcome=ExecutionOutcome.VERIFIED_SUCCESS,
        result="completed",
        policy_version="policy-8",
        authority_ref="grant-2",
    )
    assert receipt.outcome is ExecutionOutcome.VERIFIED_SUCCESS
    assert receipt.failure_reason is None


def test_action_receipt_represents_unknown_outcome_explicitly() -> None:
    receipt = ActionReceipt(
        receipt_id="receipt-unknown",
        proposal_id="proposal-3",
        completed_at=now(),
        decision="allow",
        outcome=ExecutionOutcome.UNKNOWN,
        result="executor acknowledgement timed out",
        policy_version="policy-8",
        authority_ref="grant-3",
        failure_reason="execution result could not be verified",
    )
    assert receipt.outcome is ExecutionOutcome.UNKNOWN


def test_action_receipt_represents_denied_but_observed_anomaly() -> None:
    receipt = ActionReceipt(
        receipt_id="receipt-anomaly",
        proposal_id="proposal-4",
        completed_at=now(),
        decision="deny",
        outcome=ExecutionOutcome.DENIED_BUT_OBSERVED,
        result="device state changed despite denied policy decision",
        policy_version="policy-9",
        authority_ref="decision-4",
        failure_reason="unauthorized execution observed",
    )
    assert receipt.outcome is ExecutionOutcome.DENIED_BUT_OBSERVED


def test_action_receipt_rejects_denied_verified_success() -> None:
    with pytest.raises(ValidationError):
        ActionReceipt(
            receipt_id="receipt-invalid",
            proposal_id="proposal-5",
            completed_at=now(),
            decision="deny",
            outcome=ExecutionOutcome.VERIFIED_SUCCESS,
            result="invalid success",
            policy_version="policy-9",
            authority_ref="decision-5",
        )


def test_action_receipt_requires_reason_for_unknown_outcome() -> None:
    with pytest.raises(ValidationError):
        ActionReceipt(
            receipt_id="receipt-unknown-no-reason",
            proposal_id="proposal-6",
            completed_at=now(),
            decision="allow",
            outcome=ExecutionOutcome.UNKNOWN,
            result="result unavailable",
            policy_version="policy-9",
            authority_ref="grant-6",
        )


def test_capability_handshake_supports_runtime_and_capabilities() -> None:
    item = CapabilityHandshake(
        generated_at=now(),
        producer_version="0.2.0",
        contract_versions=("1.0",),
        capabilities=frozenset({"correlation", "offline-llm"}),
        runtime_mode=RuntimeMode.EMBEDDED,
    )
    assert "offline-llm" in item.capabilities
    assert item.runtime_mode is RuntimeMode.EMBEDDED


def test_contract_models_reject_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        ObservationEnvelope(
            observation_id="obs-2",
            source_id="source-1",
            observed_at=now(),
            kind="generic-event",
            unexpected="nope",
        )


def test_confidence_bounds_fail_closed() -> None:
    with pytest.raises(ValidationError):
        ObservationEnvelope(
            observation_id="obs-3",
            source_id="source-1",
            observed_at=now(),
            kind="generic-event",
            confidence=1.01,
        )


def test_authority_modes_are_explicit() -> None:
    assert [mode.value for mode in AutonomyMode] == [
        "manual",
        "assisted",
        "auto",
        "emergency",
    ]


def test_runtime_modes_are_explicit() -> None:
    assert {mode.value for mode in RuntimeMode} == {
        "embedded",
        "local-service",
        "remote-service",
    }


def test_authority_levels_are_explicit() -> None:
    assert {level.value for level in AuthorityLevel} == {
        "observe",
        "recommend",
        "confirm",
        "bounded",
        "high",
        "emergency",
    }
