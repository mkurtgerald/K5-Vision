from __future__ import annotations

import asyncio
import ctypes
import sys

import pytest

from k5vision.media.presentation_frame import PixelFormat, PresentationVideoFrame
from k5vision.media.windows_presentation_surface import (
    _HGDI_ERROR,
    BoundedWindowsPresentationSurface,
    _Win32DibSurfaceApi,
)


pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only gate qualification")


def test_real_gdi_target_receives_exact_surface_bytes() -> None:
    async def scenario() -> None:
        native = _Win32DibSurfaceApi()
        surface = BoundedWindowsPresentationSurface(native_api=native)
        payload = bytes(
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
        frame = PresentationVideoFrame(
            payload=memoryview(payload),
            width=2,
            height=2,
            stride_bytes=8,
            pixel_format=PixelFormat.BGRX,
            source_elapsed_ms=1,
        )

        await surface.open()
        await surface.present(frame)

        destination_handle, destination_bits, _ = native.create_surface(2, 2)
        gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        create_compatible_dc = gdi32.CreateCompatibleDC
        select_object = gdi32.SelectObject
        delete_dc = gdi32.DeleteDC
        create_compatible_dc.argtypes = [ctypes.c_void_p]
        create_compatible_dc.restype = ctypes.c_void_p
        select_object.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        select_object.restype = ctypes.c_void_p
        delete_dc.argtypes = [ctypes.c_void_p]
        delete_dc.restype = ctypes.c_int

        target_dc = int(create_compatible_dc(None) or 0)
        assert target_dc != 0
        previous = int(
            select_object(
                ctypes.c_void_p(target_dc),
                ctypes.c_void_p(destination_handle),
            )
            or 0
        )
        assert previous not in {0, _HGDI_ERROR}

        try:
            await surface.blit(target_dc)
            assert ctypes.string_at(destination_bits, len(payload)) == payload
            assert surface.snapshot.blits == 1
        finally:
            select_object(ctypes.c_void_p(target_dc), ctypes.c_void_p(previous))
            delete_dc(ctypes.c_void_p(target_dc))
            native.destroy_surface(destination_handle)
            await surface.close()

    asyncio.run(scenario())
