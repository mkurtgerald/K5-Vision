"""Renderer-ready transient decoded-frame contract.

This module defines the project-owned presentation boundary only. Frame payloads
remain transient memoryviews; retained metadata is geometry/timing information and
never contains camera/source identity, credentials, paths, or media bytes.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

_MAX_DIMENSION = 16_384
_MAX_FRAME_BYTES = 128 * 1024 * 1024
_MAX_SOURCE_ELAPSED_MS = 2_147_483_647


class PixelFormat(enum.StrEnum):
    BGRX = "BGRx"


class PresentationFrameErrorCode(enum.StrEnum):
    INVALID_TIMING = "invalid_timing"
    INVALID_GEOMETRY = "invalid_geometry"
    INVALID_STRIDE = "invalid_stride"
    INVALID_PAYLOAD = "invalid_payload"
    UNSUPPORTED_FORMAT = "unsupported_format"


class PresentationFrameError(ValueError):
    """Sanitized presentation-boundary validation failure."""

    def __init__(self, code: PresentationFrameErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class PresentationFrameMetadata(BaseModel):
    """Payload-free metadata safe for retained observability."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "1"
    width: int = Field(ge=1, le=_MAX_DIMENSION)
    height: int = Field(ge=1, le=_MAX_DIMENSION)
    stride_bytes: int = Field(ge=4, le=_MAX_FRAME_BYTES)
    pixel_format: PixelFormat
    source_elapsed_ms: int = Field(ge=0, le=_MAX_SOURCE_ELAPSED_MS)
    byte_length: int = Field(ge=1, le=_MAX_FRAME_BYTES)


@dataclass(frozen=True, slots=True)
class PresentationVideoFrame:
    """Validated transient frame crossing the future renderer boundary."""

    payload: memoryview
    width: int
    height: int
    stride_bytes: int
    pixel_format: PixelFormat
    source_elapsed_ms: int

    def __post_init__(self) -> None:
        if not isinstance(self.payload, memoryview):
            raise PresentationFrameError(
                PresentationFrameErrorCode.INVALID_PAYLOAD,
                "presentation frame payload must be a memory view",
            )
        if not isinstance(self.pixel_format, PixelFormat):
            raise PresentationFrameError(
                PresentationFrameErrorCode.UNSUPPORTED_FORMAT,
                "presentation frame pixel format is unsupported",
            )
        if not 0 <= self.source_elapsed_ms <= _MAX_SOURCE_ELAPSED_MS:
            raise PresentationFrameError(
                PresentationFrameErrorCode.INVALID_TIMING,
                "presentation frame timing is invalid",
            )
        if not 1 <= self.width <= _MAX_DIMENSION or not 1 <= self.height <= _MAX_DIMENSION:
            raise PresentationFrameError(
                PresentationFrameErrorCode.INVALID_GEOMETRY,
                "presentation frame geometry is invalid",
            )

        bytes_per_pixel = 4
        minimum_stride = self.width * bytes_per_pixel
        if not minimum_stride <= self.stride_bytes <= _MAX_FRAME_BYTES:
            raise PresentationFrameError(
                PresentationFrameErrorCode.INVALID_STRIDE,
                "presentation frame stride is invalid",
            )

        expected_bytes = self.stride_bytes * self.height
        if not 1 <= expected_bytes <= _MAX_FRAME_BYTES or len(self.payload) != expected_bytes:
            raise PresentationFrameError(
                PresentationFrameErrorCode.INVALID_PAYLOAD,
                "presentation frame payload does not match bounded geometry",
            )

    @property
    def metadata(self) -> PresentationFrameMetadata:
        return PresentationFrameMetadata(
            width=self.width,
            height=self.height,
            stride_bytes=self.stride_bytes,
            pixel_format=self.pixel_format,
            source_elapsed_ms=self.source_elapsed_ms,
            byte_length=len(self.payload),
        )
