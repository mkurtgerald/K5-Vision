from __future__ import annotations

import asyncio

from k5vision.media.live_presentation import LivePresentationSnapshot, LivePresentationState
from k5vision.media.mixed_presentation import MixedLiveStream
from k5vision.media.presentation_frame import PixelFormat, PresentationVideoFrame
from k5vision.media.presentation_runtime import BoundedPresentationRuntime, PresentationRuntimeState
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


class _SingleFrameLiveDelivery:
    async def run(self, _source_uri: str, consumer: object) -> LivePresentationSnapshot:
        frame = PresentationVideoFrame(
            payload=memoryview(b"abcd"),
            width=1,
            height=1,
            stride_bytes=4,
            pixel_format=PixelFormat.BGRX,
            source_elapsed_ms=11,
        )
        await consumer(frame)  # type: ignore[operator]
        return LivePresentationSnapshot(
            state=LivePresentationState.COMPLETE,
            decoder_initialized=True,
            accepted_packets=1,
            rtp_valid_packets=1,
            rtp_invalid_packets=0,
            rtp_delivered_bytes=13,
            delivered_frames=1,
            delivered_frame_bytes=4,
            source_span_ms=11,
        )


async def _consume(_frame: PresentationVideoFrame) -> None:
    return None


def _single_stream() -> MixedLiveStream:
    return MixedLiveStream(
        slot=7,
        source_uri="rtsp://execution-only.example/live",
        delivery=_SingleFrameLiveDelivery(),
    )


def _single_layout() -> ViewportLayout:
    return ViewportLayout(
        placements=(
            ViewportPlacement(
                logical_slot=7,
                geometry=ViewportGeometry(x=0, y=0, width=1280, height=720),
            ),
        )
    )


class _SingleWindowsRuntime:
    def __init__(self, layout: ViewportLayout) -> None:
        self.layout = layout
        self._snapshot = WindowsViewportRuntimeSnapshot(
            state=WindowsViewportRuntimeState.READY,
            viewport_count=1,
            open_surface_count=0,
            presentations=0,
        )
        self._bindings = (ViewportBinding(slot=7, consumer=_consume),)

    @property
    def snapshot(self) -> WindowsViewportRuntimeSnapshot:
        return self._snapshot

    @property
    def bindings(self) -> tuple[ViewportBinding, ...]:
        return self._bindings

    async def open(self) -> WindowsViewportRuntimeSnapshot:
        self._snapshot = WindowsViewportRuntimeSnapshot(
            state=WindowsViewportRuntimeState.OPEN,
            viewport_count=1,
            open_surface_count=1,
            presentations=0,
        )
        return self._snapshot

    async def close(self) -> WindowsViewportRuntimeSnapshot:
        self._snapshot = WindowsViewportRuntimeSnapshot(
            state=WindowsViewportRuntimeState.CLOSED,
            viewport_count=1,
            open_surface_count=0,
            presentations=1,
        )
        return self._snapshot


def test_explicit_all_live_presentation_accepts_one_stream_and_one_viewport() -> None:
    runtime = BoundedPresentationRuntime(
        [ViewportBinding(slot=7, consumer=_consume)],
        max_streams=1,
        max_viewports=1,
        allow_all_live=True,
    )

    async def scenario() -> None:
        started = await runtime.start([_single_stream()])
        assert started.state == PresentationRuntimeState.RUNNING
        final = await runtime.wait()
        assert final.state == PresentationRuntimeState.COMPLETE
        assert final.stream_count == 1
        assert final.live_streams == 1
        assert final.playback_streams == 0
        assert final.viewport_count == 1
        assert final.delivered_frames == 1
        assert "rtsp://" not in final.model_dump_json()

    asyncio.run(scenario())


def test_windows_operator_runtime_requires_explicit_single_live_capability() -> None:
    layout = _single_layout()

    async def scenario() -> None:
        runtime = BoundedWindowsOperatorRuntime(
            layout,
            windows_runtime_factory=_SingleWindowsRuntime,
            allow_single_live=True,
        )
        started = await runtime.start([_single_stream()])
        assert started.state == WindowsOperatorRuntimeState.RUNNING
        assert started.viewport_count == 1
        assert started.open_surface_count == 1

        final = await runtime.wait()
        assert final.state == WindowsOperatorRuntimeState.COMPLETE
        assert final.stream_count == 1
        assert final.delivered_frames == 1
        serialized = final.model_dump_json().casefold()
        assert "rtsp://" not in serialized
        assert "execution-only" not in serialized
        assert "logical_slot" not in serialized

        closed = await runtime.close()
        assert closed.state == WindowsOperatorRuntimeState.CLOSED
        assert closed.open_surface_count == 0

    asyncio.run(scenario())
