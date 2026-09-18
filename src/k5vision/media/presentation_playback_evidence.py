"""Source-free physical evidence for bounded presentation playback delivery."""

from __future__ import annotations

import json
import pathlib
import typing

from pydantic import BaseModel, ConfigDict, Field

from k5vision.media.presentation_playback import PresentationPlaybackState


class PresentationPlaybackPhysicalEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: typing.Literal["1"] = "1"
    revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    execution_context: typing.Literal["camera-lab-windows-x64"]
    recorded_packets: int = Field(ge=1)
    recorded_bytes: int = Field(ge=1)
    delivered_frames: int = Field(ge=1)
    delivered_bytes: int = Field(ge=1)
    width: int = Field(ge=1, le=16_384)
    height: int = Field(ge=1, le=16_384)
    stride_bytes: int = Field(ge=4, le=128 * 1024 * 1024)
    source_span_ms: int = Field(ge=0)
    final_state: PresentationPlaybackState


def write_evidence(path: pathlib.Path, evidence: PresentationPlaybackPhysicalEvidence) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(evidence.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
