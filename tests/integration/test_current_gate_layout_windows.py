"""Win32 qualification for arbitrary viewport layout composition."""

from __future__ import annotations

import asyncio
import sys

import pytest

from k5vision.media.presentation_frame import PixelFormat, PresentationVideoFrame
from k5vision.media.viewport_geometry import ViewportGeometry, ViewportLayout, ViewportPlacement
from k5vision.media.windows_presentation_surface import BoundedWindowsPresentationSurface
from k5vision.media.windows_viewport_layout import (
    BoundedWindowsViewportLayout,
    WindowsViewportLayoutState,
)

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only qualification")


def test_real_win32_layout_opens_routes_and_closes_non_grid_targets() -> None:
    async def scenario() -> None:
        surface = BoundedWindowsPresentationSurface()
        frame = PresentationVideoFrame(
            payload=memoryview(bytearray([1, 2, 3, 0] * 4)),
            width=2,
            height=2,
            stride_bytes=8,
            pixel_format=PixelFormat.BGRX,
            source_elapsed_ms=1,
        )
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
        targets = BoundedWindowsViewportLayout(layout)

        await surface.open()
        await surface.present(frame)
        opened = await targets.open()
        assert opened.state == WindowsViewportLayoutState.OPEN
        assert opened.target_count == 2
        assert opened.open_target_count == 2

        first = await targets.present(7, surface)
        second = await targets.present(4095, surface)
        assert first.presentations == 1
        assert second.presentations == 2
        assert surface.snapshot.blits == 2

        closed = await targets.close()
        assert closed.state == WindowsViewportLayoutState.CLOSED
        assert closed.open_target_count == 0
        await surface.close()

    asyncio.run(scenario())
