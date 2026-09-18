from __future__ import annotations

import asyncio

import pytest

from k5vision.media.multiview_playback_presentation import (
    BoundedMultiViewPlaybackPresentation,
    MultiViewPlaybackError,
    MultiViewPlaybackErrorCode,
    MultiViewPlaybackState,
    MultiViewPlaybackStream,
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


def _streams() -> list[MultiViewPlaybackStream]:
    return [
        MultiViewPlaybackStream(0, FakePlaybackDelivery([_frame(0), _frame(100)])),
        MultiViewPlaybackStream(1, FakePlaybackDelivery([_frame(0), _frame(80)])),
    ]


def test_multiview_playback_serializes_consumer_and_retains_aggregate_state() -> None:
    coordinator = BoundedMultiViewPlaybackPresentation()
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
    assert snapshot.state == MultiViewPlaybackState.COMPLETE
    assert snapshot.stream_count == 2
    assert snapshot.completed_streams == 2
    assert snapshot.delivered_frames == 4
    assert snapshot.delivered_frame_bytes == 16
    assert snapshot.max_source_span_ms == 100
    payload = snapshot.model_dump_json()
    assert "recording_id" not in payload
    assert "source_id" not in payload
    assert "rtsp://" not in payload
    assert "SECRET" not in payload
    assert "abcd" not in payload


def test_duplicate_slots_fail_before_execution() -> None:
    coordinator = BoundedMultiViewPlaybackPresentation()
    delivery = FakePlaybackDelivery([_frame(0)])
    streams = [MultiViewPlaybackStream(2, delivery), MultiViewPlaybackStream(2, delivery)]

    async def consume(_slot: int, _frame: PresentationVideoFrame) -> None:
        return None

    with pytest.raises(MultiViewPlaybackError) as exc:
        asyncio.run(coordinator.run(streams, consume))

    assert exc.value.code == MultiViewPlaybackErrorCode.INVALID_STREAM_SET
    assert coordinator.snapshot.state == MultiViewPlaybackState.CREATED


def test_child_failure_is_sanitized() -> None:
    coordinator = BoundedMultiViewPlaybackPresentation()
    streams = [
        MultiViewPlaybackStream(0, FakePlaybackDelivery([], fail=True)),
        MultiViewPlaybackStream(1, FakePlaybackDelivery([_frame(0)])),
    ]

    async def consume(_slot: int, _frame: PresentationVideoFrame) -> None:
        return None

    with pytest.raises(MultiViewPlaybackError) as exc:
        asyncio.run(coordinator.run(streams, consume))

    assert exc.value.code == MultiViewPlaybackErrorCode.STREAM_FAILURE
    assert "SECRET" not in str(exc.value)
    assert coordinator.snapshot.state == MultiViewPlaybackState.FAILED


def test_consumer_failure_is_preserved_across_child_boundary() -> None:
    coordinator = BoundedMultiViewPlaybackPresentation()

    async def consume(_slot: int, _frame: PresentationVideoFrame) -> None:
        raise RuntimeError("SECRET renderer detail")

    with pytest.raises(MultiViewPlaybackError) as exc:
        asyncio.run(coordinator.run(_streams(), consume))

    assert exc.value.code == MultiViewPlaybackErrorCode.CONSUMER_FAILURE
    assert "SECRET" not in str(exc.value)
    assert coordinator.snapshot.state == MultiViewPlaybackState.FAILED


def test_aggregate_frame_and_byte_limits_fail_closed() -> None:
    async def consume(_slot: int, _frame: PresentationVideoFrame) -> None:
        return None

    frame_limited = BoundedMultiViewPlaybackPresentation(max_total_frames=1)
    with pytest.raises(MultiViewPlaybackError) as frame_exc:
        asyncio.run(frame_limited.run(_streams(), consume))
    assert frame_exc.value.code == MultiViewPlaybackErrorCode.FRAME_LIMIT

    byte_limited = BoundedMultiViewPlaybackPresentation(max_total_frame_bytes=4)
    with pytest.raises(MultiViewPlaybackError) as byte_exc:
        asyncio.run(byte_limited.run(_streams(), consume))
    assert byte_exc.value.code == MultiViewPlaybackErrorCode.BYTE_LIMIT


def test_single_use_and_constructor_bounds_fail_closed() -> None:
    coordinator = BoundedMultiViewPlaybackPresentation()

    async def consume(_slot: int, _frame: PresentationVideoFrame) -> None:
        return None

    asyncio.run(coordinator.run(_streams(), consume))
    with pytest.raises(MultiViewPlaybackError) as exc:
        asyncio.run(coordinator.run(_streams(), consume))
    assert exc.value.code == MultiViewPlaybackErrorCode.INVALID_STATE

    with pytest.raises(ValueError):
        BoundedMultiViewPlaybackPresentation(max_streams=1)
    with pytest.raises(ValueError):
        BoundedMultiViewPlaybackPresentation(max_total_frames=0)
    with pytest.raises(ValueError):
        BoundedMultiViewPlaybackPresentation(max_total_frame_bytes=0)
    with pytest.raises(ValueError):
        MultiViewPlaybackStream(64, FakePlaybackDelivery([]))
