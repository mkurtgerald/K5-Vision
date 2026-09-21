"""Canonical K5 integration contracts shared by analytics and reasoning subsystems."""

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class EventStatus(StrEnum):
    CANDIDATE = "candidate"
    CONFIRMED = "confirmed"
    CLEARED = "cleared"
    ABSTAIN = "abstain"


class IntegrationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    @field_validator("*", mode="before")
    @classmethod
    def require_aware_datetimes(cls, value: Any) -> Any:
        if isinstance(value, datetime) and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("timezone-aware timestamp required")
        return value


class EvidenceReference(IntegrationModel):
    evidence_id: str = Field(min_length=1, max_length=128)
    kind: str = Field(min_length=1, max_length=64)
    locator: str = Field(min_length=1, max_length=1024)


class NormalizedEvent(IntegrationModel):
    schema_version: Literal["1.0"] = "1.0"
    event_id: str = Field(min_length=1, max_length=128)
    producer_id: str = Field(min_length=1, max_length=128)
    source_id: str = Field(min_length=1, max_length=128)
    event_type: str = Field(min_length=1, max_length=128)
    observed_at: datetime
    ingested_at: datetime
    processed_at: datetime
    status: EventStatus
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    object_refs: tuple[str, ...] = ()
    zone_refs: tuple[str, ...] = ()
    evidence: tuple[EvidenceReference, ...] = ()
    parent_event_ids: tuple[str, ...] = ()
    extensions: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_time_order(self) -> "NormalizedEvent":
        if self.ingested_at < self.observed_at:
            raise ValueError("ingestion cannot precede observation")
        if self.processed_at < self.ingested_at:
            raise ValueError("processing cannot precede ingestion")
        if len(self.parent_event_ids) > 64:
            raise ValueError("too many parent events")
        return self


class DeliveryEnvelope(IntegrationModel):
    delivery_id: str = Field(min_length=1, max_length=128)
    message_id: str = Field(min_length=1, max_length=128)
    emitted_at: datetime
    sequence: int = Field(ge=0)
    replay: bool = False
    attempt: int = Field(default=1, ge=1, le=32)


class ContractOffer(IntegrationModel):
    service_id: str = Field(min_length=1, max_length=128)
    offered_versions: tuple[str, ...]
    capabilities: frozenset[str] = frozenset()

    @model_validator(mode="after")
    def require_versions(self) -> "ContractOffer":
        if not self.offered_versions:
            raise ValueError("at least one contract version is required")
        return self


class ContractSelection(IntegrationModel):
    service_id: str = Field(min_length=1, max_length=128)
    selected_version: str = Field(min_length=1, max_length=32)
    selected_capabilities: frozenset[str] = frozenset()
    accepted: bool
    reason: str | None = Field(default=None, max_length=512)
