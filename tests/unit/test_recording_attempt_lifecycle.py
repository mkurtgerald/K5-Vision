from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

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


def _sink(kind: str, tmp_path: Path, recording_id: str) -> Any:
    if kind == "raw":
        return AtomicLocalRecordingSink(tmp_path, recording_id, max_bytes=4096)
    return FramedAtomicRecordingSink(tmp_path, recording_id, max_payload_bytes=4096)


def _assert_attempt_clean(tmp_path: Path) -> None:
    assert list(tmp_path.iterdir()) == []


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
        _assert_attempt_clean(tmp_path)

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
        _assert_attempt_clean(tmp_path)

    asyncio.run(exercise())


@pytest.mark.parametrize("kind", ["raw", "framed"])
def test_write_timeout_settles_before_failure_is_reported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    async def exercise() -> None:
        sink = _sink(kind, tmp_path, f"late-write-{kind}")
        recorder = BoundedRtpRecorder(
            sink,
            max_packets=2,
            max_bytes=4096,
            operation_timeout_seconds=0.01,
        )
        await recorder.start()

        attribute = "_write_sync" if kind == "raw" else "_write_record_sync"
        real_write = getattr(sink, attribute)

        def slow_write(packet: memoryview) -> None:
            time.sleep(0.08)
            real_write(packet)

        monkeypatch.setattr(sink, attribute, slow_write)
        with pytest.raises(RecordingError) as caught:
            await recorder.consume(memoryview(_rtp(b"late-write")))
        assert caught.value.code == RecordingErrorCode.TIMEOUT
        assert recorder.snapshot.state == RecordingState.FAILED
        _assert_attempt_clean(tmp_path)

    asyncio.run(exercise())


@pytest.mark.parametrize("kind", ["raw", "framed"])
def test_flush_timeout_rolls_back_late_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    async def exercise() -> None:
        sink = _sink(kind, tmp_path, f"late-flush-{kind}")
        recorder = BoundedRtpRecorder(
            sink,
            max_packets=2,
            max_bytes=4096,
            operation_timeout_seconds=0.01,
        )
        await recorder.start()
        await recorder.consume(memoryview(_rtp(b"late-flush")))

        real_fsync = os.fsync

        def slow_fsync(fd: int) -> None:
            time.sleep(0.08)
            real_fsync(fd)

        monkeypatch.setattr(os, "fsync", slow_fsync)
        with pytest.raises(RecordingError) as caught:
            await recorder.finalize()
        assert caught.value.code == RecordingErrorCode.TIMEOUT
        assert recorder.snapshot.state == RecordingState.FAILED
        _assert_attempt_clean(tmp_path)

    asyncio.run(exercise())


@pytest.mark.parametrize("kind", ["raw", "framed"])
def test_publish_timeout_rolls_back_late_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    async def exercise() -> None:
        sink = _sink(kind, tmp_path, f"late-publish-{kind}")
        recorder = BoundedRtpRecorder(
            sink,
            max_packets=2,
            max_bytes=4096,
            operation_timeout_seconds=0.01,
        )
        await recorder.start()
        await recorder.consume(memoryview(_rtp(b"late-publish")))

        real_publish = sink._attempt.publish

        def slow_publish(final_path: Path):
            time.sleep(0.08)
            return real_publish(final_path)

        monkeypatch.setattr(sink._attempt, "publish", slow_publish)
        with pytest.raises(RecordingError) as caught:
            await recorder.finalize()
        assert caught.value.code == RecordingErrorCode.TIMEOUT
        assert recorder.snapshot.state == RecordingState.FAILED
        _assert_attempt_clean(tmp_path)

    asyncio.run(exercise())


@pytest.mark.parametrize("kind", ["raw", "framed"])
def test_cleanup_timeout_settles_before_aborted_state_is_reported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    async def exercise() -> None:
        sink = _sink(kind, tmp_path, f"late-cleanup-{kind}")
        recorder = BoundedRtpRecorder(
            sink,
            max_packets=2,
            max_bytes=4096,
            operation_timeout_seconds=0.01,
        )
        await recorder.start()
        await recorder.consume(memoryview(_rtp(b"late-cleanup")))

        real_abort = sink._abort_sync

        def slow_abort() -> None:
            time.sleep(0.08)
            real_abort()

        monkeypatch.setattr(sink, "_abort_sync", slow_abort)
        snapshot = await recorder.abort()
        assert snapshot.state == RecordingState.ABORTED
        _assert_attempt_clean(tmp_path)

    asyncio.run(exercise())
