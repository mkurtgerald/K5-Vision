"""K5-owned detector-neutral process protocol for optional analytics skills.

The message shape is intentionally compatible with the useful part of the
SharpAI/DeepCamera JSONL detector contract while retaining K5 authority over
validation, transport, source scope, and presentation.

Only transient frame geometry and an opaque frame sequence number cross the
process boundary. Camera credentials, RTSP URIs, user identity, site identity,
evidence paths, and recording state are never part of this contract.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

_MAX_DETECTIONS = 512
_MAX_CATEGORY_LENGTH = 64
_MAX_TRACK_ID_LENGTH = 128


class SkillProtocolError(ValueError):
    """Sanitized analytics skill protocol failure."""


@dataclass(frozen=True, slots=True)
class SkillBox:
    """Normalized detector box compatible with K5's analytics adapter."""

    x_min: float
    y_min: float
    x_max: float
    y_max: float


@dataclass(frozen=True, slots=True)
class SkillTrackedDetection:
    """Detector-neutral observation returned by a K5 skill process."""

    track_id: str
    category: str
    confidence: float
    box: SkillBox


def build_shared_memory_frame_message(
    *,
    frame_id: int,
    shared_memory_name: str,
    byte_length: int,
    width: int,
    height: int,
    stride_bytes: int,
    pixel_format: str,
    source_elapsed_ms: int,
) -> dict[str, object]:
    """Build one transient frame request without persisting camera pixels to disk."""
    if type(frame_id) is not int or frame_id < 0:
        raise SkillProtocolError("frame id is invalid")
    if not isinstance(shared_memory_name, str) or not shared_memory_name:
        raise SkillProtocolError("shared-memory handle is invalid")
    for name, value in (
        ("byte length", byte_length),
        ("width", width),
        ("height", height),
        ("stride", stride_bytes),
    ):
        if type(value) is not int or value <= 0:
            raise SkillProtocolError(f"{name} is invalid")
    if type(source_elapsed_ms) is not int or source_elapsed_ms < 0:
        raise SkillProtocolError("frame timing is invalid")
    if not isinstance(pixel_format, str) or not pixel_format:
        raise SkillProtocolError("pixel format is invalid")

    return {
        "event": "frame",
        "schema_version": "1",
        "frame_id": frame_id,
        "transport": "shared_memory",
        "shared_memory_name": shared_memory_name,
        "byte_length": byte_length,
        "width": width,
        "height": height,
        "stride_bytes": stride_bytes,
        "pixel_format": pixel_format,
        "source_elapsed_ms": source_elapsed_ms,
    }


def decode_event(line: bytes, *, max_response_bytes: int) -> dict[str, Any]:
    """Decode one bounded JSONL event from an untrusted analytics process."""
    if not isinstance(line, bytes):
        raise SkillProtocolError("analytics response is invalid")
    if not line or len(line) > max_response_bytes:
        raise SkillProtocolError("analytics response exceeds configured bound")
    try:
        value = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError) as exc:
        raise SkillProtocolError("analytics response is not valid JSON") from exc
    if not isinstance(value, dict):
        raise SkillProtocolError("analytics response must be a JSON object")
    event = value.get("event")
    if not isinstance(event, str) or not event:
        raise SkillProtocolError("analytics response event is invalid")
    return value


def parse_detection_event(
    value: dict[str, Any],
    *,
    expected_frame_id: int,
    width: int,
    height: int,
    max_detections: int = 128,
) -> tuple[SkillTrackedDetection, ...]:
    """Validate and normalize one detector response to K5's [0,1] box convention."""
    if type(expected_frame_id) is not int or expected_frame_id < 0:
        raise SkillProtocolError("expected frame id is invalid")
    if type(width) is not int or width <= 0 or type(height) is not int or height <= 0:
        raise SkillProtocolError("frame geometry is invalid")
    if type(max_detections) is not int or not 1 <= max_detections <= _MAX_DETECTIONS:
        raise SkillProtocolError("max detections is invalid")
    if value.get("event") != "detections":
        raise SkillProtocolError("analytics response is not a detection event")
    if value.get("frame_id") != expected_frame_id:
        raise SkillProtocolError("analytics response frame id does not match request")

    objects = value.get("objects")
    if not isinstance(objects, list):
        raise SkillProtocolError("analytics detections must be a list")
    if len(objects) > max_detections:
        raise SkillProtocolError("analytics detection count exceeds configured bound")

    detections: list[SkillTrackedDetection] = []
    for index, item in enumerate(objects):
        if not isinstance(item, dict):
            raise SkillProtocolError("analytics detection value is invalid")

        category = item.get("class")
        confidence = item.get("confidence")
        bbox = item.get("bbox")
        if not isinstance(category, str):
            raise SkillProtocolError("analytics detection category is invalid")
        category = category.strip()
        if not category or len(category) > _MAX_CATEGORY_LENGTH:
            raise SkillProtocolError("analytics detection category is invalid")
        if type(confidence) not in (int, float) or not math.isfinite(confidence):
            raise SkillProtocolError("analytics detection confidence is invalid")
        confidence = float(confidence)
        if not 0.0 <= confidence <= 1.0:
            raise SkillProtocolError("analytics detection confidence is invalid")
        if not isinstance(bbox, list) or len(bbox) != 4:
            raise SkillProtocolError("analytics detection box is invalid")

        coordinates: list[float] = []
        for coordinate in bbox:
            if type(coordinate) not in (int, float) or not math.isfinite(coordinate):
                raise SkillProtocolError("analytics detection box is invalid")
            coordinates.append(float(coordinate))
        x_min, y_min, x_max, y_max = coordinates
        if not (0.0 <= x_min < x_max <= width and 0.0 <= y_min < y_max <= height):
            raise SkillProtocolError("analytics detection box is outside frame bounds")

        track_id = item.get("track_id")
        if track_id is None:
            track_id = f"{expected_frame_id}:{index}"
        if (
            not isinstance(track_id, str)
            or not track_id
            or len(track_id) > _MAX_TRACK_ID_LENGTH
        ):
            raise SkillProtocolError("analytics detection track id is invalid")

        detections.append(
            SkillTrackedDetection(
                track_id=track_id,
                category=category,
                confidence=confidence,
                box=SkillBox(
                    x_min=x_min / width,
                    y_min=y_min / height,
                    x_max=x_max / width,
                    y_max=y_max / height,
                ),
            )
        )

    return tuple(detections)
