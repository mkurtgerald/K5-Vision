from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from k5vision.neural_contracts import (
    ActionProposal,
    AuthorityLevel,
    CapabilityHandshake,
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
