"""Source-free physical qualification evidence for the Stage-06 live-view boundary."""

from __future__ import annotations

from ipaddress import ip_address
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from k5vision.media.live_view import LiveViewState


class LiveViewPhysicalEvidence(BaseModel):
    """Retained Stage-06 lifecycle evidence with no source or media material."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    revision: str = Field(pattern=r"^(?:[0-9a-f]{40}|local)$")
    execution_context: str = Field(
        default="simulated",
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
    )
    runtime: Literal["GStreamer 1.28.7"] = "GStreamer 1.28.7"
    transport: Literal["udp"] = "udp"
    max_consumers: int = Field(ge=2, le=64)
    first_generation: int = Field(ge=1)
    shared_generation: int = Field(ge=1)
    peak_consumers: int = Field(ge=2, le=64)
    consumers_after_partial_release: int = Field(ge=1, le=63)
    state_after_final_release: LiveViewState
    reentry_generation: int = Field(ge=1)
    final_state: LiveViewState

    @field_validator("execution_context")
    @classmethod
    def reject_network_identity(cls, value: str) -> str:
        """Keep retained context generic by rejecting literal IP addresses."""
        try:
            ip_address(value)
        except ValueError:
            return value
        raise ValueError("execution context must not contain a literal network address")

    @model_validator(mode="after")
    def validate_lifecycle(self) -> Self:
        if self.first_generation != self.shared_generation:
            raise ValueError("shared consumers must use one underlying session generation")
        if self.reentry_generation <= self.first_generation:
            raise ValueError("re-entry must advance the underlying session generation")
        if self.peak_consumers > self.max_consumers:
            raise ValueError("peak consumers cannot exceed the configured bound")
        if self.consumers_after_partial_release >= self.peak_consumers:
            raise ValueError("partial release must reduce the active consumer count")
        if self.state_after_final_release != LiveViewState.IDLE:
            raise ValueError("final consumer release must return the boundary to idle")
        if self.final_state != LiveViewState.CLOSED:
            raise ValueError("qualification must finish with deterministic cleanup")
        return self

    @property
    def accepted(self) -> bool:
        return True
