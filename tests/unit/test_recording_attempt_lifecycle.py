from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from k5vision.media.file_sink import AtomicLocalRecordingSink
from k5vision.media.framed_recording import FramedAtomicRecordingSink, FramedRecordingReader
from k5vision.media.recording import (
    BoundedRtpRecorder,
    RecordingError,
    RecordingErrorCode,
    RecordingState,
)


def _rtp(payload: bytes = b"payload") -> bytes:
    return bytes([0x80, 96, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1]) + payload


def test_duplicate_raw_attempt_cannot_remove_active_recording(tmp_path: Path) -> None:
    async def exercise() -> None:
        packet = _rtp(b"raw-owner")
        first = BoundedRtpRecorder(
            AtomicLocalRecordingSink(tmp_path, "shared-raw"),
            max_packets=2,
            max_bytes=4096,
        )
        second = BoundedRtpRecorder(
            AtomicLocalRecordingSink(tmp_path, "shared-raw"),
            max_packets=2,
            max_bytes=4096,
        )

        await first.start()
        await first.consume(memoryview(packet))
        with pytest.raises(RecordingError):
            await second.start()

        snapshot = await first.finalize()
        assert snapshot.state == RecordingState.FINALIZED
        assert (tmp_path / "shared-raw.rtp").read_bytes() == packet

    asyncio.run(exercise())


def test_duplicate_framed_attempt_cannot_remove_active_recording(tmp_path: Path) -> None:
    async def exercise() -> None:
        packet = _rtp(b"framed-owner")
        first = BoundedRtpRecorder(
            FramedAtomicRecordingSink(tmp_path, "shared-framed"),
            max_packets=2,
            max_bytes=4096,
        )
        second = BoundedRtpRecorder(
            FramedAtomicRecordingSink(tmp_path, "shared-framed"),
            max_packets=2,
            max_bytes=4096,
        )

        await first.start()
        await first.consume(memoryview(packet))
        with pytest.raises(RecordingError):
            await second.start()

        snapshot = await first.finalize()
        assert snapshot.state == RecordingState.FINALIZED
        reader = FramedRecordingReader(tmp_path / "shared-framed.k5r")
        assert list(reader.iter_packets()) == [packet]

    asyncio.run(exercise())


def test_raw_open_timeout_reconciles_late_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        sink = AtomicLocalRecordingSink(tmp_path, "late-raw")
        real_open = sink._open_sync

        def slow_open():
            time.sleep(0.08)
            return real_open()

        monkeypatch.setattr(sink, "_open_sync", slow_open)
        recorder = BoundedRtpRecorder(sink, operation_timeout_seconds=0.01)

        with pytest.raises(RecordingError) as caught:
            await recorder.start()
        assert caught.value.code == RecordingErrorCode.TIMEOUT
        assert recorder.snapshot.state == RecordingState.FAILED

        await asyncio.sleep(0.1)
        assert list(tmp_path.iterdir()) == []

    asyncio.run(exercise())


def test_framed_open_timeout_reconciles_late_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        sink = FramedAtomicRecordingSink(tmp_path, "late-framed")
        real_open = sink._open_sync

        def slow_open():
            time.sleep(0.08)
            return real_open()

        monkeypatch.setattr(sink, "_open_sync", slow_open)
        recorder = BoundedRtpRecorder(sink, operation_timeout_seconds=0.01)

        with pytest.raises(RecordingError) as caught:
            await recorder.start()
        assert caught.value.code == RecordingErrorCode.TIMEOUT
        assert recorder.snapshot.state == RecordingState.FAILED

        await asyncio.sleep(0.1)
        assert list(tmp_path.iterdir()) == []

    asyncio.run(exercise())
