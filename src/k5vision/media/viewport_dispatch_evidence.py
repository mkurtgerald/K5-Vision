"""Source-free physical evidence for bounded renderer-neutral viewport dispatch."""

from __future__ import annotations

import json
import pathlib
import typing

from pydantic import BaseModel, ConfigDict, Field

from k5vision.media.viewport_dispatch import ViewportDispatchState


class ViewportDispatchPhysicalEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: typing.Literal["1"] = "1"
    revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    execution_context: typing.Literal["camera-lab-windows-x64"]
    viewport_count: int = Field(ge=2, le=16)
    delivered_frames: int = Field(ge=2, le=1_000_000)
    delivered_frame_bytes: int = Field(ge=2)
    max_source_span_ms: int = Field(ge=0, le=2_147_483_647)
    final_state: ViewportDispatchState


def write_evidence(path: pathlib.Path, evidence: ViewportDispatchPhysicalEvidence) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(evidence.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
