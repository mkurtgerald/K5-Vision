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
        _rtp(sequence=1, timestamp=123_456, payload=b"a"),
        _rtp(sequence=2, timestamp=132_456, payload=b"b"),
    ]


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


class BatchPauseDecoder(FakeDecoder):
    async def decode(self, _packet, _source_elapsed_ms):
        return [self.frame(0), self.frame(40)]

    async def flush(self):
        return [self.frame(80)]

    @staticmethod
    def frame(ms):
        return PresentationVideoFrame(memoryview(b"abcd"), 1, 1, 4, PixelFormat.BGRX, ms)


def _pausable_delivery(tmp_path, control, decoder, **kwargs):
    from k5vision.media.pausable_presentation_playback import PausablePresentationPlaybackDelivery

    packets = _packets()[:1]
    path = _write_recording(tmp_path, packets)
    return PausablePresentationPlaybackDelivery(
        path,
        _descriptor(packets),
        0,
        1000,
        pause_control=control,
        decoder_factory=lambda _payload: decoder,
        **kwargs,
    )


def test_acknowledged_pause_fences_remaining_batch_and_flush_frames(tmp_path):
    from k5vision.media.playback_control import PlaybackPauseControl

    control, decoder = PlaybackPauseControl(), BatchPauseDecoder()
    delivery = _pausable_delivery(tmp_path, control, decoder)

    async def exercise():
        paused = asyncio.Event()
        frames = []

        async def consume(frame):
            frames.append(frame.source_elapsed_ms)
            if len(frames) == 1:
                assert (await control.pause()).state.value == "paused"
                paused.set()

        task = asyncio.create_task(delivery.run(consume))
        try:
            await asyncio.wait_for(paused.wait(), 1)
            for _ in range(10):
                await asyncio.sleep(0)
            assert frames == [0]
            assert not task.done()
            # A resume immediately superseded by pause cannot release stale waiters.
            await control.resume()
            await control.pause()
            for _ in range(5):
                await asyncio.sleep(0)
            assert frames == [0]
            await control.resume()
            result = await asyncio.wait_for(task, 1)
            assert frames == [0, 40, 80]
            assert result.delivered_frames == 3
            assert result.state == PresentationPlaybackState.COMPLETE
            assert decoder.closed
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(exercise())


def test_pause_past_default_packet_and_frame_deadlines_resumes_in_order(tmp_path):
    from k5vision.media.playback_control import PlaybackPauseControl

    control, decoder = PlaybackPauseControl(), BatchPauseDecoder()
    delivery = _pausable_delivery(tmp_path, control, decoder)

    async def exercise():
        paused = asyncio.Event()
        frames = []

        async def consume(frame):
            frames.append(frame.source_elapsed_ms)
            if len(frames) == 1:
                await control.pause()
                paused.set()

        task = asyncio.create_task(delivery.run(consume))
        try:
            await asyncio.wait_for(paused.wait(), 1)
            # Exceed the actual defaults: packet callback 8s, frame consumer 0.5s.
            await asyncio.sleep(8.05)
            assert frames == [0] and not task.done()
            assert delivery.snapshot.delivered_frames == 1
            await control.resume()
            result = await asyncio.wait_for(task, 1)
            assert frames == [0, 40, 80]
            assert result.delivered_frames == 3 and decoder.closed
            assert control.snapshot.paused_total_ms >= 8000
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(exercise())


@pytest.mark.parametrize("phase", ["decode", "flush"])
def test_pause_during_decoder_operation_fences_its_returned_frames(tmp_path, phase):
    from k5vision.media.playback_control import PlaybackPauseControl

    entered, release = asyncio.Event(), asyncio.Event()

    class Decoder(BatchPauseDecoder):
        async def decode(self, packet, elapsed):
            if phase == "decode":
                entered.set()
                await release.wait()
            return [self.frame(0)]

        async def flush(self):
            if phase == "flush":
                entered.set()
                await release.wait()
            return [self.frame(40)]

    control, decoder = PlaybackPauseControl(), Decoder()
    delivery = _pausable_delivery(
        tmp_path,
        control,
        decoder,
        pump_consumer_timeout_seconds=0.05,
        frame_consumer_timeout_seconds=0.025,
    )

    async def exercise():
        frames = []

        async def consume(frame):
            frames.append(frame.source_elapsed_ms)

        task = asyncio.create_task(delivery.run(consume))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await control.pause()
            expected = [] if phase == "decode" else [0]
            release.set()
            await asyncio.sleep(0.1)
            assert frames == expected and not task.done()
            await control.resume()
            result = await asyncio.wait_for(task, 1)
            assert frames == [0, 40]
            assert result.state == PresentationPlaybackState.COMPLETE
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            assert decoder.closed

    asyncio.run(exercise())


@pytest.mark.parametrize("phase", ["decode", "flush", "consumer"])
def test_pause_does_not_extend_already_started_work_timeout(tmp_path, phase):
    from k5vision.media.playback_control import PlaybackPauseControl

    entered, never = asyncio.Event(), asyncio.Event()

    class Decoder(BatchPauseDecoder):
        async def decode(self, packet, elapsed):
            if phase == "decode":
                entered.set()
                await never.wait()
            return [self.frame(0)]

        async def flush(self):
            if phase == "flush":
                entered.set()
                await never.wait()
            return []

    control, decoder = PlaybackPauseControl(), Decoder()
    delivery = _pausable_delivery(
        tmp_path,
        control,
        decoder,
        decoder_timeout_seconds=0.03,
        frame_consumer_timeout_seconds=0.03,
        pump_consumer_timeout_seconds=0.5,
    )

    async def exercise():
        async def consume(frame):
            if phase == "consumer":
                entered.set()
                await never.wait()

        task = asyncio.create_task(delivery.run(consume))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            await control.pause()
            with pytest.raises(PresentationPlaybackError) as error:
                await asyncio.wait_for(task, 1)
            assert error.value.code == (
                PresentationPlaybackErrorCode.CONSUMER_TIMEOUT
                if phase == "consumer"
                else PresentationPlaybackErrorCode.DECODER_TIMEOUT
            )
            assert delivery.snapshot.state == PresentationPlaybackState.FAILED
            assert decoder.closed
            assert "SECRET" not in str(error.value)
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(exercise())


@pytest.mark.parametrize("pause_at", [0, 40, 80])
def test_cancel_while_paused_at_batch_or_flush_admission_closes_decoder(tmp_path, pause_at):
    from k5vision.media.playback_control import PlaybackPauseControl

    class Decoder(BatchPauseDecoder):
        async def flush(self):
            return [self.frame(80), self.frame(120)]

    control, decoder = PlaybackPauseControl(), Decoder()
    delivery = _pausable_delivery(tmp_path, control, decoder)

    async def exercise():
        paused, frames = asyncio.Event(), []

        async def consume(frame):
            frames.append(frame.source_elapsed_ms)
            if frame.source_elapsed_ms == pause_at:
                await control.pause()
                paused.set()

        task = asyncio.create_task(delivery.run(consume))
        await asyncio.wait_for(paused.wait(), 1)
        for _ in range(5):
            await asyncio.sleep(0)
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert decoder.closed
        assert delivery.snapshot.state == PresentationPlaybackState.CANCELLED
        assert frames == [ms for ms in (0, 40, 80) if ms <= pause_at]
        assert delivery.snapshot.delivered_frames == len(frames)
        assert control._work_deadline.get() is None

    asyncio.run(exercise())


def test_pause_cannot_acknowledge_between_frame_admission_and_consumer_start(tmp_path, monkeypatch):
    from k5vision.media.playback_control import PlaybackPauseControl

    control, decoder = PlaybackPauseControl(), FakeDecoder()
    delivery = _pausable_delivery(tmp_path, control, decoder)

    async def exercise():
        admitted = delivery._wait_for_frame_admission
        pause_tasks, states_at_start = [], []

        async def admit_then_queue_pause():
            await admitted()
            # The pause will run before a newly scheduled consumer task. A direct
            # consumer await must start immediately in the admitted task instead.
            pause_tasks.append(asyncio.create_task(control.pause()))

        monkeypatch.setattr(delivery, "_wait_for_frame_admission", admit_then_queue_pause)

        async def consume(frame):
            states_at_start.append(control.snapshot.state.value)

        result = await delivery.run(consume)
        await asyncio.gather(*pause_tasks)
        assert states_at_start == ["running"]
        assert control.snapshot.state.value == "paused"
        assert result.delivered_frames == 1 and decoder.closed

    asyncio.run(exercise())
