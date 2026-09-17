"""Source-free recording metadata required before deterministic playback.

The descriptor intentionally permits only bounded typed metadata. It has no URI,
network-address, credential, filesystem-path, arbitrary-string, or media-payload
field, so serialization cannot accidentally become a source-detail side channel.
"""

from __future__ import annotations

import enum
import json
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

_FORMAT_MAGIC_BYTES = 8
_RECORD_OVERHEAD_BYTES = 8
_MAX_PACKETS = 1_000_000
_MAX_PAYLOAD_BYTES = 8 * 1024 * 1024 * 1024
_MAX_FILE_BYTES = _FORMAT_MAGIC_BYTES + (_MAX_PACKETS * _RECORD_OVERHEAD_BYTES) + _MAX_PAYLOAD_BYTES
_MAX_DESCRIPTOR_BYTES = 4096
_MAX_DURATION_MS = 7 * 24 * 60 * 60 * 1000


class VideoCodec(enum.StrEnum):
    H264 = "h264"
    H265 = "h265"
    JPEG = "jpeg"


class RecordingDescriptorErrorCode(enum.StrEnum):
    TOO_LARGE = "too_large"
    MALFORMED = "malformed"
    UNSUPPORTED_VERSION = "unsupported_version"
    INVALID_DESCRIPTOR = "invalid_descriptor"


class RecordingDescriptorError(ValueError):
    """Sanitized descriptor failure that never echoes untrusted content."""

    def __init__(self, code: RecordingDescriptorErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class RecordingStreamDescriptor(BaseModel):
    """Immutable metadata contract linking a finalized recording to RTP semantics."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    recording_format_version: Literal["1"] = "1"
    recording_id: UUID
    source_id: UUID
    media_kind: Literal["video"] = "video"
    codec: VideoCodec
    payload_type: int = Field(ge=0, le=127)
    clock_rate_hz: int = Field(ge=1, le=192_000)
    started_at_utc: datetime
    ended_at_utc: datetime
    duration_ms: int = Field(ge=0, le=_MAX_DURATION_MS)
    rtp_timestamp_origin: int = Field(ge=0, le=0xFFFFFFFF)
    packet_count: int = Field(ge=0, le=_MAX_PACKETS)
    payload_bytes: int = Field(ge=0, le=_MAX_PAYLOAD_BYTES)
    file_bytes: int = Field(ge=_FORMAT_MAGIC_BYTES, le=_MAX_FILE_BYTES)
    integrity: Literal["crc32-per-record"] = "crc32-per-record"

    @model_validator(mode="after")
    def validate_contract(self) -> RecordingStreamDescriptor:
        for timestamp in (self.started_at_utc, self.ended_at_utc):
            if timestamp.tzinfo is None or timestamp.utcoffset() != timedelta(0):
                raise ValueError("recording timestamps must be UTC")
            if timestamp.microsecond % 1000:
                raise ValueError("recording timestamps must use millisecond precision")

        if self.ended_at_utc < self.started_at_utc:
            raise ValueError("recording end precedes start")

        expected_duration_ms = int(
            (self.ended_at_utc - self.started_at_utc).total_seconds() * 1000
        )
        if self.duration_ms != expected_duration_ms:
            raise ValueError("recording duration does not match timestamps")

        if self.codec in {VideoCodec.H264, VideoCodec.H265}:
            if not 96 <= self.payload_type <= 127:
                raise ValueError("dynamic video codec requires dynamic RTP payload type")
        elif self.codec == VideoCodec.JPEG and not (
            self.payload_type == 26 or 96 <= self.payload_type <= 127
        ):
            raise ValueError("JPEG RTP payload type is unsupported")

        if self.clock_rate_hz != 90_000:
            raise ValueError("supported video RTP clock rate is 90000 Hz")

        expected_file_bytes = (
            _FORMAT_MAGIC_BYTES
            + (self.packet_count * _RECORD_OVERHEAD_BYTES)
            + self.payload_bytes
        )
        if self.file_bytes != expected_file_bytes:
            raise ValueError("recording byte counts do not match format framing")

        if self.packet_count == 0:
            if self.payload_bytes != 0 or self.duration_ms != 0:
                raise ValueError("empty recording counters are inconsistent")
        elif self.payload_bytes < self.packet_count * 12:
            raise ValueError("recording payload bytes are below minimum RTP size")

        return self

    def to_json_bytes(self) -> bytes:
        """Serialize canonically without adding free-form or source-detail fields."""
        payload = self.model_dump(mode="json")
        return json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")


def parse_recording_descriptor(payload: bytes | str) -> RecordingStreamDescriptor:
    """Parse an untrusted descriptor with bounded input and sanitized failures."""
    if isinstance(payload, str):
        encoded = payload.encode("utf-8")
    else:
        encoded = bytes(payload)

    if len(encoded) > _MAX_DESCRIPTOR_BYTES:
        raise RecordingDescriptorError(
            RecordingDescriptorErrorCode.TOO_LARGE,
            "recording descriptor exceeds size limit",
        )

    try:
        decoded = encoded.decode("utf-8")
        raw = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RecordingDescriptorError(
            RecordingDescriptorErrorCode.MALFORMED,
            "recording descriptor is malformed",
        ) from None

    if not isinstance(raw, dict):
        raise RecordingDescriptorError(
            RecordingDescriptorErrorCode.MALFORMED,
            "recording descriptor must be a JSON object",
        )

    if raw.get("schema_version") != "1":
        raise RecordingDescriptorError(
            RecordingDescriptorErrorCode.UNSUPPORTED_VERSION,
            "recording descriptor schema version is unsupported",
        )

    try:
        return RecordingStreamDescriptor.model_validate(raw)
    except ValidationError:
        raise RecordingDescriptorError(
            RecordingDescriptorErrorCode.INVALID_DESCRIPTOR,
            "recording descriptor failed validation",
        ) from None
