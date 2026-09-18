from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from k5vision.media.framed_recording import FramedAtomicRecordingSink
from k5vision.media.playback_schedule import PlaybackRate
from k5vision.media.presentation_frame import PixelFormat, PresentationVideoFrame
from k5vision.media.presentation_playback import (
    BoundedPresentationPlaybackDelivery,
    PresentationPlaybackError,
    PresentationPlaybackErrorCode,
    PresentationPlaybackState,
)
from k5vision.media.recording_descriptor import RecordingStreamDescriptor, VideoCodec

_RECORDING_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
_SOURCE_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
_START = datetime(2026, 9, 18, 1, 0, 0, tzinfo=UTC)


def _rtp(*, sequence: int, timestamp: int, payload: bytes = b"x") -> bytes:
    return bytes(
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
    ) + payload


def _packets() -> list[bytes]:
    return [
        _rtp(sequence=1, timestamp=123_456, payload=b"a"),
        _rtp(sequence=2, timestamp=132_456, payload=b"b"),
    ]


def _descriptor(packets: list[bytes], *, codec: VideoCodec = VideoCodec.H264) -> RecordingStreamDescriptor:
    return RecordingStreamDescriptor(
        recording_id=_RECORDING_ID,
        source_id=_SOURCE_ID,
        codec=codec,
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


def _write_recording(tmp_path: Path, packets: list[bytes]) -> Path:
    async def write() -> None:
        sink = FramedAtomicRecordingSink(tmp_path, "presentation-playback")
        await sink.open()
        for packet in packets:
            await sink.write(memoryview(packet))
        await sink.finalize()

    asyncio.run(write())
    return tmp_path / "presentation-playback.k5r"


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.now += delay


class FakeDecoder:
    def __init__(self, *, fail: bool = False, close_fail: bool = False) -> None:
        self.fail = fail
        self.close_fail = close_fail
        self.closed = False

    async def decode(
        self,
        _packet: memoryview,
        source_elapsed_ms: int,
    ) -> list[PresentationVideoFrame]:
        if self.fail:
            raise RuntimeError("SECRET decoder detail")
        return [
            PresentationVideoFrame(
                payload=memoryview(b"abcd"),
                width=1,
                height=1,
                stride_bytes=4,
                pixel_format=PixelFormat.BGRX,
                source_elapsed_ms=source_elapsed_ms,
            )
        ]

    async def flush(self) -> list[PresentationVideoFrame]:
        return []

    async def close(self) -> None:
        self.closed = True
        if self.close_fail:
            raise RuntimeError("SECRET cleanup detail")


def _delivery(
    tmp_path: Path,
    decoder: FakeDecoder,
    **kwargs: object,
) -> BoundedPresentationPlaybackDelivery:
    packets = _packets()
    path = _write_recording(tmp_path, packets)
    clock = FakeClock()
    return BoundedPresentationPlaybackDelivery(
        path,
        _descriptor(packets),
        0,
        100,
        PlaybackRate.NORMAL,
        decoder_factory=lambda _payload_type: decoder,
        clock=clock.monotonic,
        sleep=clock.sleep,
        **kwargs,
    )


def test_delivery_emits_validated_frames_and_source_free_snapshot(tmp_path: Path) -> None:
    decoder = FakeDecoder()
    delivery = _delivery(tmp_path, decoder)
    observed: list[tuple[bytes, int, int, int, int]] = []

    async def consumer(frame: PresentationVideoFrame) -> None:
        observed.append(
            (
                bytes(frame.payload),
                frame.source_elapsed_ms,
                frame.width,
                frame.height,
                frame.stride_bytes,
            )
        )

    snapshot = asyncio.run(delivery.run(consumer))

    assert observed == [(b"abcd", 0, 1, 1, 4), (b"abcd", 100, 1, 1, 4)]
    assert snapshot.state == PresentationPlaybackState.COMPLETE
    assert snapshot.decoder_initialized is True
    assert snapshot.delivered_frames == 2
    assert snapshot.delivered_bytes == 8
    assert snapshot.pump_delivered_packets == 2
    assert snapshot.descriptor_verified is True
    assert decoder.closed is True
    payload = snapshot.model_dump_json()
    assert str(tmp_path) not in payload
    assert "recording_id" not in payload
    assert "source_id" not in payload
    assert "abcd" not in payload


def test_decoder_failure_is_sanitized_and_closes_decoder(tmp_path: Path) -> None:
    decoder = FakeDecoder(fail=True)
    delivery = _delivery(tmp_path, decoder)

    async def consumer(_frame: PresentationVideoFrame) -> None:
        return None

    with pytest.raises(PresentationPlaybackError) as exc:
        asyncio.run(delivery.run(consumer))

    assert exc.value.code == PresentationPlaybackErrorCode.DECODER_FAILURE
    assert "SECRET" not in str(exc.value)
    assert delivery.snapshot.state == PresentationPlaybackState.FAILED
    assert decoder.closed is True


def test_consumer_failure_is_sanitized(tmp_path: Path) -> None:
    decoder = FakeDecoder()
    delivery = _delivery(tmp_path, decoder)

    async def consumer(_frame: PresentationVideoFrame) -> None:
        raise RuntimeError(f"SECRET consumer detail {tmp_path}")

    with pytest.raises(PresentationPlaybackError) as exc:
        asyncio.run(delivery.run(consumer))

    assert exc.value.code == PresentationPlaybackErrorCode.CONSUMER_FAILURE
    assert "SECRET" not in str(exc.value)
    assert str(tmp_path) not in str(exc.value)
    assert delivery.snapshot.state == PresentationPlaybackState.FAILED


def test_consumer_timeout_is_bounded(tmp_path: Path) -> None:
    decoder = FakeDecoder()
    delivery = _delivery(tmp_path, decoder, frame_consumer_timeout_seconds=0.001)

    async def consumer(_frame: PresentationVideoFrame) -> None:
        await asyncio.sleep(1)

    with pytest.raises(PresentationPlaybackError) as exc:
        asyncio.run(delivery.run(consumer))

    assert exc.value.code == PresentationPlaybackErrorCode.CONSUMER_TIMEOUT
    assert delivery.snapshot.state == PresentationPlaybackState.FAILED


def test_cleanup_failure_fails_closed_when_primary_path_succeeds(tmp_path: Path) -> None:
    decoder = FakeDecoder(close_fail=True)
    delivery = _delivery(tmp_path, decoder)

    async def consumer(_frame: PresentationVideoFrame) -> None:
        return None

    with pytest.raises(PresentationPlaybackError) as exc:
        asyncio.run(delivery.run(consumer))

    assert exc.value.code == PresentationPlaybackErrorCode.CLEANUP_FAILURE
    assert "SECRET" not in str(exc.value)
    assert delivery.snapshot.state == PresentationPlaybackState.FAILED


def test_single_use_and_constructor_bounds_fail_closed(tmp_path: Path) -> None:
    decoder = FakeDecoder()
    delivery = _delivery(tmp_path, decoder)

    async def consumer(_frame: PresentationVideoFrame) -> None:
        return None

    asyncio.run(delivery.run(consumer))
    with pytest.raises(PresentationPlaybackError) as exc:
        asyncio.run(delivery.run(consumer))
    assert exc.value.code == PresentationPlaybackErrorCode.INVALID_STATE

    packets = _packets()
    path = tmp_path / "unused.k5r"
    with pytest.raises(PresentationPlaybackError) as unsupported:
        BoundedPresentationPlaybackDelivery(
            path,
            _descriptor(packets, codec=VideoCodec.H265),
            0,
            100,
        )
    assert unsupported.value.code == PresentationPlaybackErrorCode.UNSUPPORTED_CODEC
    with pytest.raises(ValueError):
        BoundedPresentationPlaybackDelivery(
            path,
            _descriptor(packets),
            0,
            100,
            max_frames=0,
        )
    with pytest.raises(TypeError):
        BoundedPresentationPlaybackDelivery(
            path,
            _descriptor(packets),
            0,
            100,
            decoder_factory=0,  # type: ignore[arg-type]
        )
