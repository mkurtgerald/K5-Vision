from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from k5vision.media.file_sink import (
    AtomicLocalRecordingSink,
    FileSinkError,
    FileSinkErrorCode,
    FileSinkState,
)
from k5vision.media.recording import BoundedRtpRecorder, RecordingState


def _rtp_packet(payload: bytes = b"payload") -> bytes:
    return bytes([0x80, 96, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1]) + payload


def test_finalize_promotes_only_complete_payload(tmp_path: Path) -> None:
    async def exercise() -> None:
        sink = AtomicLocalRecordingSink(tmp_path, "recording-1", max_bytes=1024)
        await sink.open()
        await sink.write(memoryview(b"abc"))

        part = tmp_path / ".recording-1.part"
        final = tmp_path / "recording-1.rtp"
        assert part.exists()
        assert not final.exists()
        assert sink.snapshot.state == FileSinkState.OPEN

        await sink.finalize()
        await sink.finalize()

        assert not part.exists()
        assert final.read_bytes() == b"abc"
        assert sink.snapshot.state == FileSinkState.FINALIZED
        assert sink.snapshot.bytes_written == 3

    asyncio.run(exercise())


def test_abort_removes_partial_payload(tmp_path: Path) -> None:
    async def exercise() -> None:
        sink = AtomicLocalRecordingSink(tmp_path, "recording-2")
        await sink.open()
        await sink.write(memoryview(b"temporary"))
        part = tmp_path / ".recording-2.part"
        assert part.exists()

        await sink.abort()
        await sink.abort()

        assert not part.exists()
        assert not (tmp_path / "recording-2.rtp").exists()
        assert sink.snapshot.state == FileSinkState.ABORTED

    asyncio.run(exercise())


def test_existing_final_recording_is_never_overwritten(tmp_path: Path) -> None:
    async def exercise() -> None:
        final = tmp_path / "same.rtp"
        final.write_bytes(b"original")
        sink = AtomicLocalRecordingSink(tmp_path, "same")

        with pytest.raises(FileSinkError) as caught:
            await sink.open()

        assert caught.value.code == FileSinkErrorCode.CONFLICT
        assert str(tmp_path) not in str(caught.value)
        assert final.read_bytes() == b"original"

    asyncio.run(exercise())


def test_same_id_partial_writer_conflicts(tmp_path: Path) -> None:
    async def exercise() -> None:
        first = AtomicLocalRecordingSink(tmp_path, "same")
        second = AtomicLocalRecordingSink(tmp_path, "same")
        await first.open()

        with pytest.raises(FileSinkError) as caught:
            await second.open()

        assert caught.value.code == FileSinkErrorCode.CONFLICT
        await first.abort()

    asyncio.run(exercise())


def test_sink_enforces_defense_in_depth_byte_limit(tmp_path: Path) -> None:
    async def exercise() -> None:
        sink = AtomicLocalRecordingSink(tmp_path, "bounded", max_bytes=3)
        await sink.open()
        await sink.write(memoryview(b"abc"))

        with pytest.raises(FileSinkError) as caught:
            await sink.write(memoryview(b"d"))

        assert caught.value.code == FileSinkErrorCode.LIMIT_EXCEEDED
        assert sink.snapshot.bytes_written == 3
        await sink.abort()
        assert not (tmp_path / ".bounded.part").exists()

    asyncio.run(exercise())


def test_invalid_state_transitions_are_deterministic(tmp_path: Path) -> None:
    async def exercise() -> None:
        sink = AtomicLocalRecordingSink(tmp_path, "states")
        with pytest.raises(FileSinkError) as write_error:
            await sink.write(memoryview(b"x"))
        assert write_error.value.code == FileSinkErrorCode.INVALID_STATE

        await sink.abort()
        with pytest.raises(FileSinkError) as open_error:
            await sink.open()
        assert open_error.value.code == FileSinkErrorCode.INVALID_STATE

        finalized = AtomicLocalRecordingSink(tmp_path, "done")
        await finalized.open()
        await finalized.finalize()
        with pytest.raises(FileSinkError) as abort_error:
            await finalized.abort()
        assert abort_error.value.code == FileSinkErrorCode.INVALID_STATE

    asyncio.run(exercise())


@pytest.mark.parametrize(
    "recording_id",
    ["", ".", "..", "../escape", "folder/name", "folder\\name", " space", "a" * 129],
)
def test_recording_id_cannot_escape_storage_root(tmp_path: Path, recording_id: str) -> None:
    with pytest.raises(FileSinkError) as caught:
        AtomicLocalRecordingSink(tmp_path, recording_id)
    assert caught.value.code == FileSinkErrorCode.INVALID_RECORDING_ID
    assert str(tmp_path) not in str(caught.value)


def test_stage07_recorder_integrates_with_local_sink(tmp_path: Path) -> None:
    async def exercise() -> None:
        sink = AtomicLocalRecordingSink(tmp_path, "integrated", max_bytes=4096)
        recorder = BoundedRtpRecorder(sink, max_packets=2, max_bytes=4096)
        packet = _rtp_packet()

        await recorder.start()
        await recorder.consume(memoryview(packet))
        snapshot = await recorder.finalize()

        assert snapshot.state == RecordingState.FINALIZED
        assert snapshot.packets_written == 1
        assert snapshot.bytes_written == len(packet)
        assert (tmp_path / "integrated.rtp").read_bytes() == packet

    asyncio.run(exercise())


def test_recorder_abort_deletes_local_partial_payload(tmp_path: Path) -> None:
    async def exercise() -> None:
        sink = AtomicLocalRecordingSink(tmp_path, "cleanup", max_bytes=4096)
        recorder = BoundedRtpRecorder(sink, max_packets=1, max_bytes=4096)
        packet = _rtp_packet()

        await recorder.start()
        await recorder.consume(memoryview(packet))
        await recorder.abort()

        assert not (tmp_path / ".cleanup.part").exists()
        assert not (tmp_path / "cleanup.rtp").exists()

    asyncio.run(exercise())
