from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from k5vision.media.framed_recording import FramedAtomicRecordingSink
from k5vision.media.playback_navigation import (
    BoundedPlaybackNavigator,
    PlaybackNavigationError,
    PlaybackNavigationErrorCode,
    PlaybackNavigationState,
)
from k5vision.media.playback_timeline import PlaybackTimelineErrorCode
from k5vision.media.recording_descriptor import RecordingStreamDescriptor, VideoCodec

_RECORDING_ID = UUID("77777777-7777-4777-8777-777777777777")
_SOURCE_ID = UUID("88888888-8888-4888-8888-888888888888")
_START = datetime(2026, 9, 17, 17, 0, 0, tzinfo=UTC)


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


def _packets() -> list[bytes]:
    return [
        _rtp(b"a", sequence=1, timestamp=123_456),
        _rtp(b"b", sequence=2, timestamp=126_456),
        _rtp(b"c", sequence=3, timestamp=129_456),
    ]


def test_zero_target_emits_full_timeline(tmp_path: Path) -> None:
    packets = _packets()
    path = _write_recording(tmp_path, "zero", packets)
    navigator = BoundedPlaybackNavigator(path, _descriptor(packets), 0)

    emitted = list(navigator.iter_packets())

    assert [item.packet for item in emitted] == packets
    assert [item.elapsed_ms for item in emitted] == [0, 33, 66]
    assert navigator.snapshot.positioned_at_ms == 0
    assert navigator.snapshot.scanned_packets == 3
    assert navigator.snapshot.emitted_packets == 3
    assert navigator.snapshot.state == PlaybackNavigationState.COMPLETE
    assert navigator.snapshot.descriptor_verified is True


def test_exact_target_starts_on_matching_packet(tmp_path: Path) -> None:
    packets = _packets()
    path = _write_recording(tmp_path, "exact", packets)
    navigator = BoundedPlaybackNavigator(path, _descriptor(packets), 33)

    emitted = list(navigator.iter_packets())

    assert [item.elapsed_ms for item in emitted] == [33, 66]
    assert navigator.snapshot.positioned_at_ms == 33
    assert navigator.snapshot.scanned_packets == 3
    assert navigator.snapshot.emitted_packets == 2


def test_between_packet_target_starts_on_next_packet(tmp_path: Path) -> None:
    packets = _packets()
    path = _write_recording(tmp_path, "between", packets)
    navigator = BoundedPlaybackNavigator(path, _descriptor(packets), 34)

    emitted = list(navigator.iter_packets())

    assert [item.elapsed_ms for item in emitted] == [66]
    assert navigator.snapshot.positioned_at_ms == 66
    assert navigator.snapshot.emitted_packets == 1


def test_target_after_last_packet_completes_without_emission(tmp_path: Path) -> None:
    packets = _packets()
    path = _write_recording(tmp_path, "after-last", packets)
    navigator = BoundedPlaybackNavigator(path, _descriptor(packets), 999)

    assert list(navigator.iter_packets()) == []
    assert navigator.snapshot.state == PlaybackNavigationState.COMPLETE
    assert navigator.snapshot.positioned_at_ms is None
    assert navigator.snapshot.scanned_packets == 3
    assert navigator.snapshot.emitted_packets == 0
    assert navigator.snapshot.descriptor_verified is True


def test_invalid_target_is_rejected_before_read(tmp_path: Path) -> None:
    descriptor = _descriptor(_packets())

    with pytest.raises(ValueError):
        BoundedPlaybackNavigator(tmp_path / "unused.k5r", descriptor, -1)
    with pytest.raises(ValueError):
        BoundedPlaybackNavigator(tmp_path / "unused.k5r", descriptor, 1001)
    with pytest.raises(TypeError):
        BoundedPlaybackNavigator(tmp_path / "unused.k5r", descriptor, True)


def test_underlying_timeline_failure_is_sanitized(tmp_path: Path) -> None:
    packets = _packets()
    secret_path = tmp_path / "SECRET-recording.k5r"
    navigator = BoundedPlaybackNavigator(secret_path, _descriptor(packets), 0)

    with pytest.raises(PlaybackNavigationError) as exc:
        list(navigator.iter_packets())

    assert exc.value.code == PlaybackNavigationErrorCode.TIMELINE_FAILURE
    assert exc.value.timeline_error_code == PlaybackTimelineErrorCode.PLAYBACK_FAILURE
    assert "SECRET" not in str(exc.value)
    assert str(tmp_path) not in str(exc.value)
    assert navigator.snapshot.state == PlaybackNavigationState.FAILED


def test_consumer_abort_marks_navigation_aborted(tmp_path: Path) -> None:
    packets = _packets()
    path = _write_recording(tmp_path, "abort-navigation", packets)
    navigator = BoundedPlaybackNavigator(path, _descriptor(packets), 0)
    iterator = navigator.iter_packets()

    assert next(iterator).elapsed_ms == 0
    iterator.close()

    assert navigator.snapshot.state == PlaybackNavigationState.ABORTED
    assert navigator.snapshot.emitted_packets == 1
    assert navigator.snapshot.descriptor_verified is False


def test_completed_navigator_cannot_be_reused(tmp_path: Path) -> None:
    packets = _packets()
    path = _write_recording(tmp_path, "single-use-navigation", packets)
    navigator = BoundedPlaybackNavigator(path, _descriptor(packets), 0)

    assert len(list(navigator.iter_packets())) == 3
    with pytest.raises(PlaybackNavigationError) as exc:
        list(navigator.iter_packets())

    assert exc.value.code == PlaybackNavigationErrorCode.INVALID_STATE


def test_snapshot_is_source_free_and_payload_free(tmp_path: Path) -> None:
    packets = [_rtp(b"SECRET_MEDIA")]
    path = _write_recording(tmp_path, "navigation-snapshot", packets)
    navigator = BoundedPlaybackNavigator(path, _descriptor(packets), 0)

    list(navigator.iter_packets())
    payload = navigator.snapshot.model_dump_json()

    assert "SECRET_MEDIA" not in payload
    assert str(tmp_path) not in payload
    assert "source_id" not in payload
    assert "recording_id" not in payload
