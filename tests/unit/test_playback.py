from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from k5vision.media.framed_recording import FramedAtomicRecordingSink, FramedRecordingErrorCode
from k5vision.media.playback import (
    BoundedRecordingPlayback,
    PlaybackError,
    PlaybackErrorCode,
    PlaybackState,
)
from k5vision.media.recording_descriptor import RecordingStreamDescriptor, VideoCodec

_RECORDING_ID = UUID("33333333-3333-4333-8333-333333333333")
_SOURCE_ID = UUID("44444444-4444-4444-8444-444444444444")
_START = datetime(2026, 9, 17, 15, 0, 0, tzinfo=UTC)


def _rtp(
    payload: bytes = b"x",
    *,
    sequence: int = 1,
    timestamp: int = 123_456,
    payload_type: int = 96,
) -> bytes:
    return (
        bytes(
            [
                0x80,
                payload_type & 0x7F,
                (sequence >> 8) & 0xFF,
                sequence & 0xFF,
                (timestamp >> 24) & 0xFF,
                (timestamp >> 16) & 0xFF,
                (timestamp >> 8) & 0xFF,
                timestamp & 0xFF,
                0,
                0,
                0,
                1,
            ]
        )
        + payload
    )


def _descriptor(packets: list[bytes], **changes) -> RecordingStreamDescriptor:
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
        "packet_count": len(packets),
        "payload_bytes": sum(len(packet) for packet in packets),
        "file_bytes": 8 + sum(8 + len(packet) for packet in packets),
    }
    values.update(changes)
    return RecordingStreamDescriptor(**values)


def _write_recording(tmp_path: Path, recording_id: str, packets: list[bytes]) -> Path:
    async def exercise() -> None:
        sink = FramedAtomicRecordingSink(tmp_path, recording_id)
        await sink.open()
        for packet in packets:
            await sink.write(memoryview(packet))
        await sink.finalize()

    asyncio.run(exercise())
    return tmp_path / f"{recording_id}.k5r"


def test_playback_round_trip_verifies_descriptor_and_preserves_packets(tmp_path: Path) -> None:
    packets = [
        _rtp(b"a", sequence=1, timestamp=123_456),
        _rtp(b"bc", sequence=2, timestamp=126_456),
        _rtp(b"def", sequence=3, timestamp=129_456),
    ]
    path = _write_recording(tmp_path, "round-trip", packets)
    playback = BoundedRecordingPlayback(path, _descriptor(packets))

    assert list(playback.iter_packets()) == packets
    assert playback.snapshot.state == PlaybackState.COMPLETE
    assert playback.snapshot.descriptor_verified is True
    assert playback.snapshot.packets == 3
    assert playback.snapshot.payload_bytes == sum(len(packet) for packet in packets)
    assert playback.snapshot.file_bytes == _descriptor(packets).file_bytes


def test_payload_type_mismatch_fails_closed_without_source_details(tmp_path: Path) -> None:
    packets = [_rtp(b"SECRET_MEDIA", payload_type=97)]
    path = _write_recording(tmp_path, "payload-type", packets)
    playback = BoundedRecordingPlayback(path, _descriptor(packets))

    with pytest.raises(PlaybackError) as exc:
        list(playback.iter_packets())

    assert exc.value.code == PlaybackErrorCode.RTP_MISMATCH
    assert playback.snapshot.state == PlaybackState.FAILED
    assert "SECRET_MEDIA" not in str(exc.value)
    assert str(tmp_path) not in str(exc.value)


def test_timestamp_origin_mismatch_fails_closed(tmp_path: Path) -> None:
    packets = [_rtp(timestamp=123_457)]
    path = _write_recording(tmp_path, "timestamp", packets)
    playback = BoundedRecordingPlayback(path, _descriptor(packets))

    with pytest.raises(PlaybackError) as exc:
        list(playback.iter_packets())

    assert exc.value.code == PlaybackErrorCode.RTP_MISMATCH
    assert playback.snapshot.state == PlaybackState.FAILED
    assert playback.snapshot.descriptor_verified is False


def test_descriptor_count_mismatch_is_detected_before_extra_packet_is_exposed(
    tmp_path: Path,
) -> None:
    packets = [
        _rtp(b"a", sequence=1),
        _rtp(b"b", sequence=2, timestamp=124_456),
    ]
    path = _write_recording(tmp_path, "count-mismatch", packets)
    descriptor = _descriptor([packets[0]])
    playback = BoundedRecordingPlayback(path, descriptor)
    iterator = playback.iter_packets()

    assert next(iterator) == packets[0]
    with pytest.raises(PlaybackError) as exc:
        next(iterator)

    assert exc.value.code == PlaybackErrorCode.DESCRIPTOR_MISMATCH
    assert playback.snapshot.state == PlaybackState.FAILED
    assert playback.snapshot.descriptor_verified is False


def test_underlying_integrity_failure_is_sanitized_and_classified(tmp_path: Path) -> None:
    packets = [_rtp(b"payload")]
    path = _write_recording(tmp_path, "corrupt", packets)
    damaged = bytearray(path.read_bytes())
    damaged[-1] ^= 0xFF
    path.write_bytes(damaged)
    playback = BoundedRecordingPlayback(path, _descriptor(packets))

    with pytest.raises(PlaybackError) as exc:
        list(playback.iter_packets())

    assert exc.value.code == PlaybackErrorCode.RECORDING_FAILURE
    assert exc.value.recording_error_code == FramedRecordingErrorCode.INTEGRITY_FAILURE
    assert playback.snapshot.state == PlaybackState.FAILED
    assert str(tmp_path) not in str(exc.value)


def test_missing_recording_failure_does_not_echo_path(tmp_path: Path) -> None:
    packets = [_rtp()]
    secret_path = tmp_path / "SECRET-recording-path.k5r"
    playback = BoundedRecordingPlayback(secret_path, _descriptor(packets))

    with pytest.raises(PlaybackError) as exc:
        list(playback.iter_packets())

    assert exc.value.code == PlaybackErrorCode.RECORDING_FAILURE
    assert exc.value.recording_error_code == FramedRecordingErrorCode.IO_FAILURE
    assert "SECRET" not in str(exc.value)
    assert str(tmp_path) not in str(exc.value)


def test_consumer_abort_closes_reader_and_marks_boundary_aborted(tmp_path: Path) -> None:
    packets = [_rtp(b"a"), _rtp(b"b", sequence=2, timestamp=124_456)]
    path = _write_recording(tmp_path, "abort", packets)
    playback = BoundedRecordingPlayback(path, _descriptor(packets))
    iterator = playback.iter_packets()

    assert next(iterator) == packets[0]
    iterator.close()

    assert playback.snapshot.state == PlaybackState.ABORTED
    assert playback.snapshot.descriptor_verified is False
    assert playback.snapshot.packets == 1


def test_completed_boundary_cannot_be_reused(tmp_path: Path) -> None:
    packets = [_rtp()]
    path = _write_recording(tmp_path, "single-use", packets)
    playback = BoundedRecordingPlayback(path, _descriptor(packets))

    assert list(playback.iter_packets()) == packets
    with pytest.raises(PlaybackError) as exc:
        list(playback.iter_packets())
    assert exc.value.code == PlaybackErrorCode.INVALID_STATE


def test_constructor_enforces_local_playback_limits(tmp_path: Path) -> None:
    packets = [_rtp(b"a"), _rtp(b"b", sequence=2, timestamp=124_456)]
    descriptor = _descriptor(packets)

    with pytest.raises(ValueError):
        BoundedRecordingPlayback(tmp_path / "x.k5r", descriptor, max_packets=1)
    with pytest.raises(ValueError):
        BoundedRecordingPlayback(
            tmp_path / "x.k5r",
            descriptor,
            max_payload_bytes=descriptor.payload_bytes - 1,
        )
