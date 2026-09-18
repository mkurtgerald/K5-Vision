from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from k5vision.media.framed_recording import FramedAtomicRecordingSink
from k5vision.media.playback_navigation import PlaybackNavigationErrorCode
from k5vision.media.playback_window import (
    BoundedPlaybackWindow,
    PlaybackWindowError,
    PlaybackWindowErrorCode,
    PlaybackWindowState,
)
from k5vision.media.recording_descriptor import RecordingStreamDescriptor, VideoCodec

_RECORDING_ID = UUID("99999999-9999-4999-8999-999999999999")
_SOURCE_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
_START = datetime(2026, 9, 17, 23, 0, 0, tzinfo=UTC)


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
        _rtp(b"b", sequence=2, timestamp=126_456),
        _rtp(b"c", sequence=3, timestamp=129_456),
        _rtp(b"d", sequence=4, timestamp=132_456),
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


def test_inclusive_window_emits_only_requested_range_and_verifies(tmp_path: Path) -> None:
    packets = _packets()
    path = _write_recording(tmp_path, "bounded-window", packets)
    window = BoundedPlaybackWindow(path, _descriptor(packets), 33, 66)

    emitted = list(window.iter_packets())

    assert [item.elapsed_ms for item in emitted] == [33, 66]
    assert [item.packet for item in emitted] == packets[1:3]
    assert window.snapshot.state == PlaybackWindowState.COMPLETE
    assert window.snapshot.scanned_packets == 3
    assert window.snapshot.emitted_packets == 2
    assert window.snapshot.first_emitted_ms == 33
    assert window.snapshot.last_emitted_ms == 66
    assert window.snapshot.descriptor_verified is True


def test_between_packet_bounds_are_deterministic(tmp_path: Path) -> None:
    packets = _packets()
    path = _write_recording(tmp_path, "between-window", packets)
    window = BoundedPlaybackWindow(path, _descriptor(packets), 34, 90)

    emitted = list(window.iter_packets())

    assert [item.elapsed_ms for item in emitted] == [66]
    assert window.snapshot.scanned_packets == 2
    assert window.snapshot.first_emitted_ms == 66
    assert window.snapshot.last_emitted_ms == 66
    assert window.snapshot.descriptor_verified is True


def test_empty_window_after_last_packet_still_verifies_recording(tmp_path: Path) -> None:
    packets = _packets()
    path = _write_recording(tmp_path, "empty-window", packets)
    window = BoundedPlaybackWindow(path, _descriptor(packets), 999, 1000)

    assert list(window.iter_packets()) == []
    assert window.snapshot.state == PlaybackWindowState.COMPLETE
    assert window.snapshot.scanned_packets == 0
    assert window.snapshot.emitted_packets == 0
    assert window.snapshot.first_emitted_ms is None
    assert window.snapshot.last_emitted_ms is None
    assert window.snapshot.descriptor_verified is True


def test_invalid_window_is_rejected_before_read(tmp_path: Path) -> None:
    descriptor = _descriptor(_packets())
    path = tmp_path / "unused.k5r"

    with pytest.raises(ValueError):
        BoundedPlaybackWindow(path, descriptor, -1, 10)
    with pytest.raises(ValueError):
        BoundedPlaybackWindow(path, descriptor, 20, 10)
    with pytest.raises(ValueError):
        BoundedPlaybackWindow(path, descriptor, 0, 1001)
    with pytest.raises(TypeError):
        BoundedPlaybackWindow(path, descriptor, True, 10)
    with pytest.raises(TypeError):
        BoundedPlaybackWindow(path, descriptor, 0, False)


def test_packet_limit_is_enforced_before_read(tmp_path: Path) -> None:
    descriptor = _descriptor(_packets())
    path = tmp_path / "unused.k5r"

    with pytest.raises(ValueError):
        BoundedPlaybackWindow(path, descriptor, 0, 10, max_packets=3)


def test_navigation_failure_is_sanitized(tmp_path: Path) -> None:
    packets = _packets()
    secret_path = tmp_path / "SECRET-recording.k5r"
    window = BoundedPlaybackWindow(secret_path, _descriptor(packets), 0, 100)

    with pytest.raises(PlaybackWindowError) as exc:
        list(window.iter_packets())

    assert exc.value.code == PlaybackWindowErrorCode.NAVIGATION_FAILURE
    assert exc.value.navigation_error_code == PlaybackNavigationErrorCode.TIMELINE_FAILURE
    assert "SECRET" not in str(exc.value)
    assert str(tmp_path) not in str(exc.value)
    assert window.snapshot.state == PlaybackWindowState.FAILED


def test_consumer_abort_marks_window_aborted(tmp_path: Path) -> None:
    packets = _packets()
    path = _write_recording(tmp_path, "abort-window", packets)
    window = BoundedPlaybackWindow(path, _descriptor(packets), 0, 100)
    iterator = window.iter_packets()

    assert next(iterator).elapsed_ms == 0
    iterator.close()

    assert window.snapshot.state == PlaybackWindowState.ABORTED
    assert window.snapshot.emitted_packets == 1
    assert window.snapshot.descriptor_verified is False


def test_completed_window_cannot_be_reused(tmp_path: Path) -> None:
    packets = _packets()
    path = _write_recording(tmp_path, "single-use-window", packets)
    window = BoundedPlaybackWindow(path, _descriptor(packets), 0, 100)

    assert len(list(window.iter_packets())) == 4
    with pytest.raises(PlaybackWindowError) as exc:
        list(window.iter_packets())

    assert exc.value.code == PlaybackWindowErrorCode.INVALID_STATE


def test_snapshot_is_source_path_identifier_and_payload_free(tmp_path: Path) -> None:
    packets = [_rtp(b"SECRET_MEDIA")]
    path = _write_recording(tmp_path, "window-snapshot", packets)
    window = BoundedPlaybackWindow(path, _descriptor(packets), 0, 0)

    list(window.iter_packets())
    payload = window.snapshot.model_dump_json()

    assert "SECRET_MEDIA" not in payload
    assert str(tmp_path) not in payload
    assert "source_id" not in payload
    assert "recording_id" not in payload
