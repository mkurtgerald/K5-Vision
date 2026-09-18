"""Source-free physical evidence for presentation-ready decode qualification."""

from __future__ import annotations

import json
import pathlib
import typing

from pydantic import BaseModel, ConfigDict, Field

from k5vision.media.gstreamer_playback_decoder import NativePlaybackDecoderState
from k5vision.media.presentation_frame import PixelFormat


class PresentationDecoderPhysicalEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: typing.Literal["1"] = "1"
    revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    execution_context: typing.Literal["camera-lab-windows-x64"]
    valid_rtp_packets: int = Field(ge=1)
    delivered_bytes: int = Field(ge=1)
    decoded_frames: int = Field(ge=1)
    decoded_bytes: int = Field(ge=1)
    width: int = Field(ge=1, le=16_384)
    height: int = Field(ge=1, le=16_384)
    stride_bytes: int = Field(ge=4, le=128 * 1024 * 1024)
    pixel_format: typing.Literal["BGRx"] = PixelFormat.BGRX.value
    final_state: NativePlaybackDecoderState


def write_evidence(path: pathlib.Path, evidence: PresentationDecoderPhysicalEvidence) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(evidence.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
