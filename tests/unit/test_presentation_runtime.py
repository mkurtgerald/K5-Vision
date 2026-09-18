from __future__ import annotations

import asyncio

import pytest

from k5vision.media.live_presentation import LivePresentationSnapshot, LivePresentationState
from k5vision.media.mixed_presentation import MixedLiveStream, MixedPlaybackStream
from k5vision.media.presentation_frame import PixelFormat, PresentationVideoFrame
from k5vision.media.presentation_playback import (
    PresentationPlaybackSnapshot,
    PresentationPlaybackState,
)
from k5vision.media.presentation_runtime import (
    BoundedPresentationRuntime,
    PresentationRuntimeError,
    PresentationRuntimeErrorCode,
    PresentationRuntimeState,
)
from k5vision.media.viewport_dispatch import ViewportBinding


def _frame(source_elapsed_ms: int) -> PresentationVideoFrame:
    return PresentationVideoFrame(
        payload=memoryview(b"abcd"),
        width=1,
        height=1,
        stride_bytes=4,
        pixel_format=PixelFormat.BGRX,
        source_elapsed_ms=source_elapsed_ms,
    )


class FakeLiveDelivery:
    def __init__(self, *, block: bool = False, fail: bool = False) -> None:
        self.block = block
        self.fail = fail
        self.calls = 0

    async def run(self, source_uri: str, consumer: object) -> LivePresentationSnapshot:
        self.calls += 1
        if self.fail:
            raise RuntimeError(f"SECRET source detail {source_uri}")
        if self.block:
            await asyncio.Event().wait()
        await consumer(_frame(10))  # type: ignore[operator]
        return LivePresentationSnapshot(
            state=LivePresentationState.COMPLETE,
            decoder_initialized=True,
            accepted_packets=1,
            rtp_valid_packets=1,
            rtp_invalid_packets=0,
            rtp_delivered_bytes=13,
            delivered_frames=1,
            delivered_frame_bytes=4,
            source_span_ms=10,
        )


class FakePlaybackDelivery:
    def __init__(self, *, block: bool = False) -> None:
        self.block = block
        self.calls = 0

    async def run(self, consumer: object) -> PresentationPlaybackSnapshot:
        self.calls += 1
        if self.block:
            await asyncio.Event().wait()
        await consumer(_frame(20))  # type: ignore[operator]
        return PresentationPlaybackSnapshot(
            state=PresentationPlaybackState.COMPLETE,
            decoder_initialized=True,
            delivered_frames=1,
            delivered_bytes=4,
            source_span_ms=20,
            pump_delivered_packets=1,
            late_packets=0,
            descriptor_verified=True,
        )


def _bindings(observed: dict[int, int] | None = None) -> list[ViewportBinding]:
    counts = observed if observed is not None else {0: 0, 1: 0}

    async def first(_frame: PresentationVideoFrame) -> None:
        counts[0] = counts.get(0, 0) + 1
        await asyncio.sleep(0)

    async def second(_frame: PresentationVideoFrame) -> None:
        counts[1] = counts.get(1, 0) + 1
        await asyncio.sleep(0)

    return [ViewportBinding(0, first), ViewportBinding(1, second)]


def _streams(
    *,
    live: FakeLiveDelivery | None = None,
    playback: FakePlaybackDelivery | None = None,
    live_slot: int = 0,
    playback_slot: int = 1,
) -> list[MixedLiveStream | MixedPlaybackStream]:
    return [
        MixedLiveStream(
            live_slot,
            "rtsp://user:SECRET@192.168.1.10/live",
            live or FakeLiveDelivery(),
        ),
        MixedPlaybackStream(playback_slot, playback or FakePlaybackDelivery()),
    ]


def test_runtime_assembles_boundaries_and_completes_source_free() -> None:
    observed = {0: 0, 1: 0}
    runtime = BoundedPresentationRuntime(
        _bindings(observed),
        max_streams=2,
        max_viewports=2,
    )

    async def exercise() -> None:
        started = await runtime.start(_streams())
        assert started.state == PresentationRuntimeState.RUNNING
        final = await runtime.wait()
        assert final.state == PresentationRuntimeState.COMPLETE
        assert final.stream_count == 2
        assert final.live_streams == 1
        assert final.playback_streams == 1
        assert final.viewport_count == 2
        assert final.completed_streams == 2
        assert final.delivered_frames == 2
        assert final.delivered_frame_bytes == 8
        assert final.max_source_span_ms == 20
        payload = final.model_dump_json()
        assert "rtsp://" not in payload
        assert "192.168" not in payload
        assert "SECRET" not in payload
        assert "source_id" not in payload
        assert "recording_id" not in payload
        assert "abcd" not in payload

    asyncio.run(exercise())
    assert observed == {0: 1, 1: 1}


def test_plan_must_match_viewport_slots_before_child_execution() -> None:
    live = FakeLiveDelivery()
    playback = FakePlaybackDelivery()
    runtime = BoundedPresentationRuntime(_bindings(), max_streams=2, max_viewports=2)

    async def exercise() -> None:
        with pytest.raises(PresentationRuntimeError) as exc:
            await runtime.start(
                _streams(live=live, playback=playback, playback_slot=2)
            )
        assert exc.value.code == PresentationRuntimeErrorCode.INVALID_PLAN
        assert runtime.snapshot.state == PresentationRuntimeState.CREATED

    asyncio.run(exercise())
    assert live.calls == 0
    assert playback.calls == 0


def test_plan_requires_one_live_and_one_playback_stream() -> None:
    runtime = BoundedPresentationRuntime(_bindings(), max_streams=2, max_viewports=2)
    live_a = FakeLiveDelivery()
    live_b = FakeLiveDelivery()
    streams = [
        MixedLiveStream(0, "rtsp://SECRET/one", live_a),
        MixedLiveStream(1, "rtsp://SECRET/two", live_b),
    ]

    async def exercise() -> None:
        with pytest.raises(PresentationRuntimeError) as exc:
            await runtime.start(streams)
        assert exc.value.code == PresentationRuntimeErrorCode.INVALID_PLAN
        assert "SECRET" not in str(exc.value)

    asyncio.run(exercise())
    assert live_a.calls == 0
    assert live_b.calls == 0


def test_stop_cancels_running_runtime_and_releases_consumers() -> None:
    runtime = BoundedPresentationRuntime(
        _bindings(),
        max_streams=2,
        max_viewports=2,
        stop_timeout_seconds=1.0,
    )

    async def exercise() -> None:
        await runtime.start(
            _streams(
                live=FakeLiveDelivery(block=True),
                playback=FakePlaybackDelivery(block=True),
            )
        )
        await asyncio.sleep(0)
        stopped = await runtime.stop()
        assert stopped.state == PresentationRuntimeState.STOPPED
        assert (await runtime.wait()).state == PresentationRuntimeState.STOPPED
        assert (await runtime.close()).state == PresentationRuntimeState.CLOSED

    asyncio.run(exercise())


def test_close_before_start_is_terminal_and_start_reuse_is_rejected() -> None:
    runtime = BoundedPresentationRuntime(_bindings(), max_streams=2, max_viewports=2)

    async def exercise() -> None:
        assert (await runtime.close()).state == PresentationRuntimeState.CLOSED
        assert (await runtime.close()).state == PresentationRuntimeState.CLOSED
        with pytest.raises(PresentationRuntimeError) as exc:
            await runtime.start(_streams())
        assert exc.value.code == PresentationRuntimeErrorCode.INVALID_STATE

    asyncio.run(exercise())


def test_child_failure_is_sanitized_at_runtime_boundary() -> None:
    runtime = BoundedPresentationRuntime(_bindings(), max_streams=2, max_viewports=2)

    async def exercise() -> None:
        await runtime.start(_streams(live=FakeLiveDelivery(fail=True)))
        with pytest.raises(PresentationRuntimeError) as exc:
            await runtime.wait()
        assert exc.value.code == PresentationRuntimeErrorCode.CONTROL_FAILURE
        assert "SECRET" not in str(exc.value)
        assert runtime.snapshot.state == PresentationRuntimeState.FAILED

    asyncio.run(exercise())


def test_runtime_rejects_invalid_configuration_and_duplicate_viewports() -> None:
    with pytest.raises(PresentationRuntimeError) as stream_exc:
        BoundedPresentationRuntime(_bindings(), max_streams=1, max_viewports=2)
    assert stream_exc.value.code == PresentationRuntimeErrorCode.INVALID_CONFIGURATION

    async def consume(_frame: PresentationVideoFrame) -> None:
        return None

    with pytest.raises(PresentationRuntimeError) as duplicate_exc:
        BoundedPresentationRuntime(
            [ViewportBinding(0, consume), ViewportBinding(0, consume)],
            max_streams=2,
            max_viewports=2,
        )
    assert duplicate_exc.value.code == PresentationRuntimeErrorCode.INVALID_CONFIGURATION
