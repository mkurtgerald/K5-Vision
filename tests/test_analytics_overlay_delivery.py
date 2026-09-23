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
    def __init__(self, frames: tuple[PresentationVideoFrame, ...]) -> None:
        self.frames = frames
        self.calls = 0

    async def run(self, source_uri: str, consumer: object) -> LivePresentationSnapshot:
        assert source_uri == "rtsp://private-source/live"
        self.calls += 1
        for frame in self.frames:
            await consumer(frame)  # type: ignore[operator]
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


def test_overlay_delivery_renders_provider_boxes_without_retaining_identity() -> None:
    source = _frame(10)
    runner = _Runner((source,))

    async def provider(frame: PresentationVideoFrame) -> tuple[_Tracked, ...]:
        assert frame is source
        return (_Tracked(track_id="private-track-marker", category="private-person-marker"),)

    delivery = BoundedAnalyticsOverlayDelivery(runner, provider)
    received: list[PresentationVideoFrame] = []

    async def scenario() -> None:
        result = await delivery.run(
            "rtsp://private-source/live",
            lambda frame: _append(received, frame),
        )
        assert result.state is LivePresentationState.COMPLETE

    asyncio.run(scenario())

    assert runner.calls == 1
    assert len(received) == 1
    assert bytes(received[0].payload) != bytes(source.payload)
    assert delivery.snapshot.processed_frames == 1
    assert delivery.snapshot.overlay_frames == 1
    assert delivery.snapshot.passthrough_frames == 0
    assert delivery.snapshot.analytics_failures == 0
    assert delivery.snapshot.rendered_boxes == 1
    retained = delivery.snapshot.model_dump_json().casefold()
    assert "private-track-marker" not in retained
    assert "private-person-marker" not in retained
    assert "payload" not in retained
    assert "source" not in retained


async def _append(target: list[PresentationVideoFrame], frame: PresentationVideoFrame) -> None:
    target.append(frame)


def test_low_confidence_provider_output_passes_original_frame() -> None:
    source = _frame(11)

    async def provider(_frame: PresentationVideoFrame) -> tuple[_Tracked, ...]:
        return (_Tracked(confidence=0.1),)

    delivery = BoundedAnalyticsOverlayDelivery(_Runner((source,)), provider)
    received: list[PresentationVideoFrame] = []

    asyncio.run(delivery.run("rtsp://private-source/live", lambda frame: _append(received, frame)))

    assert received == [source]
    assert delivery.snapshot.overlay_frames == 0
    assert delivery.snapshot.passthrough_frames == 1
    assert delivery.snapshot.analytics_failures == 0
    assert delivery.snapshot.rendered_boxes == 0


def test_provider_failure_and_malformed_output_fail_open() -> None:
    source = _frame(12)
    calls = 0

    async def provider(_frame: PresentationVideoFrame) -> tuple[object, ...]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("private-provider-failure")
        return (object(),)

    delivery = BoundedAnalyticsOverlayDelivery(_Runner((source, _frame(13))), provider)
    received: list[PresentationVideoFrame] = []

    asyncio.run(delivery.run("rtsp://private-source/live", lambda frame: _append(received, frame)))

    assert len(received) == 2
    assert delivery.snapshot.processed_frames == 2
    assert delivery.snapshot.analytics_failures == 2
    assert delivery.snapshot.passthrough_frames == 2
    retained = delivery.snapshot.model_dump_json().casefold()
    assert "private-provider-failure" not in retained


def test_provider_timeout_fails_open_and_capacity_bypass_skips_provider() -> None:
    first = _frame(14)
    second = _frame(15)
    provider_calls = 0

    async def provider(_frame: PresentationVideoFrame) -> tuple[_Tracked, ...]:
        nonlocal provider_calls
        provider_calls += 1
        await asyncio.sleep(0.02)
        return (_Tracked(),)

    delivery = BoundedAnalyticsOverlayDelivery(
        _Runner((first, second)),
        provider,
        max_frames=1,
        provider_timeout_seconds=0.001,
    )
    received: list[PresentationVideoFrame] = []

    asyncio.run(delivery.run("rtsp://private-source/live", lambda frame: _append(received, frame)))

    assert received == [first, second]
    assert provider_calls == 1
    assert delivery.snapshot.processed_frames == 1
    assert delivery.snapshot.analytics_failures == 1
    assert delivery.snapshot.capacity_bypasses == 1
    assert delivery.snapshot.passthrough_frames == 2


def test_consumer_failure_is_not_swallowed_as_analytics_failure() -> None:
    async def provider(_frame: PresentationVideoFrame) -> tuple[_Tracked, ...]:
        return (_Tracked(),)

    delivery = BoundedAnalyticsOverlayDelivery(_Runner((_frame(16),)), provider)

    async def reject(_frame: PresentationVideoFrame) -> None:
        raise RuntimeError("presentation-consumer-failure")

    with pytest.raises(RuntimeError, match="presentation-consumer-failure"):
        asyncio.run(delivery.run("rtsp://private-source/live", reject))

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
    with pytest.raises(ValueError, match="minimum_confidence"):
        BoundedAnalyticsOverlayDelivery(_Runner(()), provider, minimum_confidence=2)
    with pytest.raises(ValueError, match="border_width"):
        BoundedAnalyticsOverlayDelivery(_Runner(()), provider, border_width=0)
