"""Win32 qualification for the current presentation target boundary.

The pixel witness owns one disposable topmost window containing generated pixels.
It requires a readable client DC on the Windows runner: occlusion, unsupported
GetPixel, or a changed pixel palette fails qualification rather than skipping or
falling back to a desktop/screen capture. Only bounded aggregate counts are logged.
This is presentation-component evidence, not full operator or installer acceptance.
"""

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
        opened = await target.open(2, 2, x=17, y=29)
        assert opened.state == WindowsPresentationTargetState.OPEN
        assert opened.target_open
        assert opened.x == 17
        assert opened.y == 29

        presented = await target.present(surface)
        assert presented.presentations == 1
        assert presented.x == 17
        assert presented.y == 29
        assert surface.snapshot.blits == 1

        closed = await target.close()
        assert closed.state == WindowsPresentationTargetState.CLOSED
        assert not closed.target_open
        assert closed.x == 0
        assert closed.y == 0
        await surface.close()

    asyncio.run(scenario())


# This qualification is intentionally bound to the merged presentation component.
# Test-only successors must not silently qualify different installed product bytes.
_PIXEL_PRODUCT_REVISION = "6990e51b4101d6738ecb1279cea44df2797dbb07"
_PIXEL_PRODUCT_FILES = {
    "media/detection_overlay.py": (
        "b7cb839d8f370c2072dc39b98f653304272a2712fe2ca934729add202d224dbd"
    ),
    "media/presentation_frame.py": (
        "e4e55e4df45742eedccc04e4331038acd35d40288ccbf50902e331a001fdc881"
    ),
    "media/windows_presentation_surface.py": (
        "433672cc3d87311f81f7757ce8b9de603da34f101e3a038ee48089cdee985b81"
    ),
    "media/windows_presentation_target.py": (
        "8d032eb4d98cae27633356b635817b09ae9c5a40767f1d7776d2cf19d2f368f6"
    ),
    "media/windows_viewport_layout.py": (
        "f3d01e9f7d23fbdaf155f1a6ebf3c69a6569cd3d97bc438d7eb06a1e06cf955c"
    ),
    "media/windows_viewport_runtime.py": (
        "adbc33b6db626b2d174718ef5ec00a64604a59a08feb4db500f31092d60032ef"
    ),
}
_MAX_PIXEL_CLIENT = 192
_GRAY_COLORREF = 0x00202020
_GREEN_COLORREF = 0x0000FF00
_CLR_INVALID = 0xFFFFFFFF
# Independent fixed expected geometry; never use the production fitting helper.
_PIXEL_CASES = {
    "same_size": (192, 108, 0, 0, 192, 108),
    "downscale": (128, 72, 0, 0, 128, 72),
    "letterbox": (128, 96, 0, 12, 128, 72),
    "client_resize": (96, 128, 0, 37, 96, 54),
    "restored": (96, 128, 0, 37, 96, 54),
}
_PIXEL_STAT_FIELDS = {
    "sampled_pixels",
    "green_pixels",
    "gray_pixels",
    "black_letterbox_pixels",
    "letterbox_mismatches",
    "unexpected_colors",
    "outside_box_green",
    "required_green_pixels",
    "required_gray_pixels",
    "green_mismatches",
    "gray_mismatches",
    "left_edge_green",
    "right_edge_green",
    "top_edge_green",
    "bottom_edge_green",
}


def _bound_pixel_product() -> str:
    import hashlib
    import importlib
    from pathlib import Path

    import k5vision

    package = Path(k5vision.__file__).resolve().parent
    for relative, expected in _PIXEL_PRODUCT_FILES.items():
        module = importlib.import_module("k5vision." + relative[:-3].replace("/", "."))
        path = Path(module.__file__).resolve()
        if path != package / relative:
            raise AssertionError("pixel witness product package is mixed")
        # Windows checkout line endings are not runtime source changes.
        data = path.read_bytes().replace(b"\r\n", b"\n")
        if hashlib.sha256(data).hexdigest() != expected:
            raise AssertionError("pixel witness product source binding failed")
    return hashlib.sha256(
        "\n".join(f"{path}:{sha}" for path, sha in sorted(_PIXEL_PRODUCT_FILES.items())).encode()
    ).hexdigest()


def _generated_box_frame(elapsed: int) -> PresentationVideoFrame:
    from k5vision.media.detection_overlay import (
        BoundedDetectionOverlayRenderer,
        DetectionOverlayObservation,
    )

    frame = PresentationVideoFrame(
        payload=memoryview(b"\x20\x20\x20\x00" * (192 * 108)),
        width=192,
        height=108,
        stride_bytes=192 * 4,
        pixel_format=PixelFormat.BGRX,
        source_elapsed_ms=elapsed,
    )
    result = BoundedDetectionOverlayRenderer(border_width=6).render(
        frame,
        (DetectionOverlayObservation("generated-box", 1.0, 0.8, 0.2, 0.95, 0.8),),
    )
    if result.snapshot.rendered_boxes != 1:
        raise AssertionError("generated pixel witness fixture is invalid")
    return result.frame


def _pixel_statistics(
    geometry: tuple[int, int, int, int, int, int], read_pixel: object
) -> dict[str, int]:
    """Read the complete bounded owned client region; retain aggregate counts only."""
    width, height, x, y, fitted_width, fitted_height = geometry
    if not all(type(v) is int for v in geometry) or not (
        1 <= width <= _MAX_PIXEL_CLIENT
        and 1 <= height <= _MAX_PIXEL_CLIENT
        and 0 <= x < width
        and 0 <= y < height
        and 1 <= fitted_width <= width - x
        and 1 <= fitted_height <= height - y
    ):
        raise AssertionError("pixel witness geometry exceeds its bound")
    stats = dict.fromkeys(_PIXEL_STAT_FIELDS, 0)
    # Independently specified source-pixel rectangle: outer [153,183)x[21,87),
    # gray interior [159,177)x[27,81). Require the complete green core bands and
    # gray core regions, allowing only a one-destination-pixel transition band
    # around each edge for native COLORONCOLOR rounding. No production fit helper.
    outer_left, outer_right = x + 153 * fitted_width / 192, x + 183 * fitted_width / 192
    outer_top, outer_bottom = y + 21 * fitted_height / 108, y + 87 * fitted_height / 108
    inner_left, inner_right = x + 159 * fitted_width / 192, x + 177 * fitted_width / 192
    inner_top, inner_bottom = y + 27 * fitted_height / 108, y + 81 * fitted_height / 108
    for row in range(height):
        for col in range(width):
            color = read_pixel(col, row)
            if type(color) is not int or color == _CLR_INVALID:
                raise AssertionError("owned target pixel readback is unavailable")
            stats["sampled_pixels"] += 1
            inside = x <= col < x + fitted_width and y <= row < y + fitted_height
            if not inside:
                if color == 0:
                    stats["black_letterbox_pixels"] += 1
                else:
                    stats["letterbox_mismatches"] += 1
                continue
            px, py = col + 0.5, row + 0.5
            inside_outer_core = (
                outer_left + 1 <= px < outer_right - 1 and outer_top + 1 <= py < outer_bottom - 1
            )
            outside_inner_band = (
                px < inner_left - 1
                or px >= inner_right + 1
                or py < inner_top - 1
                or py >= inner_bottom + 1
            )
            required_green = inside_outer_core and outside_inner_band
            outside_outer_band = (
                px < outer_left - 1
                or px >= outer_right + 1
                or py < outer_top - 1
                or py >= outer_bottom + 1
            )
            inside_gray_core = (
                inner_left + 1 <= px < inner_right - 1 and inner_top + 1 <= py < inner_bottom - 1
            )
            required_gray = outside_outer_band or inside_gray_core
            if required_green:
                stats["required_green_pixels"] += 1
                if color != _GREEN_COLORREF:
                    stats["green_mismatches"] += 1
            if required_gray:
                stats["required_gray_pixels"] += 1
                if color != _GRAY_COLORREF:
                    stats["gray_mismatches"] += 1
            if color == _GRAY_COLORREF:
                stats["gray_pixels"] += 1
            elif color == _GREEN_COLORREF:
                stats["green_pixels"] += 1
                if outside_outer_band:
                    stats["outside_box_green"] += 1
                if required_green:
                    if px < inner_left - 1:
                        stats["left_edge_green"] += 1
                    if px >= inner_right + 1:
                        stats["right_edge_green"] += 1
                    if py < inner_top - 1:
                        stats["top_edge_green"] += 1
                    if py >= inner_bottom + 1:
                        stats["bottom_edge_green"] += 1
            else:
                stats["unexpected_colors"] += 1
    return stats


def _assert_pixel_statistics(
    geometry: tuple[int, int, int, int, int, int], stats: dict[str, int]
) -> None:
    width, height, _x, _y, fitted_width, fitted_height = geometry
    if stats.keys() != _PIXEL_STAT_FIELDS or any(
        type(v) is not int or v < 0 for v in stats.values()
    ):
        raise AssertionError("pixel witness counters are invalid")
    if stats["sampled_pixels"] != width * height:
        raise AssertionError("pixel witness did not cover the owned client")
    if stats["black_letterbox_pixels"] != width * height - fitted_width * fitted_height:
        raise AssertionError("pixel witness letterbox was not cleared")
    if any(
        stats[k]
        for k in (
            "letterbox_mismatches",
            "unexpected_colors",
            "outside_box_green",
            "green_mismatches",
            "gray_mismatches",
        )
    ):
        raise AssertionError("pixel witness composition does not match the generated fixture")
    if any(
        stats[k] < 1
        for k in (
            "green_pixels",
            "gray_pixels",
            "required_green_pixels",
            "required_gray_pixels",
            "left_edge_green",
            "right_edge_green",
            "top_edge_green",
            "bottom_edge_green",
        )
    ):
        raise AssertionError("pixel witness did not observe the complete right-edge box")


def _owned_pixel_api() -> object:
    """Construct Win32 access only inside the opted-in Windows test body."""
    import ctypes

    from k5vision.media.windows_presentation_target import _Win32WindowTargetApi

    class OwnedPixelTargetApi(_Win32WindowTargetApi):
        def __init__(self) -> None:
            super().__init__()
            self.owned_target = 0
            self.dc_acquisitions = 0
            self.dc_releases = 0
            self._owned_dcs: set[int] = set()
            self._pixel_gdi = ctypes.WinDLL("gdi32", use_last_error=True)
            self._get_pixel = self._pixel_gdi.GetPixel
            self._get_pixel.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
            self._get_pixel.restype = ctypes.c_uint32
            self._gdi_flush = self._pixel_gdi.GdiFlush
            self._gdi_flush.argtypes = []
            self._gdi_flush.restype = ctypes.c_int
            self._pat_blt = self._pixel_gdi.PatBlt
            self._pat_blt.argtypes = [ctypes.c_void_p, *([ctypes.c_int] * 4), ctypes.c_uint32]
            self._pat_blt.restype = ctypes.c_int
            self._set_window_pos = self._user32.SetWindowPos
            self._set_window_pos.argtypes = [
                ctypes.c_void_p,
                ctypes.c_void_p,
                *([ctypes.c_int] * 4),
                ctypes.c_uint32,
            ]
            self._set_window_pos.restype = ctypes.c_int
            self._show_window = self._user32.ShowWindow
            self._show_window.argtypes = [ctypes.c_void_p, ctypes.c_int]
            self._show_window.restype = ctypes.c_int
            self._update_window = self._user32.UpdateWindow
            self._update_window.argtypes = [ctypes.c_void_p]
            self._update_window.restype = ctypes.c_int

        def create_target(self, x: int, y: int, width: int, height: int) -> int:
            if self.owned_target:
                raise AssertionError("pixel witness already owns a target")
            handle = super().create_target(x, y, width, height)
            self.owned_target = handle
            return handle

        def _require_owned(self, handle: int) -> None:
            if type(handle) is not int or handle <= 0 or handle != self.owned_target:
                raise AssertionError("pixel witness refused an unowned target")

        def acquire_dc(self, target: int) -> int:
            self._require_owned(target)
            if self._owned_dcs:
                raise AssertionError("pixel witness already owns a DC")
            result = super().acquire_dc(target)
            self._owned_dcs.add(result)
            self.dc_acquisitions += 1
            return result

        def release_dc(self, target: int, target_dc: int) -> None:
            self._require_owned(target)
            if target_dc not in self._owned_dcs:
                raise AssertionError("pixel witness refused an unowned DC")
            super().release_dc(target, target_dc)
            self._owned_dcs.remove(target_dc)
            self.dc_releases += 1

        def destroy_target(self, target: int) -> None:
            self._require_owned(target)
            if self._owned_dcs:
                raise AssertionError("pixel witness target still owns a DC")
            super().destroy_target(target)
            self.owned_target = 0

        def resize_owned(self, width: int, height: int) -> None:
            self._require_owned(self.owned_target)
            if not all(type(v) is int and 1 <= v <= _MAX_PIXEL_CLIENT for v in (width, height)):
                raise AssertionError("pixel witness resize exceeds its bound")
            # Only this disposable generated-content window is positioned/topmost.
            if not self._set_window_pos(
                ctypes.c_void_p(self.owned_target),
                ctypes.c_void_p(-1),
                17,
                29,
                width,
                height,
                0x0010,
            ):
                raise AssertionError("owned target resize failed")
            self._update_window(ctypes.c_void_p(self.owned_target))
            if self.client_size(self.owned_target) != (width, height):
                raise AssertionError("owned target actual client geometry differs")

        def minimize_owned(self) -> None:
            self._require_owned(self.owned_target)
            # ShowWindow returns previous visibility, not a success boolean.
            self._show_window(ctypes.c_void_p(self.owned_target), 6)
            if not self._is_iconic(ctypes.c_void_p(self.owned_target)):
                raise AssertionError("owned target did not minimize")

        def restore_owned(self) -> None:
            self._require_owned(self.owned_target)
            self._show_window(ctypes.c_void_p(self.owned_target), 9)
            if self._is_iconic(ctypes.c_void_p(self.owned_target)):
                raise AssertionError("owned target did not restore")
            self._update_window(ctypes.c_void_p(self.owned_target))

        def _geometry(self, geometry: tuple[int, int, int, int, int, int]) -> tuple[int, int]:
            self._require_owned(self.owned_target)
            width, height = self.client_size(self.owned_target)
            if (width, height) != geometry[:2] or not (
                1 <= width <= _MAX_PIXEL_CLIENT and 1 <= height <= _MAX_PIXEL_CLIENT
            ):
                raise AssertionError("owned target pixel probe geometry is invalid")
            return width, height

        def clear_sentinel(self, geometry: tuple[int, int, int, int, int, int]) -> None:
            width, height = self._geometry(geometry)
            dc = self.acquire_dc(self.owned_target)
            try:
                # White is absent from our generated video/box/letterbox palette.
                if not self._pat_blt(ctypes.c_void_p(dc), 0, 0, width, height, 0x00FF0062):
                    raise AssertionError("owned target sentinel clear failed")
                if not self._gdi_flush():
                    raise AssertionError("owned target sentinel flush failed")
            finally:
                self.release_dc(self.owned_target, dc)

        def read_statistics(self, geometry: tuple[int, int, int, int, int, int]) -> dict[str, int]:
            self._geometry(geometry)
            dc = self.acquire_dc(self.owned_target)
            try:
                if not self._gdi_flush():
                    raise AssertionError("owned target pixel flush failed")
                return _pixel_statistics(
                    geometry, lambda x, y: int(self._get_pixel(ctypes.c_void_p(dc), x, y))
                )
            finally:
                self.release_dc(self.owned_target, dc)

    return OwnedPixelTargetApi()


def test_real_owned_client_pixels_fit_box_and_restore_after_minimize(
    capsys: pytest.CaptureFixture[str],
) -> None:
    import json

    from k5vision.media.viewport_geometry import ViewportGeometry, ViewportLayout, ViewportPlacement
    from k5vision.media.windows_viewport_layout import BoundedWindowsViewportLayout
    from k5vision.media.windows_viewport_runtime import (
        BoundedWindowsViewportRuntime,
        WindowsViewportRuntimeState,
    )

    product_digest = _bound_pixel_product()

    async def scenario() -> dict[str, object]:
        native = _owned_pixel_api()
        target = BoundedWindowsPresentationTarget(native_api=native)
        surface = BoundedWindowsPresentationSurface()
        layout = ViewportLayout(
            placements=(
                ViewportPlacement(
                    logical_slot=0,
                    geometry=ViewportGeometry(x=17, y=29, width=192, height=108),
                ),
            )
        )
        viewport = BoundedWindowsViewportRuntime(
            layout,
            surface_factory=lambda: surface,
            layout_factory=lambda value: BoundedWindowsViewportLayout(
                value, target_factory=lambda: target
            ),
        )
        cases: dict[str, dict[str, int]] = {}
        consumed = 0
        painted = 0
        try:
            await viewport.open()
            for name, geometry in _PIXEL_CASES.items():
                if name == "restored":
                    native.minimize_owned()
                    acquisitions = native.dc_acquisitions
                    for _ in range(3):
                        consumed += 1
                        await viewport.present(0, _generated_box_frame(consumed))
                        assert viewport.snapshot.state is WindowsViewportRuntimeState.OPEN
                        assert target.snapshot.state is WindowsPresentationTargetState.OPEN
                        assert viewport.snapshot.presentations == painted
                        assert target.snapshot.presentations == surface.snapshot.blits == painted
                    assert native.dc_acquisitions == acquisitions
                    native.restore_owned()
                native.resize_owned(*geometry[:2])
                native.clear_sentinel(geometry)
                consumed += 1
                await viewport.present(0, _generated_box_frame(consumed))
                painted += 1
                stats = native.read_statistics(geometry)
                _assert_pixel_statistics(geometry, stats)
                cases[name] = stats
                assert surface.snapshot.presented_frames == consumed
                assert viewport.snapshot.presentations == target.snapshot.presentations == painted
                assert surface.snapshot.blits == painted
            # Close remains deterministic after one more ordinary deferred frame.
            native.minimize_owned()
            consumed += 1
            await viewport.present(0, _generated_box_frame(consumed))
            assert surface.snapshot.presented_frames == consumed
            assert viewport.snapshot.presentations == painted
        finally:
            # Runtime cleanup attempts layout and surface cleanup even if one fails.
            await viewport.close()
        if native.owned_target != 0:
            raise AssertionError("owned target cleanup is incomplete")
        assert native.dc_acquisitions == native.dc_releases
        assert viewport.snapshot.open_surface_count == 0
        assert viewport.snapshot.state is WindowsViewportRuntimeState.CLOSED
        return {
            "schema_version": "1",
            "qualification": "owned-generated-win32-client-pixels",
            "product_revision": _PIXEL_PRODUCT_REVISION,
            "product_source_sha256": product_digest,
            "cases": cases,
            "consumed_frames": consumed,
            "painted_frames": painted,
            "deferred_frames": 4,
            "cleanup_complete": True,
        }

    receipt = asyncio.run(scenario())
    # The current hosted job retains only this bounded scalar line, never pixels.
    with capsys.disabled():
        print("K5_NATIVE_VIEWPORT_PIXEL_RECEIPT=" + json.dumps(receipt, sort_keys=True))
