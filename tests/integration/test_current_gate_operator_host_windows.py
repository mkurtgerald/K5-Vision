"""Win32 qualification for re-entrant arbitrary operator-layout replacement."""

from __future__ import annotations

import sys
from collections.abc import Sequence

import pytest

from k5vision.media.mixed_presentation import MixedPresentationStream
from k5vision.media.presentation_frame import PixelFormat, PresentationVideoFrame
from k5vision.media.presentation_runtime import (
    PresentationRuntimeSnapshot,
    PresentationRuntimeState,
)
from k5vision.media.viewport_dispatch import ViewportBinding
from k5vision.media.viewport_geometry import ViewportGeometry, ViewportLayout, ViewportPlacement
from k5vision.media.windows_operator_host import (
    BoundedWindowsOperatorHost,
    WindowsOperatorHostState,
)
from k5vision.media.windows_operator_runtime import BoundedWindowsOperatorRuntime

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only qualification")


def _presentation_snapshot(
    state: PresentationRuntimeState,
    delivered_frames: int = 0,
) -> PresentationRuntimeSnapshot:
    return PresentationRuntimeSnapshot(
        state=state,
        stream_count=2,
        live_streams=1,
        playback_streams=1,
        viewport_count=2,
        completed_streams=2 if state == PresentationRuntimeState.COMPLETE else 0,
        delivered_frames=delivered_frames,
        delivered_frame_bytes=delivered_frames * 16,
        max_source_span_ms=1 if delivered_frames else 0,
    )


class SyntheticPresentationRuntime:
    """Synthetic media boundary driving the real accepted Win32 viewport bindings."""

    def __init__(self, bindings: Sequence[ViewportBinding]) -> None:
        self._bindings = tuple(bindings)
        self._snapshot = _presentation_snapshot(PresentationRuntimeState.CREATED)

    @property
    def snapshot(self) -> PresentationRuntimeSnapshot:
        return self._snapshot

    async def start(
        self,
        _streams: Sequence[MixedPresentationStream],
    ) -> PresentationRuntimeSnapshot:
        frame = PresentationVideoFrame(
            payload=memoryview(bytearray([1, 2, 3, 0] * 4)),
            width=2,
            height=2,
            stride_bytes=8,
            pixel_format=PixelFormat.BGRX,
            source_elapsed_ms=1,
        )
        selected = {binding.slot: binding for binding in self._bindings}
        await selected[4095].consumer(frame)
        self._snapshot = _presentation_snapshot(
            PresentationRuntimeState.RUNNING,
            delivered_frames=1,
        )
        return self._snapshot

    async def wait(self) -> PresentationRuntimeSnapshot:
        self._snapshot = _presentation_snapshot(
            PresentationRuntimeState.COMPLETE,
            delivered_frames=1,
        )
        return self._snapshot

    async def stop(self) -> PresentationRuntimeSnapshot:
        self._snapshot = _presentation_snapshot(
            PresentationRuntimeState.STOPPED,
            delivered_frames=1,
        )
        return self._snapshot

    async def close(self) -> PresentationRuntimeSnapshot:
        self._snapshot = _presentation_snapshot(
            PresentationRuntimeState.CLOSED,
            delivered_frames=1,
        )
        return self._snapshot


def _layout(offset: int) -> ViewportLayout:
    return ViewportLayout(
        placements=(
            ViewportPlacement(
                logical_slot=7,
                geometry=ViewportGeometry(
                    x=17 + offset,
                    y=29 + offset,
                    width=2,
                    height=2,
                ),
            ),
            ViewportPlacement(
                logical_slot=4095,
                geometry=ViewportGeometry(
                    x=37 + offset,
                    y=53 + offset,
                    width=2,
                    height=2,
                ),
            ),
        )
    )


def test_real_win32_operator_host_replaces_sparse_non_grid_layouts() -> None:
    def runtime_factory(layout: ViewportLayout) -> BoundedWindowsOperatorRuntime:
        return BoundedWindowsOperatorRuntime(
            layout,
            presentation_runtime_factory=SyntheticPresentationRuntime,
        )

    async def scenario() -> None:
        host = BoundedWindowsOperatorHost(runtime_factory=runtime_factory)
        first = await host.start(_layout(0), ())
        assert first.state == WindowsOperatorHostState.RUNNING
        assert first.generation == 1
        assert first.open_surface_count == 2
        assert first.presentations == 1

        second = await host.replace(_layout(80), ())
        assert second.state == WindowsOperatorHostState.RUNNING
        assert second.generation == 2
        assert second.stopped_generations == 1
        assert second.open_surface_count == 2
        assert second.presentations == 2

        stopped = await host.stop()
        assert stopped.state == WindowsOperatorHostState.STOPPED
        assert stopped.open_surface_count == 0
        assert stopped.stopped_generations == 2
        assert stopped.presentations == 2

        closed = await host.close()
        assert closed.state == WindowsOperatorHostState.CLOSED
        assert closed.open_surface_count == 0
        assert closed.generation == 2
        assert closed.presentations == 2

    import asyncio

    asyncio.run(scenario())
