from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from k5vision.integration_contracts import (
    ContractOffer,
    ContractSelection,
    DeliveryEnvelope,
    EventStatus,
    EvidenceReference,
    NormalizedEvent,
)


def now() -> datetime:
    return datetime.now(UTC)


def test_normalized_event_preserves_forensic_time_order() -> None:
    observed = now()
    ingested = observed + timedelta(milliseconds=10)
    processed = ingested + timedelta(milliseconds=5)
    event = NormalizedEvent(
        event_id="evt-1",
        producer_id="analytics-1",
        source_id="camera-1",
        event_type="object-detected",
        observed_at=observed,
        ingested_at=ingested,
        processed_at=processed,
        status=EventStatus.CANDIDATE,
        confidence=0.8,
        evidence=(
            EvidenceReference(
                evidence_id="evidence-1",
                kind="clip-ref",
                locator="evidence://clip/1",
            ),
        ),
    )
    assert event.schema_version == "1.0"
    assert event.processed_at >= event.ingested_at >= event.observed_at


def test_normalized_event_rejects_impossible_time_order() -> None:
    observed = now()
    with pytest.raises(ValidationError):
        NormalizedEvent(
            event_id="evt-1",
            producer_id="analytics-1",
            source_id="camera-1",
            event_type="object-detected",
            observed_at=observed,
            ingested_at=observed - timedelta(seconds=1),
            processed_at=observed,
            status=EventStatus.CANDIDATE,
        )


def test_delivery_envelope_supports_replay_and_idempotency_identity() -> None:
    delivery = DeliveryEnvelope(
        delivery_id="delivery-1",
        message_id="evt-1",
        emitted_at=now(),
        sequence=7,
        replay=True,
        attempt=2,
    )
    assert delivery.message_id == "evt-1"
    assert delivery.replay is True
    assert delivery.attempt == 2


def test_contract_offer_requires_version() -> None:
    with pytest.raises(ValidationError):
        ContractOffer(service_id="neural-1", offered_versions=())


def test_contract_selection_is_explicit() -> None:
    selection = ContractSelection(
        service_id="neural-1",
        selected_version="1.0",
        selected_capabilities=frozenset({"correlation"}),
        accepted=True,
    )
    assert selection.accepted is True
    assert selection.selected_version == "1.0"


def test_event_unknown_fields_fail_closed() -> None:
    stamp = now()
    with pytest.raises(ValidationError):
        NormalizedEvent(
            event_id="evt-1",
            producer_id="analytics-1",
            source_id="camera-1",
            event_type="object-detected",
            observed_at=stamp,
            ingested_at=stamp,
            processed_at=stamp,
            status=EventStatus.CONFIRMED,
            unknown="nope",
        )
