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
from k5vision.media.recording_descriptor import RecordingStreamDescriptor, VideoCodec

_RECORDING_ID = UUID("12121212-1212-4212-8212-121212121212")
_SOURCE_ID = UUID("34343434-3434-4434-8434-343434343434")
_START = datetime(2026, 9, 22, 22, 0, 0, tzinfo=UTC)


def _rtp(payload: bytes, sequence: int, timestamp: int) -> bytes:
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
        _rtp(b"a", 1, 123_456),
        _rtp(b"b", 2, 132_456),
        _rtp(b"c", 3, 141_456),
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


async def _write_recording(tmp_path: Path, packets: list[bytes]) -> Path:
    sink = FramedAtomicRecordingSink(tmp_path, "pause-control")
    await sink.open()
    for packet in packets:
        await sink.write(memoryview(packet))
    await sink.finalize()
    return tmp_path / "pause-control.k5r"


class ControlledClock:
    def __init__(self) -> None:
        self.now = 100.0
        self.sleep_calls: list[float] = []
        self.sleep_started = asyncio.Event()
        self.release_sleep = asyncio.Event()

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.sleep_calls.append(delay)
        self.sleep_started.set()
        await self.release_sleep.wait()
        self.now += delay


async def _wait_for_sleep_calls(clock: ControlledClock, count: int) -> None:
    for _ in range(100):
        if len(clock.sleep_calls) >= count:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"expected at least {count} pacing sleeps")


def test_pause_resume_shifts_deadlines_without_marking_packets_late(tmp_path: Path) -> None:
    async def exercise() -> tuple[BoundedPlaybackPump, list[int], ControlledClock]:
        packets = _packets()
        path = await _write_recording(tmp_path, packets)
        clock = ControlledClock()
        delivered: list[int] = []

        async def consumer(_packet: memoryview, source_elapsed_ms: int) -> None:
            delivered.append(source_elapsed_ms)

        pump = BoundedPlaybackPump(
            path,
            _descriptor(packets),
            0,
            200,
            clock=clock.monotonic,
            sleep=clock.sleep,
        )
        task = asyncio.create_task(pump.run(consumer))
        await clock.sleep_started.wait()

        paused = await pump.pause()
        assert paused.state is PlaybackPumpState.PAUSED
        assert delivered == [0]

        clock.now += 5.0
        resumed = await pump.resume()
        assert resumed.state is PlaybackPumpState.RUNNING

        await _wait_for_sleep_calls(clock, 2)
        clock.release_sleep.set()
        result = await task
        assert result.state is PlaybackPumpState.COMPLETE
        assert result.late_packets == 0
        return pump, delivered, clock

    pump, delivered, clock = asyncio.run(exercise())

    assert delivered == [0, 100, 200]
    assert clock.sleep_calls == pytest.approx([0.1, 0.1, 0.1])
    assert pump.snapshot.delivered_packets == 3
    assert pump.snapshot.descriptor_verified is True


def test_rejected_pause_resume_controls_leave_state_unchanged(tmp_path: Path) -> None:
    packets = _packets()
    descriptor = _descriptor(packets)
    pump = BoundedPlaybackPump(tmp_path / "unused.k5r", descriptor, 0, 200)

    async def exercise() -> None:
        with pytest.raises(PlaybackPumpError) as pause_exc:
            await pump.pause()
        assert pause_exc.value.code is PlaybackPumpErrorCode.INVALID_STATE
        assert pump.snapshot.state is PlaybackPumpState.CREATED

        with pytest.raises(PlaybackPumpError) as resume_exc:
            await pump.resume()
        assert resume_exc.value.code is PlaybackPumpErrorCode.INVALID_STATE
        assert pump.snapshot.state is PlaybackPumpState.CREATED

    asyncio.run(exercise())


def test_pause_snapshot_retains_no_media_or_identifiers(tmp_path: Path) -> None:
    async def exercise() -> str:
        packets = _packets()
        path = await _write_recording(tmp_path, packets)
        clock = ControlledClock()

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
        task = asyncio.create_task(pump.run(consumer))
        await clock.sleep_started.wait()
        snapshot = await pump.pause()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return snapshot.model_dump_json()

    payload = asyncio.run(exercise())
    assert str(tmp_path) not in payload
    assert str(_RECORDING_ID) not in payload
    assert str(_SOURCE_ID) not in payload
    assert "SECRET" not in payload
