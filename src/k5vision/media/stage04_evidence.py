"""Source-free Stage-04 physical qualification evidence."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from k5vision.media.session import MediaSessionState


class Stage04PhysicalEvidence(BaseModel):
    """Retained proof for the selected UDP runtime and K5 lifecycle boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    revision: str = Field(pattern=r"^(?:[0-9a-f]{40}|local)$")
    runtime: Literal["GStreamer 1.28.7"] = "GStreamer 1.28.7"
    transport: Literal["udp"] = "udp"
    receive_probe_passed: bool
    lifecycle_states: tuple[MediaSessionState, ...] = Field(min_length=4, max_length=16)
    generation_count: int = Field(ge=2, le=32)
    final_state: MediaSessionState

    @model_validator(mode="after")
    def validate_success(self) -> Self:
        if not self.receive_probe_passed:
            raise ValueError("physical receive probe must pass")
        required = {
            MediaSessionState.RUNNING,
            MediaSessionState.STOPPED,
            MediaSessionState.CLOSED,
        }
        if not required.issubset(set(self.lifecycle_states)):
            raise ValueError("physical lifecycle evidence is incomplete")
        if self.final_state != MediaSessionState.CLOSED:
            raise ValueError("physical lifecycle must close cleanly")
        return self
