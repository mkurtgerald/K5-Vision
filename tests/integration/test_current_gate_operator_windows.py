"""Win32 qualification for accepted operator media and arbitrary viewport composition."""

from __future__ import annotations

import asyncio
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
from k5vision.media.windows_operator_runtime import (
    BoundedWindowsOperatorRuntime,
    WindowsOperatorRuntimeState,
)

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only qualification")


def _snapshot(
    state: PresentationRuntimeState, delivered_frames: int = 0
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
    """Synthetic media boundary that drives the real accepted Win32 viewport bindings."""

    def __init__(self, bindings: Sequence[ViewportBinding]) -> None:
        self._bindings = tuple(bindings)
        self._snapshot = _snapshot(PresentationRuntimeState.CREATED)

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
        self._snapshot = _snapshot(PresentationRuntimeState.RUNNING, delivered_frames=1)
        return self._snapshot

    async def wait(self) -> PresentationRuntimeSnapshot:
        self._snapshot = _snapshot(PresentationRuntimeState.COMPLETE, delivered_frames=1)
        return self._snapshot

    async def stop(self) -> PresentationRuntimeSnapshot:
        self._snapshot = _snapshot(PresentationRuntimeState.STOPPED, delivered_frames=1)
        return self._snapshot

    async def close(self) -> PresentationRuntimeSnapshot:
        self._snapshot = _snapshot(PresentationRuntimeState.CLOSED, delivered_frames=1)
        return self._snapshot


def test_real_win32_operator_runtime_binds_sparse_non_grid_viewports() -> None:
    async def scenario() -> None:
        layout = ViewportLayout(
            placements=(
                ViewportPlacement(
                    logical_slot=7,
                    geometry=ViewportGeometry(x=17, y=29, width=2, height=2),
                ),
                ViewportPlacement(
                    logical_slot=4095,
                    geometry=ViewportGeometry(x=37, y=53, width=2, height=2),
                ),
            )
        )
        runtime = BoundedWindowsOperatorRuntime(
            layout,
            presentation_runtime_factory=SyntheticPresentationRuntime,
        )
        started = await runtime.start(())
        assert started.state == WindowsOperatorRuntimeState.RUNNING
        assert started.open_surface_count == 2
        assert started.delivered_frames == 1
        assert started.presentations == 1

        completed = await runtime.wait()
        assert completed.state == WindowsOperatorRuntimeState.COMPLETE
        assert completed.open_surface_count == 2
        assert completed.presentations == 1

        closed = await runtime.close()
        assert closed.state == WindowsOperatorRuntimeState.CLOSED
        assert closed.open_surface_count == 0
        assert closed.presentations == 1

    asyncio.run(scenario())
