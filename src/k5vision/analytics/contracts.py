"""Versioned contracts shared by K5 Vision and external analytics runtimes."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

ANALYTICS_API_VERSION = "k5.analytics/v1"


class AnalyticState(StrEnum):
    """Lifecycle state for an analytic integration."""

    STAGED = "staged"
    VALIDATED = "validated"
    ENABLED = "enabled"
    DEGRADED = "degraded"
    DISABLED = "disabled"


class BoundingBox(BaseModel):
    """Normalized [0, 1] image-space bounding box."""

    model_config = ConfigDict(extra="forbid")

    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)
    width: float = Field(gt=0.0, le=1.0)
    height: float = Field(gt=0.0, le=1.0)

    @field_validator("width")
    @classmethod
    def _validate_width(cls, value: float, info: Any) -> float:
        x = info.data.get("x")
        if x is not None and x + value > 1.0:
            raise ValueError("x + width must be <= 1")
        return value

    @field_validator("height")
    @classmethod
    def _validate_height(cls, value: float, info: Any) -> float:
        y = info.data.get("y")
        if y is not None and y + value > 1.0:
            raise ValueError("y + height must be <= 1")
        return value


class EvidenceReference(BaseModel):
    """Reference to K5-managed evidence; analytics do not own video retention."""

    model_config = ConfigDict(extra="forbid")

    kind: str
    uri: str
    sha256: str | None = None


class AnalyticManifest(BaseModel):
    """Installation metadata supplied by an Analytics Lab module."""

    model_config = ConfigDict(extra="forbid")

    analytic_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{1,63}$")
    name: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=64)
    api_version: str = ANALYTICS_API_VERSION
    event_types: tuple[str, ...] = Field(min_length=1)
    description: str | None = None
    vendor: str = "K5 Vision"
    runtime: str = "external"
    entrypoint: str | None = None
    model_sha256: str | None = None
    source_repository: str | None = None
    source_revision: str | None = None
    license_spdx: str | None = None
    requires_gpu: bool = False
    minimum_vram_mb: int | None = Field(default=None, ge=0)
    default_confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class AnalyticEvent(BaseModel):
    """Canonical event emitted into K5's event/rules/evidence pipeline."""

    model_config = ConfigDict(extra="allow")

    event_id: str = Field(min_length=1, max_length=128)
    analytic_id: str = Field(min_length=1, max_length=64)
    analytic_version: str = Field(min_length=1, max_length=64)
    event_type: str = Field(min_length=1, max_length=128)
    occurred_at: datetime
    source_id: str = Field(min_length=1, max_length=128)
    confidence: float = Field(ge=0.0, le=1.0)
    bbox: BoundingBox | None = None
    track_id: str | None = None
    zone_ids: tuple[str, ...] = ()
    correlation_id: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)
    evidence: tuple[EvidenceReference, ...] = ()


class AnalyticHealth(BaseModel):
    """Health snapshot for an isolated analytics runtime."""

    model_config = ConfigDict(extra="forbid")

    analytic_id: str
    state: AnalyticState
    checked_at: datetime
    message: str | None = None
    latency_ms: float | None = Field(default=None, ge=0.0)
    frames_per_second: float | None = Field(default=None, ge=0.0)
    dropped_frames: int = Field(default=0, ge=0)
