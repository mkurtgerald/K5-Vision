from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from k5vision.media.framed_recording import FramedAtomicRecordingSink
from k5vision.media.playback import PlaybackErrorCode
from k5vision.media.playback_timeline import (
    BoundedPlaybackTimeline,
    PlaybackTimelineError,
    PlaybackTimelineErrorCode,
    PlaybackTimelineState,
)
from k5vision.media.recording_descriptor import RecordingStreamDescriptor, VideoCodec

_RECORDING_ID = UUID("55555555-5555-4555-8555-555555555555")
_SOURCE_ID = UUID("66666666-6666-4666-8666-666666666666")
_START = datetime(2026, 9, 17, 16, 0, 0, tzinfo=UTC)


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


def _descriptor(
    packets: list[bytes],
    *,
    origin: int = 123_456,
) -> RecordingStreamDescriptor:
    return RecordingStreamDescriptor(
        recording_id=_RECORDING_ID,
        source_id=_SOURCE_ID,
        codec=VideoCodec.H264,
        payload_type=96,
        clock_rate_hz=90_000,
        started_at_utc=_START,
        ended_at_utc=_START + timedelta(seconds=1),
        duration_ms=1000,
        rtp_timestamp_origin=origin,
        packet_count=len(packets),
        payload_bytes=sum(len(packet) for packet in packets),
        file_bytes=8 + sum(8 + len(packet) for packet in packets),
    )


def _write_recording(tmp_path: Path, recording_id: str, packets: list[bytes]) -> Path:
    async def exercise() -> None:
        sink = FramedAtomicRecordingSink(tmp_path, recording_id)
        await sink.open()
        for packet in packets:
            await sink.write(memoryview(packet))
        await sink.finalize()

    asyncio.run(exercise())
    return tmp_path / f"{recording_id}.k5r"


def test_timeline_emits_deterministic_relative_milliseconds(tmp_path: Path) -> None:
    packets = [
        _rtp(b"a", sequence=1, timestamp=123_456),
        _rtp(b"b", sequence=2, timestamp=126_456),
        _rtp(b"c", sequence=3, timestamp=129_456),
    ]
    path = _write_recording(tmp_path, "timeline", packets)
    timeline = BoundedPlaybackTimeline(path, _descriptor(packets))

    emitted = list(timeline.iter_packets())

    assert [item.packet for item in emitted] == packets
    assert [item.ordinal for item in emitted] == [0, 1, 2]
    assert [item.elapsed_ms for item in emitted] == [0, 33, 66]
    assert timeline.snapshot.state == PlaybackTimelineState.COMPLETE
    assert timeline.snapshot.descriptor_verified is True
    assert timeline.snapshot.timestamp_wraps == 0


def test_timeline_unwraps_forward_rtp_timestamp_rollover(tmp_path: Path) -> None:
    origin = 0xFFFFFF00
    next_timestamp = (origin + 3000) & 0xFFFFFFFF
    packets = [
        _rtp(sequence=1, timestamp=origin),
        _rtp(sequence=2, timestamp=next_timestamp),
    ]
    path = _write_recording(tmp_path, "wrap", packets)
    timeline = BoundedPlaybackTimeline(path, _descriptor(packets, origin=origin))

    emitted = list(timeline.iter_packets())

    assert [item.elapsed_ms for item in emitted] == [0, 33]
    assert timeline.snapshot.timestamp_wraps == 1
    assert timeline.snapshot.state == PlaybackTimelineState.COMPLETE


def test_timestamp_regression_fails_closed(tmp_path: Path) -> None:
    packets = [
        _rtp(sequence=1, timestamp=123_456),
        _rtp(sequence=2, timestamp=122_456),
    ]
    path = _write_recording(tmp_path, "regression", packets)
    timeline = BoundedPlaybackTimeline(path, _descriptor(packets))

    with pytest.raises(PlaybackTimelineError) as exc:
        list(timeline.iter_packets())

    assert exc.value.code == PlaybackTimelineErrorCode.TIMESTAMP_REGRESSION
    assert timeline.snapshot.state == PlaybackTimelineState.FAILED
    assert timeline.snapshot.descriptor_verified is False


def test_underlying_playback_failure_is_sanitized(tmp_path: Path) -> None:
    packets = [_rtp()]
    secret_path = tmp_path / "SECRET-recording.k5r"
    timeline = BoundedPlaybackTimeline(secret_path, _descriptor(packets))

    with pytest.raises(PlaybackTimelineError) as exc:
        list(timeline.iter_packets())

    assert exc.value.code == PlaybackTimelineErrorCode.PLAYBACK_FAILURE
    assert exc.value.playback_error_code == PlaybackErrorCode.RECORDING_FAILURE
    assert "SECRET" not in str(exc.value)
    assert str(tmp_path) not in str(exc.value)
    assert timeline.snapshot.state == PlaybackTimelineState.FAILED


def test_consumer_abort_closes_underlying_playback(tmp_path: Path) -> None:
    packets = [
        _rtp(sequence=1, timestamp=123_456),
        _rtp(sequence=2, timestamp=126_456),
    ]
    path = _write_recording(tmp_path, "abort-timeline", packets)
    timeline = BoundedPlaybackTimeline(path, _descriptor(packets))
    iterator = timeline.iter_packets()

    assert next(iterator).ordinal == 0
    iterator.close()

    assert timeline.snapshot.state == PlaybackTimelineState.ABORTED
    assert timeline.snapshot.packets == 1
    assert timeline.snapshot.descriptor_verified is False


def test_completed_timeline_cannot_be_reused(tmp_path: Path) -> None:
    packets = [_rtp()]
    path = _write_recording(tmp_path, "single-use-timeline", packets)
    timeline = BoundedPlaybackTimeline(path, _descriptor(packets))

    assert len(list(timeline.iter_packets())) == 1
    with pytest.raises(PlaybackTimelineError) as exc:
        list(timeline.iter_packets())

    assert exc.value.code == PlaybackTimelineErrorCode.INVALID_STATE


def test_timeline_limit_rejects_descriptor_before_read(tmp_path: Path) -> None:
    packets = [_rtp(), _rtp(sequence=2, timestamp=126_456)]
    descriptor = _descriptor(packets)

    with pytest.raises(ValueError):
        BoundedPlaybackTimeline(tmp_path / "unused.k5r", descriptor, max_packets=1)


def test_snapshot_is_source_free_and_payload_free(tmp_path: Path) -> None:
    packets = [_rtp(b"SECRET_MEDIA")]
    path = _write_recording(tmp_path, "snapshot", packets)
    timeline = BoundedPlaybackTimeline(path, _descriptor(packets))

    list(timeline.iter_packets())
    payload = timeline.snapshot.model_dump_json()

    assert "SECRET_MEDIA" not in payload
    assert str(tmp_path) not in payload
    assert "source_id" not in payload
    assert "recording_id" not in payload
