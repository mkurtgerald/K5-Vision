from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from k5vision.media.framed_recording import FramedAtomicRecordingSink
from k5vision.media.playback_decode import (
    BoundedPlaybackDecodeBridge,
    DecodedVideoFrame,
    PlaybackDecodeError,
    PlaybackDecodeErrorCode,
    PlaybackDecodeState,
)
from k5vision.media.recording_descriptor import RecordingStreamDescriptor, VideoCodec

_RECORDING_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
_SOURCE_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
_START = datetime(2026, 9, 18, 2, 0, 0, tzinfo=UTC)


def _rtp(payload: bytes = b"x", *, sequence: int = 1) -> bytes:
    timestamp = 123_456
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


class FakeDecoder:
    def __init__(self) -> None:
        self.closed = False
        self.flush_frames: list[DecodedVideoFrame] = []
        self.fail_decode = False
        self.fail_flush = False
        self.fail_close = False
        self.block_decode = False
        self.frames_per_packet = 1
        self.payload = b"decoded"

    async def decode(
        self,
        _packet: memoryview,
        source_elapsed_ms: int,
    ) -> list[DecodedVideoFrame]:
        if self.block_decode:
            await asyncio.sleep(10)
        if self.fail_decode:
            raise RuntimeError("rtsp://user:secret@192.0.2.44/private")
        return [
            DecodedVideoFrame(memoryview(self.payload), source_elapsed_ms)
            for _ in range(self.frames_per_packet)
        ]

    async def flush(self) -> list[DecodedVideoFrame]:
        if self.fail_flush:
            raise RuntimeError("private decoder detail")
        return self.flush_frames

    async def close(self) -> None:
        self.closed = True
        if self.fail_close:
            raise RuntimeError("private cleanup path C:/camera")


def _bridge(
    tmp_path: Path,
    decoder: FakeDecoder,
    *,
    recording_id: str = "decode-boundary",
    **kwargs: object,
) -> BoundedPlaybackDecodeBridge:
    packets = _packets()
    path = _write_recording(tmp_path, recording_id, packets)
    return BoundedPlaybackDecodeBridge(
        path,
        _descriptor(packets),
        0,
        0,
        decoder,
        **kwargs,
    )


def test_decode_bridge_delivers_transient_frames_and_flushes(tmp_path: Path) -> None:
    decoder = FakeDecoder()
    decoder.flush_frames = [DecodedVideoFrame(memoryview(b"flush"), 0)]
    bridge = _bridge(tmp_path, decoder)
    delivered: list[tuple[bytes, int]] = []

    async def consumer(frame: memoryview, source_elapsed_ms: int) -> None:
        delivered.append((bytes(frame), source_elapsed_ms))

    snapshot = asyncio.run(bridge.run(consumer))

    assert delivered == [(b"decoded", 0), (b"decoded", 0), (b"flush", 0)]
    assert snapshot.state == PlaybackDecodeState.COMPLETE
    assert snapshot.decoded_frames == 3
    assert snapshot.decoded_bytes == 19
    assert snapshot.pump_delivered_packets == 2
    assert snapshot.descriptor_verified is True
    assert decoder.closed is True
    serialized = str(snapshot.model_dump())
    assert "decoded" not in serialized
    assert "decode-boundary" not in serialized


def test_decoder_failure_is_sanitized_and_cleanup_runs(tmp_path: Path) -> None:
    decoder = FakeDecoder()
    decoder.fail_decode = True
    bridge = _bridge(tmp_path, decoder, recording_id="decoder-failure")

    async def consumer(_frame: memoryview, _source_elapsed_ms: int) -> None:
        return None

    with pytest.raises(PlaybackDecodeError) as caught:
        asyncio.run(bridge.run(consumer))

    assert caught.value.code == PlaybackDecodeErrorCode.DECODER_FAILURE
    assert "secret" not in str(caught.value)
    assert "192.0.2.44" not in str(caught.value)
    assert bridge.snapshot.state == PlaybackDecodeState.FAILED
    assert decoder.closed is True


def test_decoder_timeout_is_bounded(tmp_path: Path) -> None:
    decoder = FakeDecoder()
    decoder.block_decode = True
    bridge = _bridge(
        tmp_path,
        decoder,
        recording_id="decoder-timeout",
        decoder_timeout_seconds=0.01,
    )

    async def consumer(_frame: memoryview, _source_elapsed_ms: int) -> None:
        return None

    with pytest.raises(PlaybackDecodeError) as caught:
        asyncio.run(bridge.run(consumer))

    assert caught.value.code == PlaybackDecodeErrorCode.DECODER_TIMEOUT
    assert decoder.closed is True


def test_frame_consumer_failure_is_sanitized(tmp_path: Path) -> None:
    decoder = FakeDecoder()
    bridge = _bridge(tmp_path, decoder, recording_id="consumer-failure")

    async def consumer(_frame: memoryview, _source_elapsed_ms: int) -> None:
        raise RuntimeError("C:/private/output/frame.raw")

    with pytest.raises(PlaybackDecodeError) as caught:
        asyncio.run(bridge.run(consumer))

    assert caught.value.code == PlaybackDecodeErrorCode.CONSUMER_FAILURE
    assert "private" not in str(caught.value)
    assert decoder.closed is True


def test_frame_consumer_timeout_is_bounded(tmp_path: Path) -> None:
    decoder = FakeDecoder()
    bridge = _bridge(
        tmp_path,
        decoder,
        recording_id="consumer-timeout",
        frame_consumer_timeout_seconds=0.01,
    )

    async def consumer(_frame: memoryview, _source_elapsed_ms: int) -> None:
        await asyncio.sleep(10)

    with pytest.raises(PlaybackDecodeError) as caught:
        asyncio.run(bridge.run(consumer))

    assert caught.value.code == PlaybackDecodeErrorCode.CONSUMER_TIMEOUT
    assert decoder.closed is True


def test_per_packet_frame_limit_fails_closed(tmp_path: Path) -> None:
    decoder = FakeDecoder()
    decoder.frames_per_packet = 2
    bridge = _bridge(
        tmp_path,
        decoder,
        recording_id="per-packet-limit",
        max_frames_per_packet=1,
    )

    async def consumer(_frame: memoryview, _source_elapsed_ms: int) -> None:
        return None

    with pytest.raises(PlaybackDecodeError) as caught:
        asyncio.run(bridge.run(consumer))

    assert caught.value.code == PlaybackDecodeErrorCode.FRAME_LIMIT
    assert bridge.snapshot.decoded_frames == 0


def test_total_frame_limit_fails_closed(tmp_path: Path) -> None:
    decoder = FakeDecoder()
    bridge = _bridge(
        tmp_path,
        decoder,
        recording_id="total-limit",
        max_frames=1,
    )

    async def consumer(_frame: memoryview, _source_elapsed_ms: int) -> None:
        return None

    with pytest.raises(PlaybackDecodeError) as caught:
        asyncio.run(bridge.run(consumer))

    assert caught.value.code == PlaybackDecodeErrorCode.FRAME_LIMIT
    assert bridge.snapshot.decoded_frames == 1


def test_invalid_frame_size_fails_closed(tmp_path: Path) -> None:
    decoder = FakeDecoder()
    decoder.payload = b""
    bridge = _bridge(tmp_path, decoder, recording_id="invalid-size")

    async def consumer(_frame: memoryview, _source_elapsed_ms: int) -> None:
        return None

    with pytest.raises(PlaybackDecodeError) as caught:
        asyncio.run(bridge.run(consumer))

    assert caught.value.code == PlaybackDecodeErrorCode.INVALID_FRAME


def test_flush_failure_is_sanitized(tmp_path: Path) -> None:
    decoder = FakeDecoder()
    decoder.fail_flush = True
    bridge = _bridge(tmp_path, decoder, recording_id="flush-failure")

    async def consumer(_frame: memoryview, _source_elapsed_ms: int) -> None:
        return None

    with pytest.raises(PlaybackDecodeError) as caught:
        asyncio.run(bridge.run(consumer))

    assert caught.value.code == PlaybackDecodeErrorCode.DECODER_FAILURE
    assert "private" not in str(caught.value)
    assert decoder.closed is True


def test_cleanup_failure_fails_completed_run(tmp_path: Path) -> None:
    decoder = FakeDecoder()
    decoder.fail_close = True
    bridge = _bridge(tmp_path, decoder, recording_id="cleanup-failure")

    async def consumer(_frame: memoryview, _source_elapsed_ms: int) -> None:
        return None

    with pytest.raises(PlaybackDecodeError) as caught:
        asyncio.run(bridge.run(consumer))

    assert caught.value.code == PlaybackDecodeErrorCode.CLEANUP_FAILURE
    assert "C:/camera" not in str(caught.value)
    assert bridge.snapshot.state == PlaybackDecodeState.FAILED


def test_boundary_is_single_use(tmp_path: Path) -> None:
    decoder = FakeDecoder()
    bridge = _bridge(tmp_path, decoder, recording_id="single-use")

    async def consumer(_frame: memoryview, _source_elapsed_ms: int) -> None:
        return None

    asyncio.run(bridge.run(consumer))
    with pytest.raises(PlaybackDecodeError) as caught:
        asyncio.run(bridge.run(consumer))
    assert caught.value.code == PlaybackDecodeErrorCode.INVALID_STATE


def test_cancellation_sets_state_and_closes_decoder(tmp_path: Path) -> None:
    decoder = FakeDecoder()
    decoder.block_decode = True
    bridge = _bridge(
        tmp_path,
        decoder,
        recording_id="cancelled",
        decoder_timeout_seconds=10,
    )

    async def consumer(_frame: memoryview, _source_elapsed_ms: int) -> None:
        return None

    async def exercise() -> None:
        task = asyncio.create_task(bridge.run(consumer))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())
    assert bridge.snapshot.state == PlaybackDecodeState.CANCELLED
    assert decoder.closed is True


def test_invalid_decoder_contract_is_rejected(tmp_path: Path) -> None:
    packets = _packets()
    path = _write_recording(tmp_path, "bad-decoder", packets)
    with pytest.raises(PlaybackDecodeError) as caught:
        BoundedPlaybackDecodeBridge(path, _descriptor(packets), 0, 0, object())  # type: ignore[arg-type]
    assert caught.value.code == PlaybackDecodeErrorCode.INVALID_DECODER


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"max_frames": 0}, "max_frames"),
        ({"max_frames_per_packet": 0}, "max_frames_per_packet"),
        ({"max_frame_bytes": 0}, "max_frame_bytes"),
        ({"decoder_timeout_seconds": 0}, "decoder_timeout_seconds"),
        ({"frame_consumer_timeout_seconds": 11}, "frame_consumer_timeout_seconds"),
        ({"pump_consumer_timeout_seconds": 0}, "pump_consumer_timeout_seconds"),
        ({"cleanup_timeout_seconds": 11}, "cleanup_timeout_seconds"),
    ],
)
def test_constructor_bounds_fail_closed(tmp_path: Path, kwargs: dict[str, object], message: str) -> None:
    decoder = FakeDecoder()
    packets = _packets()
    path = _write_recording(tmp_path, f"bounds-{message}", packets)
    with pytest.raises(ValueError, match=message):
        BoundedPlaybackDecodeBridge(path, _descriptor(packets), 0, 0, decoder, **kwargs)
