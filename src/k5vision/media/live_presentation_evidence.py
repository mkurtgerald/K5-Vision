"""Source-free physical evidence for bounded live presentation delivery."""

from __future__ import annotations

import json
import pathlib
import typing

from pydantic import BaseModel, ConfigDict, Field

from k5vision.media.live_presentation import LivePresentationState


class LivePresentationPhysicalEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: typing.Literal["1"] = "1"
    revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    execution_context: typing.Literal["camera-lab-windows-x64"]
    accepted_packets: int = Field(ge=1, le=4096)
    rtp_delivered_bytes: int = Field(ge=1)
    delivered_frames: int = Field(ge=1)
    delivered_frame_bytes: int = Field(ge=1)
    width: int = Field(ge=1, le=16_384)
    height: int = Field(ge=1, le=16_384)
    stride_bytes: int = Field(ge=4, le=128 * 1024 * 1024)
    source_span_ms: int = Field(ge=0, le=600_000)
    final_state: LivePresentationState


def write_evidence(path: pathlib.Path, evidence: LivePresentationPhysicalEvidence) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(evidence.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
