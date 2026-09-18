"""Source-free exact-revision evidence for the Windows presentation surface."""

from __future__ import annotations

import json
import pathlib
import typing

from pydantic import BaseModel, ConfigDict, Field

from k5vision.media.windows_presentation_surface import WindowsPresentationSurfaceState


class WindowsPresentationSurfacePhysicalEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: typing.Literal["1"] = "1"
    revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    execution_context: typing.Literal["camera-lab-windows-x64"]
    width: int = Field(ge=1, le=16_384)
    height: int = Field(ge=1, le=16_384)
    native_stride_bytes: int = Field(ge=4, le=128 * 1024 * 1024)
    presented_frames: int = Field(ge=1, le=1_000_000)
    presented_frame_bytes: int = Field(ge=1, le=64 * 1024 * 1024 * 1024)
    surface_replacements: int = Field(ge=0, le=1024)
    max_source_span_ms: int = Field(ge=0, le=2_147_483_647)
    final_state: WindowsPresentationSurfaceState


def write_evidence(
    path: pathlib.Path,
    evidence: WindowsPresentationSurfacePhysicalEvidence,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(evidence.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
