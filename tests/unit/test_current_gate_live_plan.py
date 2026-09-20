from __future__ import annotations

import asyncio

import pytest

from k5vision.media.live_presentation import LivePresentationSnapshot, LivePresentationState
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
from k5vision.media.viewport_geometry import ViewportGeometry, ViewportLayout, ViewportPlacement
from k5vision.media.windows_operator_runtime import (
    BoundedWindowsOperatorRuntime,
    WindowsOperatorRuntimeState,
)
from k5vision.media.windows_viewport_runtime import (
    WindowsViewportRuntimeSnapshot,
    WindowsViewportRuntimeState,
)


def _frame(elapsed: int) -> PresentationVideoFrame:
    return PresentationVideoFrame(
        payload=memoryview(b"abcd"),
        width=1,
        height=1,
        stride_bytes=4,
        pixel_format=PixelFormat.BGRX,
        source_elapsed_ms=elapsed,
    )


class FakeLiveDelivery:
    def __init__(self, elapsed: int) -> None:
        self.elapsed = elapsed
        self.calls = 0

    async def run(self, source_uri: str, consumer: object) -> LivePresentationSnapshot:
        self.calls += 1
        await consumer(_frame(self.elapsed))  # type: ignore[operator]
        return LivePresentationSnapshot(
            state=LivePresentationState.COMPLETE,
            decoder_initialized=True,
            accepted_packets=1,
            rtp_valid_packets=1,
            rtp_invalid_packets=0,
            rtp_delivered_bytes=13,
            delivered_frames=1,
            delivered_frame_bytes=4,
            source_span_ms=self.elapsed,
        )


class FakePlaybackDelivery:
    async def run(self, consumer: object) -> PresentationPlaybackSnapshot:
        await consumer(_frame(30))  # type: ignore[operator]
        return PresentationPlaybackSnapshot(
            state=PresentationPlaybackState.COMPLETE,
            decoder_initialized=True,
            delivered_frames=1,
            delivered_bytes=4,
            source_span_ms=30,
            pump_delivered_packets=1,
            late_packets=0,
            descriptor_verified=True,
        )


def _all_live() -> tuple[MixedLiveStream, MixedLiveStream]:
    return (
        MixedLiveStream(7, "rtsp://execution-only-a", FakeLiveDelivery(10)),
        MixedLiveStream(4095, "rtsp://execution-only-b", FakeLiveDelivery(20)),
    )


def _bindings(observed: dict[int, int]) -> tuple[ViewportBinding, ViewportBinding]:
    async def seven(_frame: PresentationVideoFrame) -> None:
        observed[7] += 1

    async def maximum(_frame: PresentationVideoFrame) -> None:
        observed[4095] += 1

    return ViewportBinding(7, seven), ViewportBinding(4095, maximum)


def _layout() -> ViewportLayout:
    return ViewportLayout(
        placements=(
            ViewportPlacement(
                logical_slot=7,
                geometry=ViewportGeometry(x=17, y=29, width=613, height=347, z_index=2),
            ),
            ViewportPlacement(
                logical_slot=4095,
                geometry=ViewportGeometry(x=701, y=41, width=211, height=719, z_index=1),
            ),
        )
    )


def test_generic_boundary_keeps_mixed_default_and_all_live_is_explicit_opt_in() -> None:
    async def consume(_slot: int, _frame: PresentationVideoFrame) -> None:
        return None

    default = BoundedMixedPresentation()
    with pytest.raises(MixedPresentationError) as default_exc:
        asyncio.run(default.run(_all_live(), consume))
    assert default_exc.value.code == MixedPresentationErrorCode.INVALID_STREAM_SET
    assert default.snapshot.state == MixedPresentationState.CREATED

    enabled = BoundedMixedPresentation(allow_all_live=True)
    snapshot = asyncio.run(enabled.run(_all_live(), consume))
    assert snapshot.state == MixedPresentationState.COMPLETE
    assert snapshot.stream_count == 2
    assert snapshot.live_streams == 2
    assert snapshot.playback_streams == 0
    assert snapshot.completed_streams == 2
    serialized = snapshot.model_dump_json().casefold()
    assert "rtsp://" not in serialized
    assert "execution-only" not in serialized
    assert "4095" not in serialized

    playback_only = BoundedMixedPresentation(allow_all_live=True)
    with pytest.raises(MixedPresentationError) as playback_exc:
        asyncio.run(
            playback_only.run(
                (
                    MixedPlaybackStream(7, FakePlaybackDelivery()),
                    MixedPlaybackStream(4095, FakePlaybackDelivery()),
                ),
                consume,
            )
        )
    assert playback_exc.value.code == MixedPresentationErrorCode.INVALID_STREAM_SET


def test_presentation_runtime_opt_in_runs_sparse_all_live_plan() -> None:
    observed = {7: 0, 4095: 0}

    default = BoundedPresentationRuntime(
        _bindings(observed),
        max_streams=2,
        max_viewports=2,
    )
    with pytest.raises(PresentationRuntimeError) as default_exc:
        asyncio.run(default.start(_all_live()))
    assert default_exc.value.code == PresentationRuntimeErrorCode.INVALID_PLAN

    runtime = BoundedPresentationRuntime(
        _bindings(observed),
        max_streams=2,
        max_viewports=2,
        allow_all_live=True,
    )

    async def exercise() -> object:
        started = await runtime.start(_all_live())
        assert started.state == PresentationRuntimeState.RUNNING
        return await runtime.wait()

    final = asyncio.run(exercise())
    assert final.state == PresentationRuntimeState.COMPLETE
    assert final.live_streams == 2
    assert final.playback_streams == 0
    assert final.delivered_frames == 2
    assert observed == {7: 1, 4095: 1}
    serialized = final.model_dump_json().casefold()
    for forbidden in ("rtsp://", "execution-only", "4095", "source_id", "recording_id"):
        assert forbidden not in serialized


class FakeWindowsRuntime:
    def __init__(self, layout: ViewportLayout, observed: dict[int, int]) -> None:
        self.layout = layout
        self._bindings = _bindings(observed)
        self._snapshot = WindowsViewportRuntimeSnapshot(
            state=WindowsViewportRuntimeState.READY,
            viewport_count=2,
            open_surface_count=0,
            presentations=0,
        )

    @property
    def snapshot(self) -> WindowsViewportRuntimeSnapshot:
        return self._snapshot

    @property
    def bindings(self) -> tuple[ViewportBinding, ViewportBinding]:
        return self._bindings

    async def open(self) -> WindowsViewportRuntimeSnapshot:
        self._snapshot = WindowsViewportRuntimeSnapshot(
            state=WindowsViewportRuntimeState.OPEN,
            viewport_count=2,
            open_surface_count=2,
            presentations=0,
        )
        return self._snapshot

    async def close(self) -> WindowsViewportRuntimeSnapshot:
        self._snapshot = WindowsViewportRuntimeSnapshot(
            state=WindowsViewportRuntimeState.CLOSED,
            viewport_count=2,
            open_surface_count=0,
            presentations=0,
        )
        return self._snapshot


def test_windows_operator_runtime_enables_all_live_without_fixed_grid_contract() -> None:
    observed = {7: 0, 4095: 0}

    def windows_factory(layout: ViewportLayout) -> FakeWindowsRuntime:
        return FakeWindowsRuntime(layout, observed)

    runtime = BoundedWindowsOperatorRuntime(
        _layout(),
        windows_runtime_factory=windows_factory,
    )

    async def exercise() -> object:
        started = await runtime.start(_all_live())
        assert started.state == WindowsOperatorRuntimeState.RUNNING
        completed = await runtime.wait()
        assert completed.state == WindowsOperatorRuntimeState.COMPLETE
        assert completed.stream_count == 2
        assert completed.delivered_frames == 2
        return await runtime.close()

    closed = asyncio.run(exercise())
    assert closed.state == WindowsOperatorRuntimeState.CLOSED
    assert closed.open_surface_count == 0
    assert observed == {7: 1, 4095: 1}
    serialized = closed.model_dump_json().casefold()
    for forbidden in (
        "rtsp://",
        "execution-only",
        "4095",
        "logical_slot",
        "path",
        "handle",
        "pointer",
        "payload",
    ):
        assert forbidden not in serialized
