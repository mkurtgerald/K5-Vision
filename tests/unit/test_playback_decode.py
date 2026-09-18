from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from k5vision.media.framed_recording import FramedAtomicRecordingSink
from k5vision.media.playback_decode import (
    BoundedDecodedPlayback,
    DecodedPlaybackError,
    DecodedPlaybackErrorCode,
    DecodedPlaybackState,
    DecodedVideoFrame,
    PixelFormat,
)
from k5vision.media.playback_pump import PlaybackPumpErrorCode
from k5vision.media.playback_schedule import PlaybackRate
from k5vision.media.recording_descriptor import RecordingStreamDescriptor, VideoCodec

_RECORDING_ID = UUID("11111111-2222-4333-8444-555555555555")
_SOURCE_ID = UUID("66666666-7777-4888-8999-aaaaaaaaaaaa")
_START = datetime(2026, 9, 18, 2, 0, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.now += delay


class FakeDecoder:
    def __init__(
        self,
        *,
        frames_per_packet: int = 1,
        fail: bool = False,
        block: bool = False,
        invalid_batch: bool = False,
        close_fail: bool = False,
    ) -> None:
        self.frames_per_packet = frames_per_packet
        self.fail = fail
        self.block = block
        self.invalid_batch = invalid_batch
        self.close_fail = close_fail
        self.decode_calls = 0
        self.close_calls = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def decode(self, _packet: memoryview, _source_elapsed_ms: int):  # type: ignore[no-untyped-def]
        self.decode_calls += 1
        self.entered.set()
        if self.block:
            await self.release.wait()
        if self.fail:
            raise RuntimeError("SECRET decoder detail")
        if self.invalid_batch:
            return [self._frame()]  # deliberately not a tuple
        return tuple(self._frame() for _ in range(self.frames_per_packet))

    async def close(self) -> None:
        self.close_calls += 1
        if self.close_fail:
            raise RuntimeError("SECRET close detail")

    @staticmethod
    def _frame() -> DecodedVideoFrame:
        return DecodedVideoFrame(
            pixels=memoryview(b"FRAME"),
            width=2,
            height=1,
            stride=6,
            pixel_format=PixelFormat.RGB24,
        )


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


def _playback(
    path: Path,
    packets: list[bytes],
    decoder: FakeDecoder,
    clock: FakeClock,
    **kwargs: object,
) -> BoundedDecodedPlayback:
    return BoundedDecodedPlayback(
        path,
        _descriptor(packets),
        0,
        200,
        decoder,
        clock=clock.monotonic,
        sleep=clock.sleep,
        **kwargs,  # type: ignore[arg-type]
    )


def test_decode_boundary_delivers_ephemeral_frames_and_closes(tmp_path: Path) -> None:
    packets = _packets()
    path = _write_recording(tmp_path, "decode-success", packets)
    decoder = FakeDecoder()
    clock = FakeClock()
    delivered: list[tuple[bytes, int]] = []

    async def consumer(frame: DecodedVideoFrame, source_elapsed_ms: int) -> None:
        delivered.append((bytes(frame.pixels), source_elapsed_ms))

    playback = _playback(path, packets, decoder, clock)
    snapshot = asyncio.run(playback.run(consumer))

    assert delivered == [(b"FRAME", 0), (b"FRAME", 100), (b"FRAME", 200)]
    assert snapshot.state == DecodedPlaybackState.COMPLETE
    assert snapshot.packets_decoded == 3
    assert snapshot.frames_delivered == 3
    assert snapshot.frame_bytes_delivered == 15
    assert snapshot.max_width == 2
    assert snapshot.max_height == 1
    assert snapshot.decoder_closed is True
    assert snapshot.descriptor_verified is True
    assert decoder.close_calls == 1


def test_inherited_playback_rate_remains_observable(tmp_path: Path) -> None:
    packets = _packets()
    path = _write_recording(tmp_path, "decode-rate", packets)
    decoder = FakeDecoder()
    clock = FakeClock()

    async def consumer(_frame: DecodedVideoFrame, _source_elapsed_ms: int) -> None:
        return None

    playback = BoundedDecodedPlayback(
        path,
        _descriptor(packets),
        0,
        200,
        decoder,
        PlaybackRate.DOUBLE,
        clock=clock.monotonic,
        sleep=clock.sleep,
    )
    snapshot = asyncio.run(playback.run(consumer))

    assert snapshot.rate == PlaybackRate.DOUBLE
    assert snapshot.frames_delivered == 3


def test_frame_limit_fails_closed_and_still_closes_decoder(tmp_path: Path) -> None:
    packets = _packets()
    path = _write_recording(tmp_path, "decode-limit", packets)
    decoder = FakeDecoder(frames_per_packet=2)
    clock = FakeClock()

    async def consumer(_frame: DecodedVideoFrame, _source_elapsed_ms: int) -> None:
        return None

    playback = _playback(
        path,
        packets,
        decoder,
        clock,
        max_frames_per_packet=1,
    )
    with pytest.raises(DecodedPlaybackError) as exc:
        asyncio.run(playback.run(consumer))

    assert exc.value.code == DecodedPlaybackErrorCode.FRAME_LIMIT
    assert playback.snapshot.state == DecodedPlaybackState.FAILED
    assert playback.snapshot.decoder_closed is True
    assert decoder.close_calls == 1


def test_invalid_frame_batch_fails_closed(tmp_path: Path) -> None:
    packets = _packets()
    path = _write_recording(tmp_path, "decode-invalid-batch", packets)
    decoder = FakeDecoder(invalid_batch=True)
    clock = FakeClock()

    async def consumer(_frame: DecodedVideoFrame, _source_elapsed_ms: int) -> None:
        return None

    playback = _playback(path, packets, decoder, clock)
    with pytest.raises(DecodedPlaybackError) as exc:
        asyncio.run(playback.run(consumer))

    assert exc.value.code == DecodedPlaybackErrorCode.FRAME_BATCH_INVALID
    assert playback.snapshot.decoder_closed is True


def test_decoder_failure_is_sanitized(tmp_path: Path) -> None:
    packets = _packets()
    path = _write_recording(tmp_path, "decode-failure", packets)
    decoder = FakeDecoder(fail=True)
    clock = FakeClock()

    async def consumer(_frame: DecodedVideoFrame, _source_elapsed_ms: int) -> None:
        return None

    playback = _playback(path, packets, decoder, clock)
    with pytest.raises(DecodedPlaybackError) as exc:
        asyncio.run(playback.run(consumer))

    assert exc.value.code == DecodedPlaybackErrorCode.DECODER_FAILURE
    assert "SECRET" not in str(exc.value)
    assert str(tmp_path) not in str(exc.value)
    assert playback.snapshot.decoder_closed is True


def test_decoder_timeout_is_bounded(tmp_path: Path) -> None:
    packets = _packets()
    path = _write_recording(tmp_path, "decode-timeout", packets)
    decoder = FakeDecoder(block=True)
    clock = FakeClock()

    async def consumer(_frame: DecodedVideoFrame, _source_elapsed_ms: int) -> None:
        return None

    playback = _playback(
        path,
        packets,
        decoder,
        clock,
        decoder_timeout_seconds=0.001,
    )
    with pytest.raises(DecodedPlaybackError) as exc:
        asyncio.run(playback.run(consumer))

    assert exc.value.code == DecodedPlaybackErrorCode.DECODER_TIMEOUT
    assert playback.snapshot.decoder_closed is True


def test_frame_consumer_failure_is_sanitized(tmp_path: Path) -> None:
    packets = _packets()
    path = _write_recording(tmp_path, "consumer-failure", packets)
    decoder = FakeDecoder()
    clock = FakeClock()

    async def consumer(_frame: DecodedVideoFrame, _source_elapsed_ms: int) -> None:
        raise RuntimeError(f"SECRET frame detail {tmp_path}")

    playback = _playback(path, packets, decoder, clock)
    with pytest.raises(DecodedPlaybackError) as exc:
        asyncio.run(playback.run(consumer))

    assert exc.value.code == DecodedPlaybackErrorCode.FRAME_CONSUMER_FAILURE
    assert "SECRET" not in str(exc.value)
    assert str(tmp_path) not in str(exc.value)
    assert playback.snapshot.decoder_closed is True


def test_frame_consumer_timeout_is_bounded(tmp_path: Path) -> None:
    packets = _packets()
    path = _write_recording(tmp_path, "consumer-timeout", packets)
    decoder = FakeDecoder()
    clock = FakeClock()

    async def consumer(_frame: DecodedVideoFrame, _source_elapsed_ms: int) -> None:
        await asyncio.sleep(1)

    playback = _playback(
        path,
        packets,
        decoder,
        clock,
        frame_consumer_timeout_seconds=0.001,
    )
    with pytest.raises(DecodedPlaybackError) as exc:
        asyncio.run(playback.run(consumer))

    assert exc.value.code == DecodedPlaybackErrorCode.FRAME_CONSUMER_TIMEOUT
    assert playback.snapshot.decoder_closed is True


def test_missing_recording_becomes_sanitized_pump_failure(tmp_path: Path) -> None:
    packets = _packets()
    path = tmp_path / "SECRET-missing.k5r"
    decoder = FakeDecoder()
    clock = FakeClock()

    async def consumer(_frame: DecodedVideoFrame, _source_elapsed_ms: int) -> None:
        return None

    playback = _playback(path, packets, decoder, clock)
    with pytest.raises(DecodedPlaybackError) as exc:
        asyncio.run(playback.run(consumer))

    assert exc.value.code == DecodedPlaybackErrorCode.PUMP_FAILURE
    assert exc.value.pump_error_code == PlaybackPumpErrorCode.SCHEDULE_FAILURE
    assert "SECRET" not in str(exc.value)
    assert str(tmp_path) not in str(exc.value)
    assert playback.snapshot.decoder_closed is True


def test_decoder_close_failure_fails_successful_session(tmp_path: Path) -> None:
    packets = _packets()
    path = _write_recording(tmp_path, "close-failure", packets)
    decoder = FakeDecoder(close_fail=True)
    clock = FakeClock()

    async def consumer(_frame: DecodedVideoFrame, _source_elapsed_ms: int) -> None:
        return None

    playback = _playback(path, packets, decoder, clock)
    with pytest.raises(DecodedPlaybackError) as exc:
        asyncio.run(playback.run(consumer))

    assert exc.value.code == DecodedPlaybackErrorCode.DECODER_CLOSE_FAILURE
    assert "SECRET" not in str(exc.value)
    assert playback.snapshot.state == DecodedPlaybackState.FAILED
    assert playback.snapshot.decoder_closed is False


def test_cancellation_propagates_and_decoder_is_closed(tmp_path: Path) -> None:
    packets = _packets()
    path = _write_recording(tmp_path, "decode-cancel", packets)

    async def exercise() -> BoundedDecodedPlayback:
        decoder = FakeDecoder(block=True)
        clock = FakeClock()
        playback = _playback(path, packets, decoder, clock)

        async def consumer(_frame: DecodedVideoFrame, _source_elapsed_ms: int) -> None:
            return None

        task = asyncio.create_task(playback.run(consumer))
        await decoder.entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert decoder.close_calls == 1
        return playback

    playback = asyncio.run(exercise())
    assert playback.snapshot.state == DecodedPlaybackState.CANCELLED
    assert playback.snapshot.decoder_closed is True


def test_completed_decode_session_cannot_be_reused(tmp_path: Path) -> None:
    packets = _packets()
    path = _write_recording(tmp_path, "decode-single-use", packets)
    decoder = FakeDecoder()
    clock = FakeClock()

    async def consumer(_frame: DecodedVideoFrame, _source_elapsed_ms: int) -> None:
        return None

    playback = _playback(path, packets, decoder, clock)
    asyncio.run(playback.run(consumer))

    with pytest.raises(DecodedPlaybackError) as exc:
        asyncio.run(playback.run(consumer))

    assert exc.value.code == DecodedPlaybackErrorCode.INVALID_STATE


def test_frame_contract_validates_bounds() -> None:
    with pytest.raises(TypeError):
        DecodedVideoFrame(b"x", 1, 1, 1, PixelFormat.RGB24)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        DecodedVideoFrame(memoryview(b""), 1, 1, 1, PixelFormat.RGB24)
    with pytest.raises(ValueError):
        DecodedVideoFrame(memoryview(b"x"), 0, 1, 1, PixelFormat.RGB24)
    with pytest.raises(ValueError):
        DecodedVideoFrame(memoryview(b"x"), 1, 0, 1, PixelFormat.RGB24)
    with pytest.raises(ValueError):
        DecodedVideoFrame(memoryview(b"x"), 1, 1, 0, PixelFormat.RGB24)
    with pytest.raises(TypeError):
        DecodedVideoFrame(memoryview(b"x"), 1, 1, 1, "rgb24")  # type: ignore[arg-type]


def test_constructor_bounds_fail_closed(tmp_path: Path) -> None:
    packets = _packets()
    descriptor = _descriptor(packets)
    decoder = FakeDecoder()
    path = tmp_path / "unused.k5r"

    with pytest.raises(ValueError):
        BoundedDecodedPlayback(path, descriptor, 0, 100, decoder, max_frames_per_packet=0)
    with pytest.raises(ValueError):
        BoundedDecodedPlayback(path, descriptor, 0, 100, decoder, decoder_timeout_seconds=0)
    with pytest.raises(ValueError):
        BoundedDecodedPlayback(
            path,
            descriptor,
            0,
            100,
            decoder,
            frame_consumer_timeout_seconds=0,
        )
    with pytest.raises(ValueError):
        BoundedDecodedPlayback(
            path,
            descriptor,
            0,
            100,
            decoder,
            decoder_close_timeout_seconds=0,
        )
    with pytest.raises(ValueError):
        BoundedDecodedPlayback(
            path,
            descriptor,
            0,
            100,
            decoder,
            decoder_timeout_seconds=2,
            frame_consumer_timeout_seconds=2,
        )
    with pytest.raises(TypeError):
        BoundedDecodedPlayback(path, descriptor, 0, 100, object())  # type: ignore[arg-type]


def test_snapshot_is_source_path_identifier_and_payload_free(tmp_path: Path) -> None:
    packets = [_rtp(b"SECRET_MEDIA")]
    path = _write_recording(tmp_path, "decode-snapshot", packets)
    decoder = FakeDecoder()
    clock = FakeClock()

    async def consumer(_frame: DecodedVideoFrame, _source_elapsed_ms: int) -> None:
        return None

    playback = BoundedDecodedPlayback(
        path,
        _descriptor(packets),
        0,
        0,
        decoder,
        clock=clock.monotonic,
        sleep=clock.sleep,
    )
    asyncio.run(playback.run(consumer))
    payload = playback.snapshot.model_dump_json()

    assert "SECRET_MEDIA" not in payload
    assert "FRAME" not in payload
    assert str(tmp_path) not in payload
    assert "source_id" not in payload
    assert "recording_id" not in payload
