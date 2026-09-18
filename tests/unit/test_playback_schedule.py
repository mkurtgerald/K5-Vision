from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from k5vision.media.framed_recording import FramedAtomicRecordingSink
from k5vision.media.playback_schedule import (
    BoundedPlaybackSchedule,
    PlaybackRate,
    PlaybackScheduleError,
    PlaybackScheduleErrorCode,
    PlaybackScheduleState,
)
from k5vision.media.playback_window import PlaybackWindowErrorCode
from k5vision.media.recording_descriptor import RecordingStreamDescriptor, VideoCodec

_RECORDING_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
_SOURCE_ID = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
_START = datetime(2026, 9, 18, 0, 0, 0, tzinfo=UTC)


def _rtp(
    payload: bytes = b"x",
    *,
    sequence: int = 1,
    timestamp: int = 123_456,
) -> bytes:
    return (
        bytes(
            [
                0x80,
                96,
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


def _packets() -> list[bytes]:
    return [
        _rtp(b"a", sequence=1, timestamp=123_456),
        _rtp(b"b", sequence=2, timestamp=132_456),
        _rtp(b"c", sequence=3, timestamp=141_456),
        _rtp(b"d", sequence=4, timestamp=150_456),
    ]


def _descriptor(packets: list[bytes]) -> RecordingStreamDescriptor:
    return RecordingStreamDescriptor(
        recording_id=_RECORDING_ID,
        source_id=_SOURCE_ID,
        codec=VideoCodec.H264,
        payload_type=96,
        clock_rate_hz=90_000,
        started_at_utc=_START,
        ended_at_utc=_START + timedelta(seconds=1),
        duration_ms=1000,
        rtp_timestamp_origin=123_456,
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


def test_normal_rate_schedule_is_relative_to_window_start(tmp_path: Path) -> None:
    packets = _packets()
    path = _write_recording(tmp_path, "normal-schedule", packets)
    schedule = BoundedPlaybackSchedule(path, _descriptor(packets), 100, 300)

    emitted = list(schedule.iter_packets())

    assert [item.source_elapsed_ms for item in emitted] == [100, 200, 300]
    assert [item.due_ms for item in emitted] == [0, 100, 200]
    assert [item.packet for item in emitted] == packets[1:]
    assert schedule.snapshot.state == PlaybackScheduleState.COMPLETE
    assert schedule.snapshot.packets == 3
    assert schedule.snapshot.source_span_ms == 200
    assert schedule.snapshot.scheduled_span_ms == 200
    assert schedule.snapshot.descriptor_verified is True


def test_double_rate_halves_due_times(tmp_path: Path) -> None:
    packets = _packets()
    path = _write_recording(tmp_path, "double-schedule", packets)
    schedule = BoundedPlaybackSchedule(
        path,
        _descriptor(packets),
        0,
        300,
        PlaybackRate.DOUBLE,
    )

    emitted = list(schedule.iter_packets())

    assert [item.due_ms for item in emitted] == [0, 50, 100, 150]
    assert schedule.snapshot.rate == PlaybackRate.DOUBLE
    assert schedule.snapshot.scheduled_span_ms == 150


def test_half_rate_doubles_due_times(tmp_path: Path) -> None:
    packets = _packets()
    path = _write_recording(tmp_path, "half-schedule", packets)
    schedule = BoundedPlaybackSchedule(
        path,
        _descriptor(packets),
        0,
        300,
        PlaybackRate.HALF,
    )

    emitted = list(schedule.iter_packets())

    assert [item.due_ms for item in emitted] == [0, 200, 400, 600]
    assert schedule.snapshot.scheduled_span_ms == 600


def test_quarter_and_sixteen_rates_are_bounded_and_deterministic(tmp_path: Path) -> None:
    packets = _packets()
    path = _write_recording(tmp_path, "rate-extremes", packets)

    quarter = BoundedPlaybackSchedule(
        path,
        _descriptor(packets),
        0,
        100,
        PlaybackRate.QUARTER,
    )
    sixteen = BoundedPlaybackSchedule(
        path,
        _descriptor(packets),
        0,
        100,
        PlaybackRate.SIXTEEN,
    )

    assert [item.due_ms for item in quarter.iter_packets()] == [0, 400]
    assert [item.due_ms for item in sixteen.iter_packets()] == [0, 6]


def test_empty_window_completes_and_verifies(tmp_path: Path) -> None:
    packets = _packets()
    path = _write_recording(tmp_path, "empty-schedule", packets)
    schedule = BoundedPlaybackSchedule(path, _descriptor(packets), 999, 1000)

    assert list(schedule.iter_packets()) == []
    assert schedule.snapshot.state == PlaybackScheduleState.COMPLETE
    assert schedule.snapshot.packets == 0
    assert schedule.snapshot.source_span_ms == 0
    assert schedule.snapshot.scheduled_span_ms == 0
    assert schedule.snapshot.descriptor_verified is True


def test_invalid_rate_is_rejected_before_read(tmp_path: Path) -> None:
    descriptor = _descriptor(_packets())

    with pytest.raises(TypeError):
        BoundedPlaybackSchedule(  # type: ignore[arg-type]
            tmp_path / "unused.k5r",
            descriptor,
            0,
            100,
            "2x",
        )


def test_window_failure_is_sanitized(tmp_path: Path) -> None:
    packets = _packets()
    secret_path = tmp_path / "SECRET-recording.k5r"
    schedule = BoundedPlaybackSchedule(secret_path, _descriptor(packets), 0, 100)

    with pytest.raises(PlaybackScheduleError) as exc:
        list(schedule.iter_packets())

    assert exc.value.code == PlaybackScheduleErrorCode.WINDOW_FAILURE
    assert exc.value.window_error_code == PlaybackWindowErrorCode.NAVIGATION_FAILURE
    assert "SECRET" not in str(exc.value)
    assert str(tmp_path) not in str(exc.value)
    assert schedule.snapshot.state == PlaybackScheduleState.FAILED


def test_consumer_abort_marks_schedule_aborted(tmp_path: Path) -> None:
    packets = _packets()
    path = _write_recording(tmp_path, "abort-schedule", packets)
    schedule = BoundedPlaybackSchedule(path, _descriptor(packets), 0, 300)
    iterator = schedule.iter_packets()

    assert next(iterator).due_ms == 0
    iterator.close()

    assert schedule.snapshot.state == PlaybackScheduleState.ABORTED
    assert schedule.snapshot.packets == 1
    assert schedule.snapshot.descriptor_verified is False


def test_completed_schedule_cannot_be_reused(tmp_path: Path) -> None:
    packets = _packets()
    path = _write_recording(tmp_path, "single-use-schedule", packets)
    schedule = BoundedPlaybackSchedule(path, _descriptor(packets), 0, 300)

    assert len(list(schedule.iter_packets())) == 4
    with pytest.raises(PlaybackScheduleError) as exc:
        list(schedule.iter_packets())

    assert exc.value.code == PlaybackScheduleErrorCode.INVALID_STATE


def test_snapshot_is_source_path_identifier_and_payload_free(tmp_path: Path) -> None:
    packets = [_rtp(b"SECRET_MEDIA")]
    path = _write_recording(tmp_path, "schedule-snapshot", packets)
    schedule = BoundedPlaybackSchedule(path, _descriptor(packets), 0, 0)

    list(schedule.iter_packets())
    payload = schedule.snapshot.model_dump_json()

    assert "SECRET_MEDIA" not in payload
    assert str(tmp_path) not in payload
    assert "source_id" not in payload
    assert "recording_id" not in payload
