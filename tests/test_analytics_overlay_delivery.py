from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from k5vision.media.analytics_overlay_delivery import BoundedAnalyticsOverlayDelivery
from k5vision.media.live_presentation import LivePresentationSnapshot, LivePresentationState
from k5vision.media.presentation_frame import PixelFormat, PresentationVideoFrame


def _frame(marker: int = 0) -> PresentationVideoFrame:
    width = 12
    height = 8
    stride = width * 4
    payload = bytearray(stride * height)
    payload[-1] = marker
    return PresentationVideoFrame(
        payload=memoryview(bytes(payload)),
        width=width,
        height=height,
        stride_bytes=stride,
        pixel_format=PixelFormat.BGRX,
        source_elapsed_ms=marker,
    )


@dataclass(frozen=True)
class _Box:
    x_min: float = 0.2
    y_min: float = 0.2
    x_max: float = 0.8
    y_max: float = 0.8


@dataclass(frozen=True)
class _Tracked:
    track_id: str = "session-track-1"
    category: str = "person"
    confidence: float = 0.95
    box: _Box = _Box()
    model_class_id: int = 0


class _Runner:
    def __init__(
        self,
        frames: tuple[PresentationVideoFrame, ...],
        *,
        inter_frame_delay: float = 0,
    ) -> None:
        self.frames = frames
        self.inter_frame_delay = inter_frame_delay
        self.calls = 0

    async def run(self, source_uri: str, consumer: object) -> LivePresentationSnapshot:
        assert source_uri == "rtsp://private-source/live"
        self.calls += 1
        for frame in self.frames:
            await consumer(frame)  # type: ignore[operator]
            await asyncio.sleep(self.inter_frame_delay)
        return LivePresentationSnapshot(
            state=LivePresentationState.COMPLETE,
            decoder_initialized=True,
            accepted_packets=len(self.frames),
            rtp_valid_packets=len(self.frames),
            rtp_invalid_packets=0,
            rtp_delivered_bytes=sum(len(frame.payload) for frame in self.frames),
            delivered_frames=len(self.frames),
            delivered_frame_bytes=sum(len(frame.payload) for frame in self.frames),
            source_span_ms=max((frame.source_elapsed_ms for frame in self.frames), default=0),
        )


async def _append(target: list[PresentationVideoFrame], frame: PresentationVideoFrame) -> None:
    target.append(frame)


def test_completed_provider_result_overlays_following_frame_without_retaining_identity() -> None:
    first = _frame(10)
    second = _frame(11)
    runner = _Runner((first, second))

    async def provider(_frame: PresentationVideoFrame) -> tuple[_Tracked, ...]:
        return (_Tracked(track_id="private-track-marker", category="private-person-marker"),)

    delivery = BoundedAnalyticsOverlayDelivery(runner, provider)
    received: list[PresentationVideoFrame] = []

    result = asyncio.run(
        delivery.run(
            "rtsp://private-source/live",
            lambda frame: _append(received, frame),
        )
    )

    assert result.state is LivePresentationState.COMPLETE
    assert runner.calls == 1
    assert len(received) == 2
    assert received[0] is first
    assert bytes(received[1].payload) != bytes(second.payload)
    assert delivery.snapshot.processed_frames == 2
    assert delivery.snapshot.overlay_frames == 1
    assert delivery.snapshot.passthrough_frames == 1
    assert delivery.snapshot.provider_submissions == 2
    assert delivery.snapshot.provider_completions == 2
    assert delivery.snapshot.analytics_failures == 0
    assert delivery.snapshot.rendered_boxes == 1
    retained = delivery.snapshot.model_dump_json().casefold()
    assert "private-track-marker" not in retained
    assert "private-person-marker" not in retained
    assert "payload" not in retained
    assert "source" not in retained


def test_low_confidence_provider_result_stays_passthrough() -> None:
    first = _frame(20)
    second = _frame(21)

    async def provider(_frame: PresentationVideoFrame) -> tuple[_Tracked, ...]:
        return (_Tracked(confidence=0.1),)

    delivery = BoundedAnalyticsOverlayDelivery(_Runner((first, second)), provider)
    received: list[PresentationVideoFrame] = []

    asyncio.run(delivery.run("rtsp://private-source/live", lambda frame: _append(received, frame)))

    assert received == [first, second]
    assert delivery.snapshot.overlay_frames == 0
    assert delivery.snapshot.passthrough_frames == 2
    assert delivery.snapshot.provider_completions == 2
    assert delivery.snapshot.analytics_failures == 0
    assert delivery.snapshot.rendered_boxes == 0


def test_provider_failure_and_malformed_output_fail_open() -> None:
    frames = (_frame(30), _frame(31), _frame(32))
    calls = 0

    async def provider(_frame: PresentationVideoFrame) -> tuple[object, ...]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("private-provider-failure")
        return (object(),)

    delivery = BoundedAnalyticsOverlayDelivery(
        _Runner(frames),
        provider,
        max_frames=2,
    )
    received: list[PresentationVideoFrame] = []

    asyncio.run(delivery.run("rtsp://private-source/live", lambda frame: _append(received, frame)))

    assert received == list(frames)
    assert calls == 2
    assert delivery.snapshot.processed_frames == 3
    assert delivery.snapshot.provider_submissions == 2
    assert delivery.snapshot.provider_completions == 0
    assert delivery.snapshot.analytics_failures == 2
    assert delivery.snapshot.capacity_bypasses == 1
    assert delivery.snapshot.passthrough_frames == 3
    retained = delivery.snapshot.model_dump_json().casefold()
    assert "private-provider-failure" not in retained


def test_provider_timeout_fails_open_without_delaying_next_frame() -> None:
    first = _frame(40)
    second = _frame(41)
    provider_calls = 0

    async def provider(_frame: PresentationVideoFrame) -> tuple[_Tracked, ...]:
        nonlocal provider_calls
        provider_calls += 1
        await asyncio.sleep(0.02)
        return (_Tracked(),)

    delivery = BoundedAnalyticsOverlayDelivery(
        _Runner((first, second), inter_frame_delay=0.005),
        provider,
        max_frames=1,
        provider_timeout_seconds=0.001,
    )
    received: list[PresentationVideoFrame] = []

    asyncio.run(delivery.run("rtsp://private-source/live", lambda frame: _append(received, frame)))

    assert received == [first, second]
    assert provider_calls == 1
    assert delivery.snapshot.processed_frames == 2
    assert delivery.snapshot.provider_submissions == 1
    assert delivery.snapshot.provider_completions == 0
    assert delivery.snapshot.analytics_failures == 1
    assert delivery.snapshot.capacity_bypasses == 1
    assert delivery.snapshot.passthrough_frames == 2


def test_busy_provider_is_single_flight_and_cancelled_when_live_run_ends() -> None:
    frames = (_frame(50), _frame(51), _frame(52))
    provider_calls = 0
    cancelled = False

    async def provider(_frame: PresentationVideoFrame) -> tuple[_Tracked, ...]:
        nonlocal provider_calls, cancelled
        provider_calls += 1
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled = True
            raise
        return ()

    delivery = BoundedAnalyticsOverlayDelivery(_Runner(frames), provider)
    received: list[PresentationVideoFrame] = []

    asyncio.run(delivery.run("rtsp://private-source/live", lambda frame: _append(received, frame)))

    assert received == list(frames)
    assert provider_calls == 1
    assert cancelled is True
    assert delivery.snapshot.provider_submissions == 1
    assert delivery.snapshot.provider_completions == 0
    assert delivery.snapshot.busy_bypasses == 2
    assert delivery.snapshot.passthrough_frames == 3


def test_stale_completed_result_is_not_rendered() -> None:
    first = _frame(0)
    second = _frame(10)

    async def provider(_frame: PresentationVideoFrame) -> tuple[_Tracked, ...]:
        return (_Tracked(),)

    delivery = BoundedAnalyticsOverlayDelivery(
        _Runner((first, second)),
        provider,
        max_frames=1,
        max_stale_ms=5,
    )
    received: list[PresentationVideoFrame] = []

    asyncio.run(delivery.run("rtsp://private-source/live", lambda frame: _append(received, frame)))

    assert received == [first, second]
    assert delivery.snapshot.provider_completions == 1
    assert delivery.snapshot.stale_bypasses == 1
    assert delivery.snapshot.overlay_frames == 0
    assert delivery.snapshot.capacity_bypasses == 1


def test_provider_self_cancellation_is_fail_open() -> None:
    first = _frame(60)
    second = _frame(61)

    async def provider(_frame: PresentationVideoFrame) -> tuple[_Tracked, ...]:
        raise asyncio.CancelledError

    delivery = BoundedAnalyticsOverlayDelivery(
        _Runner((first, second)),
        provider,
        max_frames=1,
    )
    received: list[PresentationVideoFrame] = []

    asyncio.run(delivery.run("rtsp://private-source/live", lambda frame: _append(received, frame)))

    assert received == [first, second]
    assert delivery.snapshot.analytics_failures == 1
    assert delivery.snapshot.provider_completions == 0


def test_consumer_failure_is_not_swallowed_as_analytics_failure() -> None:
    first = _frame(70)
    second = _frame(71)

    async def provider(_frame: PresentationVideoFrame) -> tuple[_Tracked, ...]:
        return (_Tracked(),)

    delivery = BoundedAnalyticsOverlayDelivery(_Runner((first, second)), provider)
    consumed = 0

    async def reject_second(_frame: PresentationVideoFrame) -> None:
        nonlocal consumed
        consumed += 1
        if consumed == 2:
            raise RuntimeError("presentation-consumer-failure")

    with pytest.raises(RuntimeError, match="presentation-consumer-failure"):
        asyncio.run(delivery.run("rtsp://private-source/live", reject_second))

    assert consumed == 2
    assert delivery.snapshot.analytics_failures == 0
    assert delivery.snapshot.overlay_frames == 1


def test_overlay_delivery_is_single_use() -> None:
    async def provider(_frame: PresentationVideoFrame) -> tuple[_Tracked, ...]:
        return ()

    delivery = BoundedAnalyticsOverlayDelivery(_Runner((_frame(),)), provider)
    asyncio.run(delivery.run("rtsp://private-source/live", lambda frame: _append([], frame)))

    with pytest.raises(RuntimeError, match="cannot be reused"):
        asyncio.run(delivery.run("rtsp://private-source/live", lambda frame: _append([], frame)))


def test_overlay_delivery_configuration_is_bounded() -> None:
    async def provider(_frame: PresentationVideoFrame) -> tuple[_Tracked, ...]:
        return ()

    with pytest.raises(TypeError, match="runner"):
        BoundedAnalyticsOverlayDelivery(object(), provider)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="provider"):
        BoundedAnalyticsOverlayDelivery(_Runner(()), object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="max_observations"):
        BoundedAnalyticsOverlayDelivery(_Runner(()), provider, max_observations=0)
    with pytest.raises(ValueError, match="max_frames"):
        BoundedAnalyticsOverlayDelivery(_Runner(()), provider, max_frames=0)
    with pytest.raises(ValueError, match="provider_timeout_seconds"):
        BoundedAnalyticsOverlayDelivery(_Runner(()), provider, provider_timeout_seconds=0)
    with pytest.raises(ValueError, match="max_stale_ms"):
        BoundedAnalyticsOverlayDelivery(_Runner(()), provider, max_stale_ms=0)
    with pytest.raises(ValueError, match="minimum_confidence"):
        BoundedAnalyticsOverlayDelivery(_Runner(()), provider, minimum_confidence=2)
    with pytest.raises(ValueError, match="border_width"):
        BoundedAnalyticsOverlayDelivery(_Runner(()), provider, border_width=0)
