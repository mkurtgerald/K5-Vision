"""Source-free evidence contract for concrete playback-decoder qualification."""

from __future__ import annotations

import typing

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PlaybackDecoderPhysicalEvidence(BaseModel):
    """Retained counters only; RTP/frame payloads and source identity are excluded."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: typing.Literal["1"] = "1"
    revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    execution_context: typing.Literal["camera-lab-windows-x64"]
    runtime_version: typing.Literal["1.28.7"] = "1.28.7"
    codec: typing.Literal["h264"] = "h264"
    valid_rtp_packets: int = Field(ge=1, le=4096)
    delivered_bytes: int = Field(ge=1)
    decoded_frames: int = Field(ge=1, le=1_000_000)
    decoded_bytes: int = Field(ge=1)
    final_state: typing.Literal["closed"]

    @model_validator(mode="after")
    def validate_counts(self) -> PlaybackDecoderPhysicalEvidence:
        if self.delivered_bytes < self.valid_rtp_packets * 12:
            raise ValueError("delivered byte count is inconsistent with RTP packets")
        if self.decoded_bytes < self.decoded_frames:
            raise ValueError("decoded byte count is inconsistent with decoded frames")
        return self
