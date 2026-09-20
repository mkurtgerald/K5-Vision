from __future__ import annotations

import asyncio

import pytest

from k5vision.media.live_presentation import (
    LivePresentationError,
    LivePresentationErrorCode,
    LivePresentationSnapshot,
    LivePresentationState,
)
from k5vision.media.mixed_presentation import (
    BoundedMixedPresentation,
    MixedLiveStream,
    MixedPlaybackStream,
    MixedPresentationError,
    MixedPresentationErrorCode,
    MixedPresentationState,
)
from k5vision.media.presentation_frame import PixelFormat, PresentationVideoFrame
from k5vision.media.presentation_playback import (
    PresentationPlaybackError,
    PresentationPlaybackErrorCode,
    PresentationPlaybackSnapshot,
    PresentationPlaybackState,
)


def _frame(source_elapsed_ms: int, payload: bytes = b"abcd") -> PresentationVideoFrame:
    return PresentationVideoFrame(
        payload=memoryview(payload),
        width=1,
        height=1,
        stride_bytes=4,
        pixel_format=PixelFormat.BGRX,
        source_elapsed_ms=source_elapsed_ms,
    )


class FakeLiveDelivery:
    def __init__(
        self,
        frames: list[PresentationVideoFrame],
        *,
        fail: bool = False,
    ) -> None:
        self.frames = frames
        self.fail = fail
        self.cancelled = False

    async def run(self, source_uri: str, consumer: object) -> LivePresentationSnapshot:
        if self.fail:
            raise LivePresentationError(
                LivePresentationErrorCode.DELIVERY_FAILURE,
                f"SECRET child detail {source_uri}",
            )
        try:
            for frame in self.frames:
                await consumer(frame)  # type: ignore[operator]
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return LivePresentationSnapshot(
            state=LivePresentationState.COMPLETE,
            decoder_initialized=True,
            accepted_packets=max(1, len(self.frames)),
            rtp_valid_packets=max(1, len(self.frames)),
            rtp_invalid_packets=0,
            rtp_delivered_bytes=max(1, len(self.frames) * 13),
            delivered_frames=len(self.frames),
            delivered_frame_bytes=sum(len(frame.payload) for frame in self.frames),
            source_span_ms=max((frame.source_elapsed_ms for frame in self.frames), default=0),
        )


class FakePlaybackDelivery:
    def __init__(
        self,
        frames: list[PresentationVideoFrame],
        *,
        fail: bool = False,
    ) -> None:
        self.frames = frames
        self.fail = fail
        self.cancelled = False

    async def run(self, consumer: object) -> PresentationPlaybackSnapshot:
        if self.fail:
            raise PresentationPlaybackError(
                PresentationPlaybackErrorCode.PUMP_FAILURE,
                "SECRET playback detail",
            )
        try:
            for frame in self.frames:
                await consumer(frame)  # type: ignore[operator]
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return PresentationPlaybackSnapshot(
            state=PresentationPlaybackState.COMPLETE,
            decoder_initialized=True,
            delivered_frames=len(self.frames),
            delivered_bytes=sum(len(frame.payload) for frame in self.frames),
            source_span_ms=max((frame.source_elapsed_ms for frame in self.frames), default=0),
            pump_delivered_packets=max(1, len(self.frames)),
            late_packets=0,
            descriptor_verified=True,
        )


def _streams() -> list[MixedLiveStream | MixedPlaybackStream]:
    return [
        MixedLiveStream(
            0,
            "rtsp://user:SECRET@192.168.1.10/live",
            FakeLiveDelivery([_frame(0), _frame(100)]),
        ),
        MixedPlaybackStream(1, FakePlaybackDelivery([_frame(0), _frame(80)])),
    ]


def test_mixed_presentation_serializes_consumer_and_retains_aggregate_state() -> None:
    coordinator = BoundedMixedPresentation()
    observed: list[tuple[int, int]] = []
    active_consumers = 0
    max_active_consumers = 0

    async def consume(slot: int, frame: PresentationVideoFrame) -> None:
        nonlocal active_consumers, max_active_consumers
        active_consumers += 1
        max_active_consumers = max(max_active_consumers, active_consumers)
        await asyncio.sleep(0)
        observed.append((slot, frame.source_elapsed_ms))
        active_consumers -= 1

    snapshot = asyncio.run(coordinator.run(_streams(), consume))

    assert sorted(observed) == [(0, 0), (0, 100), (1, 0), (1, 80)]
    assert max_active_consumers == 1
    assert snapshot.state == MixedPresentationState.COMPLETE
    assert snapshot.stream_count == 2
    assert snapshot.live_streams == 1
    assert snapshot.playback_streams == 1
    assert snapshot.completed_streams == 2
    assert snapshot.delivered_frames == 4
    assert snapshot.delivered_frame_bytes == 16
    assert snapshot.max_source_span_ms == 100
    payload = snapshot.model_dump_json()
    assert "rtsp://" not in payload
    assert "192.168" not in payload
    assert "recording_id" not in payload
    assert "source_id" not in payload
    assert "SECRET" not in payload
    assert "abcd" not in payload


def test_mixed_streams_accept_full_arbitrary_viewport_slot_ceiling() -> None:
    coordinator = BoundedMixedPresentation()
    observed: list[int] = []

    async def consume(slot: int, _frame: PresentationVideoFrame) -> None:
        observed.append(slot)

    streams = [
        MixedLiveStream(4095, "rtsp://execution-only", FakeLiveDelivery([_frame(0)])),
        MixedPlaybackStream(7, FakePlaybackDelivery([_frame(0)])),
    ]
    snapshot = asyncio.run(coordinator.run(streams, consume))

    assert sorted(observed) == [7, 4095]
    assert snapshot.state == MixedPresentationState.COMPLETE
    assert snapshot.stream_count == 2
    assert "4095" not in snapshot.model_dump_json()
    assert "rtsp://" not in snapshot.model_dump_json()


def test_mixed_set_requires_both_kinds_and_unique_slots() -> None:
    async def consume(_slot: int, _frame: PresentationVideoFrame) -> None:
        return None

    only_live = BoundedMixedPresentation()
    with pytest.raises(MixedPresentationError) as live_exc:
        asyncio.run(
            only_live.run(
                [
                    MixedLiveStream(0, "rtsp://one", FakeLiveDelivery([])),
                    MixedLiveStream(1, "rtsp://two", FakeLiveDelivery([])),
                ],
                consume,
            )
        )
    assert live_exc.value.code == MixedPresentationErrorCode.INVALID_STREAM_SET
    assert only_live.snapshot.state == MixedPresentationState.CREATED

    duplicate = BoundedMixedPresentation()
    with pytest.raises(MixedPresentationError) as duplicate_exc:
        asyncio.run(
            duplicate.run(
                [
                    MixedLiveStream(2, "rtsp://one", FakeLiveDelivery([])),
                    MixedPlaybackStream(2, FakePlaybackDelivery([])),
                ],
                consume,
            )
        )
    assert duplicate_exc.value.code == MixedPresentationErrorCode.INVALID_STREAM_SET


def test_live_and_playback_child_failures_are_sanitized() -> None:
    async def consume(_slot: int, _frame: PresentationVideoFrame) -> None:
        return None

    for streams in (
        [
            MixedLiveStream(0, "rtsp://SECRET", FakeLiveDelivery([], fail=True)),
            MixedPlaybackStream(1, FakePlaybackDelivery([_frame(0)])),
        ],
        [
            MixedLiveStream(0, "rtsp://source", FakeLiveDelivery([_frame(0)])),
            MixedPlaybackStream(1, FakePlaybackDelivery([], fail=True)),
        ],
    ):
        coordinator = BoundedMixedPresentation()
        with pytest.raises(MixedPresentationError) as exc:
            asyncio.run(coordinator.run(streams, consume))
        assert exc.value.code == MixedPresentationErrorCode.STREAM_FAILURE
        assert "SECRET" not in str(exc.value)
        assert coordinator.snapshot.state == MixedPresentationState.FAILED


def test_consumer_failure_and_limits_fail_closed() -> None:
    async def fail_consumer(_slot: int, _frame: PresentationVideoFrame) -> None:
        raise RuntimeError("SECRET renderer detail")

    coordinator = BoundedMixedPresentation()
    with pytest.raises(MixedPresentationError) as consumer_exc:
        asyncio.run(coordinator.run(_streams(), fail_consumer))
    assert consumer_exc.value.code == MixedPresentationErrorCode.CONSUMER_FAILURE
    assert "SECRET" not in str(consumer_exc.value)

    async def consume(_slot: int, _frame: PresentationVideoFrame) -> None:
        return None

    frame_limited = BoundedMixedPresentation(max_total_frames=1)
    with pytest.raises(MixedPresentationError) as frame_exc:
        asyncio.run(frame_limited.run(_streams(), consume))
    assert frame_exc.value.code == MixedPresentationErrorCode.FRAME_LIMIT

    byte_limited = BoundedMixedPresentation(max_total_frame_bytes=4)
    with pytest.raises(MixedPresentationError) as byte_exc:
        asyncio.run(byte_limited.run(_streams(), consume))
    assert byte_exc.value.code == MixedPresentationErrorCode.BYTE_LIMIT


def test_single_use_and_constructor_bounds_fail_closed() -> None:
    coordinator = BoundedMixedPresentation()

    async def consume(_slot: int, _frame: PresentationVideoFrame) -> None:
        return None

    asyncio.run(coordinator.run(_streams(), consume))
    with pytest.raises(MixedPresentationError) as exc:
        asyncio.run(coordinator.run(_streams(), consume))
    assert exc.value.code == MixedPresentationErrorCode.INVALID_STATE

    with pytest.raises(ValueError):
        BoundedMixedPresentation(max_streams=1)
    with pytest.raises(ValueError):
        BoundedMixedPresentation(max_total_frames=0)
    with pytest.raises(ValueError):
        BoundedMixedPresentation(max_total_frame_bytes=0)
    with pytest.raises(ValueError):
        MixedLiveStream(4096, "rtsp://source", FakeLiveDelivery([]))
    with pytest.raises(ValueError):
        MixedLiveStream(0, "", FakeLiveDelivery([]))
    with pytest.raises(ValueError):
        MixedPlaybackStream(4096, FakePlaybackDelivery([]))
