"""Bounded transient object-detection overlay rendering for operator video.

The product owns this renderer boundary. Analytics producers may supply normalized
observations, but they never receive presentation authority or camera credentials.
Only aggregate counters are retained; boxes, labels, frame payloads, source identity,
and private topology remain transient execution data.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable

from pydantic import BaseModel, ConfigDict, Field

from k5vision.media.presentation_frame import PixelFormat, PresentationVideoFrame

_MAX_BOXES = 512
_MAX_BORDER_WIDTH = 8
_MAX_PIXEL_WRITES = 16_384 * 16_384


class DetectionOverlayError(ValueError):
    """Sanitized validation failure for the transient overlay boundary."""


@dataclass(frozen=True, slots=True)
class DetectionOverlayObservation:
    """One detector-neutral normalized observation for transient presentation."""

    category: str
    confidence: float
    x_min: float
    y_min: float
    x_max: float
    y_max: float

    def __post_init__(self) -> None:
        if not isinstance(self.category, str):
            raise DetectionOverlayError("overlay category must be a string")
        category = self.category.strip()
        if not category or len(category) > 64:
            raise DetectionOverlayError("overlay category must contain 1-64 characters")
        object.__setattr__(self, "category", category)

        for name in ("confidence", "x_min", "y_min", "x_max", "y_max"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value):
                raise DetectionOverlayError(f"overlay {name} must be finite")
            value = float(value)
            if not 0.0 <= value <= 1.0:
                raise DetectionOverlayError(f"overlay {name} must be within [0, 1]")
            object.__setattr__(self, name, value)

        if self.x_max <= self.x_min or self.y_max <= self.y_min:
            raise DetectionOverlayError("overlay box must have positive normalized area")


class DetectionOverlaySnapshot(BaseModel):
    """Payload/box/label/source-free aggregate overlay observability."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "1"
    input_observations: int = Field(ge=0, le=_MAX_BOXES)
    rendered_boxes: int = Field(ge=0, le=_MAX_BOXES)
    confidence_filtered: int = Field(ge=0, le=_MAX_BOXES)
    pixel_writes: int = Field(ge=0, le=_MAX_PIXEL_WRITES)


@dataclass(frozen=True, slots=True)
class DetectionOverlayResult:
    """One transient rendered frame plus aggregate-only retained metadata."""

    frame: PresentationVideoFrame
    snapshot: DetectionOverlaySnapshot


class BoundedDetectionOverlayRenderer:
    """Draw normalized detector boxes onto a transient BGRX presentation frame."""

    def __init__(
        self,
        *,
        max_boxes: int = 128,
        border_width: int = 2,
        minimum_confidence: float = 0.0,
    ) -> None:
        if type(max_boxes) is not int or not 1 <= max_boxes <= _MAX_BOXES:
            raise ValueError(f"max_boxes must be between 1 and {_MAX_BOXES}")
        if type(border_width) is not int or not 1 <= border_width <= _MAX_BORDER_WIDTH:
            raise ValueError(f"border_width must be between 1 and {_MAX_BORDER_WIDTH}")
        if (
            type(minimum_confidence) not in (int, float)
            or not math.isfinite(minimum_confidence)
            or not 0.0 <= minimum_confidence <= 1.0
        ):
            raise ValueError("minimum_confidence must be finite and within [0, 1]")
        self._max_boxes = max_boxes
        self._border_width = border_width
        self._minimum_confidence = float(minimum_confidence)

    @staticmethod
    def _pixel_bounds(
        observation: DetectionOverlayObservation,
        width: int,
        height: int,
    ) -> tuple[int, int, int, int]:
        left = min(width - 1, int(observation.x_min * width))
        top = min(height - 1, int(observation.y_min * height))
        right = min(width - 1, max(left, math.ceil(observation.x_max * width) - 1))
        bottom = min(height - 1, max(top, math.ceil(observation.y_max * height) - 1))
        return left, top, right, bottom

    @staticmethod
    def _write_pixel(buffer: bytearray, stride_bytes: int, x: int, y: int) -> None:
        offset = y * stride_bytes + x * 4
        buffer[offset : offset + 4] = b"\x00\xff\x00\x00"

    def render(
        self,
        frame: PresentationVideoFrame,
        observations: Iterable[DetectionOverlayObservation],
    ) -> DetectionOverlayResult:
        if not isinstance(frame, PresentationVideoFrame):
            raise DetectionOverlayError("overlay frame is invalid")
        if frame.pixel_format is not PixelFormat.BGRX:
            raise DetectionOverlayError("overlay frame pixel format is unsupported")
        try:
            selected = tuple(observations)
        except TypeError as exc:
            raise DetectionOverlayError("overlay observations must be iterable") from exc
        if len(selected) > self._max_boxes:
            raise DetectionOverlayError("overlay observation count exceeds configured bound")
        if any(not isinstance(item, DetectionOverlayObservation) for item in selected):
            raise DetectionOverlayError("overlay observations contain an unsupported value")

        active = tuple(item for item in selected if item.confidence >= self._minimum_confidence)
        if not active:
            return DetectionOverlayResult(
                frame=frame,
                snapshot=DetectionOverlaySnapshot(
                    input_observations=len(selected),
                    rendered_boxes=0,
                    confidence_filtered=len(selected),
                    pixel_writes=0,
                ),
            )

        rendered = bytearray(frame.payload)
        pixel_writes = 0
        for observation in active:
            left, top, right, bottom = self._pixel_bounds(
                observation,
                frame.width,
                frame.height,
            )
            for inset in range(self._border_width):
                x0 = left + inset
                x1 = right - inset
                y0 = top + inset
                y1 = bottom - inset
                if x0 > x1 or y0 > y1:
                    break
                for x in range(x0, x1 + 1):
                    self._write_pixel(rendered, frame.stride_bytes, x, y0)
                    pixel_writes += 1
                    if y1 != y0:
                        self._write_pixel(rendered, frame.stride_bytes, x, y1)
                        pixel_writes += 1
                for y in range(y0 + 1, y1):
                    self._write_pixel(rendered, frame.stride_bytes, x0, y)
                    pixel_writes += 1
                    if x1 != x0:
                        self._write_pixel(rendered, frame.stride_bytes, x1, y)
                        pixel_writes += 1

        output = PresentationVideoFrame(
            payload=memoryview(rendered),
            width=frame.width,
            height=frame.height,
            stride_bytes=frame.stride_bytes,
            pixel_format=frame.pixel_format,
            source_elapsed_ms=frame.source_elapsed_ms,
        )
        return DetectionOverlayResult(
            frame=output,
            snapshot=DetectionOverlaySnapshot(
                input_observations=len(selected),
                rendered_boxes=len(active),
                confidence_filtered=len(selected) - len(active),
                pixel_writes=pixel_writes,
            ),
        )
