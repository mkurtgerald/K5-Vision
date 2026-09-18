from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from k5vision.media.framed_recording import FramedAtomicRecordingSink
from k5vision.media.playback_pump import (
    BoundedPlaybackPump,
    PlaybackPumpError,
    PlaybackPumpErrorCode,
    PlaybackPumpState,
)
from k5vision.media.playback_schedule import PlaybackRate, PlaybackScheduleErrorCode
from k5vision.media.recording_descriptor import RecordingStreamDescriptor, VideoCodec

_RECORDING_ID = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
_SOURCE_ID = UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")
_START = datetime(2026, 9, 18, 1, 0, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self, now: float = 100.0) -> None:
        self.now = now
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.now += delay


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


def test_normal_rate_paces_and_delivers_source_relative_timing(tmp_path: Path) -> None:
    packets = _packets()
    path = _write_recording(tmp_path, "normal-pump", packets)
    clock = FakeClock()
    delivered: list[tuple[bytes, int]] = []

    async def consumer(packet: memoryview, source_elapsed_ms: int) -> None:
        delivered.append((bytes(packet), source_elapsed_ms))

    pump = BoundedPlaybackPump(
        path,
        _descriptor(packets),
        0,
        200,
        clock=clock.monotonic,
        sleep=clock.sleep,
    )
    snapshot = asyncio.run(pump.run(consumer))

    assert delivered == list(zip(packets, [0, 100, 200], strict=True))
    assert clock.sleeps == pytest.approx([0.1, 0.1])
    assert snapshot.state == PlaybackPumpState.COMPLETE
    assert snapshot.delivered_packets == 3
    assert snapshot.delivered_bytes == sum(len(packet) for packet in packets)
    assert snapshot.source_span_ms == 200
    assert snapshot.scheduled_span_ms == 200
    assert snapshot.late_packets == 0
    assert snapshot.descriptor_verified is True


def test_double_rate_halves_pacing_delays(tmp_path: Path) -> None:
    packets = _packets()
    path = _write_recording(tmp_path, "double-pump", packets)
    clock = FakeClock()

    async def consumer(_packet: memoryview, _source_elapsed_ms: int) -> None:
        return None

    pump = BoundedPlaybackPump(
        path,
        _descriptor(packets),
        0,
        200,
        PlaybackRate.DOUBLE,
        clock=clock.monotonic,
        sleep=clock.sleep,
    )
    snapshot = asyncio.run(pump.run(consumer))

    assert clock.sleeps == pytest.approx([0.05, 0.05])
    assert snapshot.rate == PlaybackRate.DOUBLE
    assert snapshot.scheduled_span_ms == 100


def test_late_packets_are_counted_without_extra_sleep(tmp_path: Path) -> None:
    packets = _packets()
    path = _write_recording(tmp_path, "late-pump", packets)
    clock = FakeClock()

    async def consumer(_packet: memoryview, _source_elapsed_ms: int) -> None:
        clock.now += 0.15

    pump = BoundedPlaybackPump(
        path,
        _descriptor(packets),
        0,
        200,
        clock=clock.monotonic,
        sleep=clock.sleep,
    )
    snapshot = asyncio.run(pump.run(consumer))

    assert clock.sleeps == []
    assert snapshot.late_packets == 2
    assert snapshot.delivered_packets == 3


def test_empty_window_completes_without_delivery(tmp_path: Path) -> None:
    packets = _packets()
    path = _write_recording(tmp_path, "empty-pump", packets)
    clock = FakeClock()
    calls = 0

    async def consumer(_packet: memoryview, _source_elapsed_ms: int) -> None:
        nonlocal calls
        calls += 1

    pump = BoundedPlaybackPump(
        path,
        _descriptor(packets),
        999,
        1000,
        clock=clock.monotonic,
        sleep=clock.sleep,
    )
    snapshot = asyncio.run(pump.run(consumer))

    assert calls == 0
    assert snapshot.state == PlaybackPumpState.COMPLETE
    assert snapshot.delivered_packets == 0
    assert snapshot.descriptor_verified is True


def test_consumer_timeout_is_bounded_and_sanitized(tmp_path: Path) -> None:
    packets = _packets()
    path = _write_recording(tmp_path, "timeout-pump", packets)
    clock = FakeClock()

    async def consumer(_packet: memoryview, _source_elapsed_ms: int) -> None:
        await asyncio.sleep(1)

    pump = BoundedPlaybackPump(
        path,
        _descriptor(packets),
        0,
        200,
        consumer_timeout_seconds=0.001,
        clock=clock.monotonic,
        sleep=clock.sleep,
    )

    with pytest.raises(PlaybackPumpError) as exc:
        asyncio.run(pump.run(consumer))

    assert exc.value.code == PlaybackPumpErrorCode.CONSUMER_TIMEOUT
    assert str(tmp_path) not in str(exc.value)
    assert pump.snapshot.state == PlaybackPumpState.FAILED
    assert pump.snapshot.delivered_packets == 0


def test_consumer_failure_does_not_expose_callback_detail(tmp_path: Path) -> None:
    packets = _packets()
    path = _write_recording(tmp_path, "failure-pump", packets)
    clock = FakeClock()

    async def consumer(_packet: memoryview, _source_elapsed_ms: int) -> None:
        raise RuntimeError(f"SECRET consumer detail {tmp_path}")

    pump = BoundedPlaybackPump(
        path,
        _descriptor(packets),
        0,
        200,
        clock=clock.monotonic,
        sleep=clock.sleep,
    )

    with pytest.raises(PlaybackPumpError) as exc:
        asyncio.run(pump.run(consumer))

    assert exc.value.code == PlaybackPumpErrorCode.CONSUMER_FAILURE
    assert "SECRET" not in str(exc.value)
    assert str(tmp_path) not in str(exc.value)
    assert pump.snapshot.state == PlaybackPumpState.FAILED


def test_schedule_failure_is_sanitized(tmp_path: Path) -> None:
    packets = _packets()
    secret_path = tmp_path / "SECRET-recording.k5r"
    clock = FakeClock()

    async def consumer(_packet: memoryview, _source_elapsed_ms: int) -> None:
        return None

    pump = BoundedPlaybackPump(
        secret_path,
        _descriptor(packets),
        0,
        200,
        clock=clock.monotonic,
        sleep=clock.sleep,
    )

    with pytest.raises(PlaybackPumpError) as exc:
        asyncio.run(pump.run(consumer))

    assert exc.value.code == PlaybackPumpErrorCode.SCHEDULE_FAILURE
    assert exc.value.schedule_error_code == PlaybackScheduleErrorCode.WINDOW_FAILURE
    assert "SECRET" not in str(exc.value)
    assert str(tmp_path) not in str(exc.value)
    assert pump.snapshot.state == PlaybackPumpState.FAILED


def test_clock_failure_is_sanitized(tmp_path: Path) -> None:
    packets = _packets()
    path = _write_recording(tmp_path, "clock-failure", packets)

    def broken_clock() -> float:
        raise RuntimeError(f"SECRET clock detail {tmp_path}")

    async def consumer(_packet: memoryview, _source_elapsed_ms: int) -> None:
        return None

    pump = BoundedPlaybackPump(path, _descriptor(packets), 0, 200, clock=broken_clock)

    with pytest.raises(PlaybackPumpError) as exc:
        asyncio.run(pump.run(consumer))

    assert exc.value.code == PlaybackPumpErrorCode.PACING_FAILURE
    assert "SECRET" not in str(exc.value)
    assert str(tmp_path) not in str(exc.value)
    assert pump.snapshot.state == PlaybackPumpState.FAILED


def test_sleep_failure_is_sanitized(tmp_path: Path) -> None:
    packets = _packets()
    path = _write_recording(tmp_path, "sleep-failure", packets)
    clock = FakeClock()

    async def broken_sleep(_delay: float) -> None:
        raise RuntimeError(f"SECRET sleep detail {tmp_path}")

    async def consumer(_packet: memoryview, _source_elapsed_ms: int) -> None:
        return None

    pump = BoundedPlaybackPump(
        path,
        _descriptor(packets),
        0,
        200,
        clock=clock.monotonic,
        sleep=broken_sleep,
    )

    with pytest.raises(PlaybackPumpError) as exc:
        asyncio.run(pump.run(consumer))

    assert exc.value.code == PlaybackPumpErrorCode.PACING_FAILURE
    assert "SECRET" not in str(exc.value)
    assert pump.snapshot.state == PlaybackPumpState.FAILED


def test_cancellation_is_propagated_and_recorded(tmp_path: Path) -> None:
    packets = _packets()
    path = _write_recording(tmp_path, "cancel-pump", packets)
    reached_sleep = asyncio.Event()
    block = asyncio.Event()
    now = 100.0

    def clock() -> float:
        return now

    async def blocking_sleep(_delay: float) -> None:
        reached_sleep.set()
        await block.wait()

    async def consumer(_packet: memoryview, _source_elapsed_ms: int) -> None:
        return None

    async def exercise() -> BoundedPlaybackPump:
        pump = BoundedPlaybackPump(
            path,
            _descriptor(packets),
            0,
            200,
            clock=clock,
            sleep=blocking_sleep,
        )
        task = asyncio.create_task(pump.run(consumer))
        await reached_sleep.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return pump

    pump = asyncio.run(exercise())
    assert pump.snapshot.state == PlaybackPumpState.CANCELLED
    assert pump.snapshot.delivered_packets == 1
    assert pump.snapshot.descriptor_verified is False


def test_completed_pump_cannot_be_reused(tmp_path: Path) -> None:
    packets = _packets()
    path = _write_recording(tmp_path, "single-use-pump", packets)
    clock = FakeClock()

    async def consumer(_packet: memoryview, _source_elapsed_ms: int) -> None:
        return None

    pump = BoundedPlaybackPump(
        path,
        _descriptor(packets),
        0,
        200,
        clock=clock.monotonic,
        sleep=clock.sleep,
    )
    asyncio.run(pump.run(consumer))

    with pytest.raises(PlaybackPumpError) as exc:
        asyncio.run(pump.run(consumer))

    assert exc.value.code == PlaybackPumpErrorCode.INVALID_STATE


def test_constructor_and_consumer_bounds_fail_closed(tmp_path: Path) -> None:
    descriptor = _descriptor(_packets())
    path = tmp_path / "unused.k5r"

    with pytest.raises(ValueError):
        BoundedPlaybackPump(path, descriptor, 0, 100, consumer_timeout_seconds=0)
    with pytest.raises(ValueError):
        BoundedPlaybackPump(path, descriptor, 0, 100, consumer_timeout_seconds=11)
    with pytest.raises(TypeError):
        BoundedPlaybackPump(path, descriptor, 0, 100, clock=0)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        BoundedPlaybackPump(path, descriptor, 0, 100, sleep=0)  # type: ignore[arg-type]

    pump = BoundedPlaybackPump(path, descriptor, 0, 100)
    with pytest.raises(TypeError):
        asyncio.run(pump.run(None))  # type: ignore[arg-type]
    assert pump.snapshot.state == PlaybackPumpState.CREATED


def test_snapshot_is_source_path_identifier_and_payload_free(tmp_path: Path) -> None:
    packets = [_rtp(b"SECRET_MEDIA")]
    path = _write_recording(tmp_path, "pump-snapshot", packets)
    clock = FakeClock()

    async def consumer(_packet: memoryview, _source_elapsed_ms: int) -> None:
        return None

    pump = BoundedPlaybackPump(
        path,
        _descriptor(packets),
        0,
        0,
        clock=clock.monotonic,
        sleep=clock.sleep,
    )
    asyncio.run(pump.run(consumer))
    payload = pump.snapshot.model_dump_json()

    assert "SECRET_MEDIA" not in payload
    assert str(tmp_path) not in payload
    assert "source_id" not in payload
    assert "recording_id" not in payload
