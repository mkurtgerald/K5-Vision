"""Win32 qualification for the current presentation target boundary."""

from __future__ import annotations

import asyncio
import sys

import pytest

from k5vision.media.presentation_frame import PixelFormat, PresentationVideoFrame
from k5vision.media.windows_presentation_surface import BoundedWindowsPresentationSurface
from k5vision.media.windows_presentation_target import (
    BoundedWindowsPresentationTarget,
    WindowsPresentationTargetState,
)

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only qualification")


def test_real_win32_target_accepts_the_accepted_surface() -> None:
    async def scenario() -> None:
        surface = BoundedWindowsPresentationSurface()
        target = BoundedWindowsPresentationTarget()
        frame = PresentationVideoFrame(
            payload=memoryview(
                bytearray(
                    [
                        1,
                        2,
                        3,
                        0,
                        4,
                        5,
                        6,
                        0,
                        7,
                        8,
                        9,
                        0,
                        10,
                        11,
                        12,
                        0,
                    ]
                )
            ),
            width=2,
            height=2,
            stride_bytes=8,
            pixel_format=PixelFormat.BGRX,
            source_elapsed_ms=1,
        )

        await surface.open()
        await surface.present(frame)
        opened = await target.open(2, 2)
        assert opened.state == WindowsPresentationTargetState.OPEN
        assert opened.target_open

        presented = await target.present(surface)
        assert presented.presentations == 1
        assert surface.snapshot.blits == 1

        closed = await target.close()
        assert closed.state == WindowsPresentationTargetState.CLOSED
        assert not closed.target_open
        await surface.close()

    asyncio.run(scenario())
