from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from k5vision.media.recording_descriptor import (
    RecordingDescriptorError,
    RecordingDescriptorErrorCode,
    RecordingStreamDescriptor,
    VideoCodec,
    parse_recording_descriptor,
)

_RECORDING_ID = UUID("11111111-1111-4111-8111-111111111111")
_SOURCE_ID = UUID("22222222-2222-4222-8222-222222222222")
_START = datetime(2026, 9, 17, 14, 0, 0, tzinfo=UTC)


def _values(**changes):
    values = {
        "recording_id": _RECORDING_ID,
        "source_id": _SOURCE_ID,
        "codec": VideoCodec.H264,
        "payload_type": 96,
        "clock_rate_hz": 90_000,
        "started_at_utc": _START,
        "ended_at_utc": _START + timedelta(seconds=1),
        "duration_ms": 1000,
        "rtp_timestamp_origin": 123_456,
        "packet_count": 2,
        "payload_bytes": 24,
        "file_bytes": 48,
    }
    values.update(changes)
    return values


def _descriptor(**changes) -> RecordingStreamDescriptor:
    return RecordingStreamDescriptor(**_values(**changes))


def test_descriptor_serialization_is_deterministic_and_round_trips() -> None:
    descriptor = _descriptor()
    first = descriptor.to_json_bytes()
    second = descriptor.to_json_bytes()

    assert first == second
    assert first == json.dumps(
        descriptor.model_dump(mode="json"),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()

    parsed = parse_recording_descriptor(first)
    assert parsed == descriptor
    assert parsed.recording_id == _RECORDING_ID
    assert parsed.source_id == _SOURCE_ID


def test_descriptor_is_immutable() -> None:
    descriptor = _descriptor()
    with pytest.raises(ValidationError):
        descriptor.packet_count = 3


@pytest.mark.parametrize(
    ("codec", "payload_type"),
    [
        (VideoCodec.H264, 96),
        (VideoCodec.H264, 127),
        (VideoCodec.H265, 96),
        (VideoCodec.H265, 127),
        (VideoCodec.JPEG, 26),
        (VideoCodec.JPEG, 96),
        (VideoCodec.JPEG, 127),
    ],
)
def test_supported_codec_payload_combinations(codec: VideoCodec, payload_type: int) -> None:
    descriptor = _descriptor(codec=codec, payload_type=payload_type)
    assert descriptor.codec == codec
    assert descriptor.payload_type == payload_type


@pytest.mark.parametrize(
    ("codec", "payload_type"),
    [
        (VideoCodec.H264, 26),
        (VideoCodec.H264, 95),
        (VideoCodec.H265, 0),
        (VideoCodec.H265, 95),
        (VideoCodec.JPEG, 25),
        (VideoCodec.JPEG, 27),
        (VideoCodec.JPEG, 95),
    ],
)
def test_unsupported_codec_payload_combinations_fail_closed(
    codec: VideoCodec,
    payload_type: int,
) -> None:
    with pytest.raises(ValidationError):
        _descriptor(codec=codec, payload_type=payload_type)


def test_video_clock_rate_is_explicit_and_fixed() -> None:
    assert _descriptor().clock_rate_hz == 90_000
    with pytest.raises(ValidationError):
        _descriptor(clock_rate_hz=89_999)
    with pytest.raises(ValidationError):
        _descriptor(clock_rate_hz=90_001)


def test_timestamp_contract_requires_utc_order_and_millisecond_precision() -> None:
    with pytest.raises(ValidationError):
        _descriptor(started_at_utc=_START.replace(tzinfo=None))

    with pytest.raises(ValidationError):
        _descriptor(
            started_at_utc=_START.astimezone(timezone(timedelta(hours=-4))),
            ended_at_utc=(_START + timedelta(seconds=1)).astimezone(
                timezone(timedelta(hours=-4))
            ),
        )

    with pytest.raises(ValidationError):
        _descriptor(started_at_utc=_START + timedelta(microseconds=1))

    with pytest.raises(ValidationError):
        _descriptor(
            ended_at_utc=_START - timedelta(milliseconds=1),
            duration_ms=0,
        )

    with pytest.raises(ValidationError):
        _descriptor(duration_ms=999)


def test_zero_packet_descriptor_is_consistent() -> None:
    descriptor = _descriptor(
        ended_at_utc=_START,
        duration_ms=0,
        packet_count=0,
        payload_bytes=0,
        file_bytes=8,
    )
    assert descriptor.packet_count == 0
    assert descriptor.file_bytes == 8

    with pytest.raises(ValidationError):
        _descriptor(
            ended_at_utc=_START,
            duration_ms=0,
            packet_count=0,
            payload_bytes=1,
            file_bytes=9,
        )

    with pytest.raises(ValidationError):
        _descriptor(packet_count=0, payload_bytes=0, file_bytes=8)


def test_framing_counts_are_cross_checked() -> None:
    with pytest.raises(ValidationError):
        _descriptor(file_bytes=49)

    with pytest.raises(ValidationError):
        _descriptor(packet_count=2, payload_bytes=23, file_bytes=47)

    descriptor = _descriptor(packet_count=1, payload_bytes=12, file_bytes=28)
    assert descriptor.file_bytes == 8 + 8 + 12


def test_count_and_timestamp_boundaries_are_bounded() -> None:
    with pytest.raises(ValidationError):
        _descriptor(packet_count=1_000_001)
    with pytest.raises(ValidationError):
        _descriptor(payload_bytes=8 * 1024 * 1024 * 1024 + 1)
    with pytest.raises(ValidationError):
        _descriptor(rtp_timestamp_origin=0x1_0000_0000)
    with pytest.raises(ValidationError):
        _descriptor(duration_ms=7 * 24 * 60 * 60 * 1000 + 1)


def test_unknown_schema_version_is_rejected_without_model_coercion() -> None:
    raw = json.loads(_descriptor().to_json_bytes())
    raw["schema_version"] = "2"
    with pytest.raises(RecordingDescriptorError) as exc:
        parse_recording_descriptor(json.dumps(raw))
    assert exc.value.code == RecordingDescriptorErrorCode.UNSUPPORTED_VERSION


def test_malformed_and_non_object_payloads_are_rejected() -> None:
    for payload in (b"\xff", b"{", b"[]", b"null", b'"value"'):
        with pytest.raises(RecordingDescriptorError) as exc:
            parse_recording_descriptor(payload)
        assert exc.value.code == RecordingDescriptorErrorCode.MALFORMED


def test_descriptor_size_limit_is_enforced_before_parsing() -> None:
    with pytest.raises(RecordingDescriptorError) as exc:
        parse_recording_descriptor(b"{" + (b"x" * 4096) + b"}")
    assert exc.value.code == RecordingDescriptorErrorCode.TOO_LARGE


def test_unknown_codec_extra_fields_and_invalid_ids_fail_safely() -> None:
    raw = json.loads(_descriptor().to_json_bytes())

    for key, value in (
        ("codec", "SECRET_CODEC_MARKER"),
        ("recording_id", "SECRET_ID_MARKER"),
        ("source_id", "rtsp://SECRET_SOURCE_MARKER"),
    ):
        candidate = dict(raw)
        candidate[key] = value
        with pytest.raises(RecordingDescriptorError) as exc:
            parse_recording_descriptor(json.dumps(candidate))
        assert exc.value.code == RecordingDescriptorErrorCode.INVALID_DESCRIPTOR
        assert "SECRET" not in str(exc.value)
        assert "rtsp://" not in str(exc.value).casefold()

    candidate = dict(raw)
    candidate["source_uri"] = "rtsp://SECRET_URI_MARKER"
    with pytest.raises(RecordingDescriptorError) as exc:
        parse_recording_descriptor(json.dumps(candidate))
    assert exc.value.code == RecordingDescriptorErrorCode.INVALID_DESCRIPTOR
    assert "SECRET_URI_MARKER" not in str(exc.value)


def test_serialized_contract_has_no_source_detail_escape_hatches() -> None:
    payload = _descriptor().to_json_bytes().decode()
    lowered = payload.casefold()
    for forbidden in (
        "rtsp://",
        "credential",
        "password",
        "username",
        "address",
        "hostname",
        "path",
        "runner",
        "frame",
        "clip",
        "source_uri",
        "fmtp",
        "sps",
        "pps",
    ):
        assert forbidden not in lowered


def test_parser_accepts_string_payload_and_canonicalizes_uuid_output() -> None:
    raw = json.loads(_descriptor().to_json_bytes())
    raw["recording_id"] = str(_RECORDING_ID).upper()
    parsed = parse_recording_descriptor(json.dumps(raw))
    assert parsed.recording_id == _RECORDING_ID
    assert str(parsed.recording_id) == str(_RECORDING_ID)
