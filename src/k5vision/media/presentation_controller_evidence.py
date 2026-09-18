"""Source-free physical evidence for the bounded presentation controller."""

from __future__ import annotations

import json
import pathlib
import typing

from pydantic import BaseModel, ConfigDict, Field

from k5vision.media.presentation_controller import PresentationControllerState


class PresentationControllerPhysicalEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: typing.Literal["1"] = "1"
    revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    execution_context: typing.Literal["camera-lab-windows-x64"]
    stream_count: int = Field(ge=2, le=16)
    live_streams: int = Field(ge=1, le=15)
    playback_streams: int = Field(ge=1, le=15)
    viewport_count: int = Field(ge=2, le=16)
    completed_streams: int = Field(ge=2, le=16)
    delivered_frames: int = Field(ge=2, le=1_000_000)
    delivered_frame_bytes: int = Field(ge=2)
    max_source_span_ms: int = Field(ge=0, le=2_147_483_647)
    final_state: PresentationControllerState


def write_evidence(path: pathlib.Path, evidence: PresentationControllerPhysicalEvidence) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(evidence.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
