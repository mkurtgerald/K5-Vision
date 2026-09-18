from __future__ import annotations

import asyncio

import pytest

from k5vision.media.live_presentation import (
    LivePresentationError,
    LivePresentationErrorCode,
    LivePresentationSnapshot,
    LivePresentationState,
)
from k5vision.media.multiview_live_presentation import (
    BoundedMultiViewLivePresentation,
    MultiViewLiveError,
    MultiViewLiveErrorCode,
    MultiViewLiveState,
    MultiViewLiveStream,
)
from k5vision.media.presentation_frame import PixelFormat, PresentationVideoFrame


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


def _streams() -> list[MultiViewLiveStream]:
    return [
        MultiViewLiveStream(
            0,
            "rtsp://user:SECRET@192.168.1.10/a",
            FakeLiveDelivery([_frame(0), _frame(100)]),
        ),
        MultiViewLiveStream(
            1,
            "rtsp://user:SECRET@192.168.1.11/b",
            FakeLiveDelivery([_frame(0), _frame(80)]),
        ),
    ]


def test_multiview_coordinates_streams_and_retains_source_free_aggregate_state() -> None:
    coordinator = BoundedMultiViewLivePresentation()
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
    assert snapshot.state == MultiViewLiveState.COMPLETE
    assert snapshot.stream_count == 2
    assert snapshot.completed_streams == 2
    assert snapshot.delivered_frames == 4
    assert snapshot.delivered_frame_bytes == 16
    assert snapshot.max_source_span_ms == 100
    payload = snapshot.model_dump_json()
    assert "rtsp://" not in payload
    assert "192.168" not in payload
    assert "SECRET" not in payload
    assert "abcd" not in payload


def test_duplicate_slots_fail_before_execution() -> None:
    coordinator = BoundedMultiViewLivePresentation()
    delivery = FakeLiveDelivery([_frame(0)])
    streams = [
        MultiViewLiveStream(2, "rtsp://first", delivery),
        MultiViewLiveStream(2, "rtsp://second", delivery),
    ]

    async def consume(_slot: int, _frame: PresentationVideoFrame) -> None:
        return None

    with pytest.raises(MultiViewLiveError) as exc:
        asyncio.run(coordinator.run(streams, consume))

    assert exc.value.code == MultiViewLiveErrorCode.INVALID_STREAM_SET
    assert coordinator.snapshot.state == MultiViewLiveState.CREATED


def test_child_failure_is_sanitized() -> None:
    coordinator = BoundedMultiViewLivePresentation()
    streams = [
        MultiViewLiveStream(0, "rtsp://SECRET-first", FakeLiveDelivery([], fail=True)),
        MultiViewLiveStream(1, "rtsp://SECRET-second", FakeLiveDelivery([_frame(0)])),
    ]

    async def consume(_slot: int, _frame: PresentationVideoFrame) -> None:
        return None

    with pytest.raises(MultiViewLiveError) as exc:
        asyncio.run(coordinator.run(streams, consume))

    assert exc.value.code == MultiViewLiveErrorCode.STREAM_FAILURE
    assert "SECRET" not in str(exc.value)
    assert coordinator.snapshot.state == MultiViewLiveState.FAILED


def test_consumer_failure_is_preserved_across_child_boundary() -> None:
    coordinator = BoundedMultiViewLivePresentation()

    async def consume(_slot: int, _frame: PresentationVideoFrame) -> None:
        raise RuntimeError("SECRET renderer detail")

    with pytest.raises(MultiViewLiveError) as exc:
        asyncio.run(coordinator.run(_streams(), consume))

    assert exc.value.code == MultiViewLiveErrorCode.CONSUMER_FAILURE
    assert "SECRET" not in str(exc.value)
    assert coordinator.snapshot.state == MultiViewLiveState.FAILED


def test_aggregate_frame_and_byte_limits_fail_closed() -> None:
    async def consume(_slot: int, _frame: PresentationVideoFrame) -> None:
        return None

    frame_limited = BoundedMultiViewLivePresentation(max_total_frames=1)
    with pytest.raises(MultiViewLiveError) as frame_exc:
        asyncio.run(frame_limited.run(_streams(), consume))
    assert frame_exc.value.code == MultiViewLiveErrorCode.FRAME_LIMIT

    byte_limited = BoundedMultiViewLivePresentation(max_total_frame_bytes=4)
    with pytest.raises(MultiViewLiveError) as byte_exc:
        asyncio.run(byte_limited.run(_streams(), consume))
    assert byte_exc.value.code == MultiViewLiveErrorCode.BYTE_LIMIT


def test_single_use_and_constructor_bounds_fail_closed() -> None:
    coordinator = BoundedMultiViewLivePresentation()

    async def consume(_slot: int, _frame: PresentationVideoFrame) -> None:
        return None

    asyncio.run(coordinator.run(_streams(), consume))
    with pytest.raises(MultiViewLiveError) as exc:
        asyncio.run(coordinator.run(_streams(), consume))
    assert exc.value.code == MultiViewLiveErrorCode.INVALID_STATE

    with pytest.raises(ValueError):
        BoundedMultiViewLivePresentation(max_streams=1)
    with pytest.raises(ValueError):
        BoundedMultiViewLivePresentation(max_total_frames=0)
    with pytest.raises(ValueError):
        BoundedMultiViewLivePresentation(max_total_frame_bytes=0)
    with pytest.raises(ValueError):
        MultiViewLiveStream(64, "rtsp://source", FakeLiveDelivery([]))
    with pytest.raises(ValueError):
        MultiViewLiveStream(0, "", FakeLiveDelivery([]))
