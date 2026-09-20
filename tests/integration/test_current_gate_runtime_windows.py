"""Win32 qualification for dispatcher-to-arbitrary-viewport runtime composition."""

from __future__ import annotations

import asyncio
import sys

import pytest

from k5vision.media.presentation_frame import PixelFormat, PresentationVideoFrame
from k5vision.media.viewport_dispatch import BoundedViewportDispatcher, ViewportDispatchState
from k5vision.media.viewport_geometry import ViewportGeometry, ViewportLayout, ViewportPlacement
from k5vision.media.windows_viewport_runtime import (
    BoundedWindowsViewportRuntime,
    WindowsViewportRuntimeState,
)

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only qualification")


def test_real_win32_runtime_routes_transient_frame_to_sparse_non_grid_target() -> None:
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
        runtime = BoundedWindowsViewportRuntime(layout)
        opened = await runtime.open()
        assert opened.state == WindowsViewportRuntimeState.OPEN
        assert opened.viewport_count == 2
        assert opened.open_surface_count == 2

        dispatcher = BoundedViewportDispatcher(runtime.bindings)
        frame = PresentationVideoFrame(
            payload=memoryview(bytearray([1, 2, 3, 0] * 4)),
            width=2,
            height=2,
            stride_bytes=8,
            pixel_format=PixelFormat.BGRX,
            source_elapsed_ms=1,
        )
        await dispatcher.dispatch(4095, frame)
        assert dispatcher.snapshot.delivered_frames == 1
        assert runtime.snapshot.presentations == 1

        dispatch_closed = await dispatcher.close()
        assert dispatch_closed.state == ViewportDispatchState.CLOSED
        runtime_closed = await runtime.close()
        assert runtime_closed.state == WindowsViewportRuntimeState.CLOSED
        assert runtime_closed.open_surface_count == 0

    asyncio.run(scenario())
