from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from k5vision.media.framed_recording import FramedAtomicRecordingSink
from k5vision.media.playback_decode import DecodedVideoFrame
from k5vision.media.playback_session import (
    BoundedPlaybackSession,
    PlaybackSessionError,
    PlaybackSessionErrorCode,
    PlaybackSessionState,
)
from k5vision.media.recording_descriptor import RecordingStreamDescriptor, VideoCodec

_RECORDING_ID = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
_SOURCE_ID = UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
_START = datetime(2026, 9, 18, 3, 0, 0, tzinfo=UTC)


def _rtp(payload: bytes = b"x", *, sequence: int = 1) -> bytes:
    timestamp = 321_000
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
    return [_rtp(b"a", sequence=1), _rtp(b"b", sequence=2)]


def _descriptor(
    packets: list[bytes], *, codec: VideoCodec = VideoCodec.H264
) -> RecordingStreamDescriptor:
    return RecordingStreamDescriptor(
        recording_id=_RECORDING_ID,
        source_id=_SOURCE_ID,
        codec=codec,
        payload_type=96,
        clock_rate_hz=90_000,
        started_at_utc=_START,
        ended_at_utc=_START + timedelta(seconds=1),
        duration_ms=1000,
        rtp_timestamp_origin=321_000,
        packet_count=len(packets),
        payload_bytes=sum(len(packet) for packet in packets),
        file_bytes=8 + sum(8 + len(packet) for packet in packets),
    )


def _write_recording(tmp_path: Path, packets: list[bytes]) -> Path:
    async def exercise() -> None:
        sink = FramedAtomicRecordingSink(tmp_path, "session-boundary")
        await sink.open()
        for packet in packets:
            await sink.write(memoryview(packet))
        await sink.finalize()

    asyncio.run(exercise())
    return tmp_path / "session-boundary.k5r"


class FakeDecoder:
    def __init__(self) -> None:
        self.closed = False
        self.fail = False

    async def decode(
        self,
        _packet: memoryview,
        source_elapsed_ms: int,
    ) -> list[DecodedVideoFrame]:
        if self.fail:
            raise RuntimeError("rtsp://user:secret@192.0.2.10/private")
        return [DecodedVideoFrame(memoryview(b"frame"), source_elapsed_ms)]

    async def flush(self) -> list[DecodedVideoFrame]:
        return []

    async def close(self) -> None:
        self.closed = True


def test_session_composes_recording_to_transient_frames(tmp_path: Path) -> None:
    packets = _packets()
    path = _write_recording(tmp_path, packets)
    decoder = FakeDecoder()
    payload_types: list[int] = []

    def factory(payload_type: int) -> FakeDecoder:
        payload_types.append(payload_type)
        return decoder

    session = BoundedPlaybackSession(
        path,
        _descriptor(packets),
        0,
        0,
        decoder_factory=factory,
    )
    delivered: list[tuple[bytes, int]] = []

    async def consumer(frame: memoryview, source_elapsed_ms: int) -> None:
        delivered.append((bytes(frame), source_elapsed_ms))

    snapshot = asyncio.run(session.run(consumer))

    assert payload_types == [96]
    assert delivered == [(b"frame", 0), (b"frame", 0)]
    assert decoder.closed is True
    assert snapshot.state == PlaybackSessionState.COMPLETE
    assert snapshot.decoder_initialized is True
    assert snapshot.decoded_frames == 2
    assert snapshot.decoded_bytes == 10
    assert snapshot.delivered_packets == 2
    assert snapshot.descriptor_verified is True
    serialized = str(snapshot.model_dump())
    assert "session-boundary" not in serialized
    assert "b'frame'" not in serialized


def test_unsupported_codec_fails_before_decoder_initialization(tmp_path: Path) -> None:
    packets = _packets()
    path = _write_recording(tmp_path, packets)

    with pytest.raises(PlaybackSessionError) as caught:
        BoundedPlaybackSession(path, _descriptor(packets, codec=VideoCodec.H265), 0, 0)

    assert caught.value.code == PlaybackSessionErrorCode.UNSUPPORTED_CODEC
    assert "path" not in str(caught.value).lower()


def test_decoder_factory_failure_is_sanitized(tmp_path: Path) -> None:
    packets = _packets()
    path = _write_recording(tmp_path, packets)

    def factory(_payload_type: int) -> FakeDecoder:
        raise RuntimeError("C:/private/runtime secret")

    session = BoundedPlaybackSession(
        path,
        _descriptor(packets),
        0,
        0,
        decoder_factory=factory,
    )

    async def consumer(_frame: memoryview, _source_elapsed_ms: int) -> None:
        return None

    with pytest.raises(PlaybackSessionError) as caught:
        asyncio.run(session.run(consumer))

    assert caught.value.code == PlaybackSessionErrorCode.DECODER_INIT_FAILURE
    assert "private" not in str(caught.value)
    assert "secret" not in str(caught.value)
    assert session.snapshot.state == PlaybackSessionState.FAILED


def test_decode_failure_is_mapped_and_sanitized(tmp_path: Path) -> None:
    packets = _packets()
    path = _write_recording(tmp_path, packets)
    decoder = FakeDecoder()
    decoder.fail = True
    session = BoundedPlaybackSession(
        path,
        _descriptor(packets),
        0,
        0,
        decoder_factory=lambda _payload_type: decoder,
    )

    async def consumer(_frame: memoryview, _source_elapsed_ms: int) -> None:
        return None

    with pytest.raises(PlaybackSessionError) as caught:
        asyncio.run(session.run(consumer))

    assert caught.value.code == PlaybackSessionErrorCode.DECODE_FAILURE
    assert "secret" not in str(caught.value)
    assert "192.0.2.10" not in str(caught.value)
    assert decoder.closed is True
    assert session.snapshot.state == PlaybackSessionState.FAILED


def test_session_is_single_use(tmp_path: Path) -> None:
    packets = _packets()
    path = _write_recording(tmp_path, packets)
    session = BoundedPlaybackSession(
        path,
        _descriptor(packets),
        0,
        0,
        decoder_factory=lambda _payload_type: FakeDecoder(),
    )

    async def consumer(_frame: memoryview, _source_elapsed_ms: int) -> None:
        return None

    asyncio.run(session.run(consumer))
    with pytest.raises(PlaybackSessionError) as caught:
        asyncio.run(session.run(consumer))

    assert caught.value.code == PlaybackSessionErrorCode.INVALID_STATE


def test_invalid_composition_bounds_close_decoder(tmp_path: Path) -> None:
    packets = _packets()
    path = _write_recording(tmp_path, packets)
    decoder = FakeDecoder()
    session = BoundedPlaybackSession(
        path,
        _descriptor(packets),
        0,
        0,
        decoder_factory=lambda _payload_type: decoder,
        max_frames_per_packet=0,
    )

    async def consumer(_frame: memoryview, _source_elapsed_ms: int) -> None:
        return None

    with pytest.raises(PlaybackSessionError) as caught:
        asyncio.run(session.run(consumer))

    assert caught.value.code == PlaybackSessionErrorCode.COMPOSITION_FAILURE
    assert decoder.closed is True
    assert session.snapshot.state == PlaybackSessionState.FAILED
