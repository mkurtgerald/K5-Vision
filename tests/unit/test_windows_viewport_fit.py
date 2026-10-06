"""Portable raster oracle for composed-frame viewport fitting; no native calls."""

from __future__ import annotations

import asyncio
import ctypes

import pytest

from k5vision.media.detection_overlay import (
    BoundedDetectionOverlayRenderer,
    DetectionOverlayObservation,
)
from k5vision.media.presentation_frame import PixelFormat, PresentationVideoFrame
from k5vision.media.windows_presentation_surface import (
    BoundedWindowsPresentationSurface,
    _Win32DibSurfaceApi,
)
from k5vision.media.windows_presentation_target import BoundedWindowsPresentationTarget

_GREEN = b"\x00\xff\x00\x00"
_BLACK = b"\x00\x00\x00\x00"


class RasterGDI(_Win32DibSurfaceApi):
    """Execute the product's real blit logic against inert, bounded pixel arrays."""

    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.source = b""
        self.source_width = 0
        self.source_height = 0
        self.target_width = 0
        self.target_height = 0
        self.target = bytearray()
        self.mode = 1

    def resize(self, width: int, height: int) -> None:
        self.target_width, self.target_height = width, height
        self.target = bytearray(b"\xff\x00\xff\x00" * width * height)

    def create_surface(self, width: int, height: int) -> tuple[int, int, int]:
        self.source_width, self.source_height = width, height
        return 3, 4, width * 4

    def copy_frame(self, bits_pointer: int, stride: int, frame: PresentationVideoFrame) -> None:
        self.source = b"".join(
            bytes(
                frame.payload[row * frame.stride_bytes : row * frame.stride_bytes + frame.width * 4]
            )
            for row in range(frame.height)
        )

    def destroy_surface(self, handle: int) -> None:
        self.calls.append(("destroy", handle))

    def _create_compatible_dc(self, target: ctypes.c_void_p) -> int:
        self.calls.append(("create_dc",))
        return 5

    def _select_object(self, dc: ctypes.c_void_p, handle: ctypes.c_void_p) -> int:
        self.calls.append(("select", handle.value))
        return 6

    def _delete_dc(self, dc: ctypes.c_void_p) -> int:
        self.calls.append(("delete_dc",))
        return 1

    def _set_stretch_blt_mode(self, dc: ctypes.c_void_p, mode: int) -> int:
        old, self.mode = self.mode, mode
        self.calls.append(("mode", mode))
        return old

    def _pat_blt(self, dc: object, x: int, y: int, width: int, height: int, rop: int) -> int:
        self.calls.append(("clear", x, y, width, height, rop))
        for row in range(y, min(y + height, self.target_height)):
            for col in range(x, min(x + width, self.target_width)):
                offset = (row * self.target_width + col) * 4
                self.target[offset : offset + 4] = _BLACK
        return 1

    def _copy(self, x: int, y: int, width: int, height: int, sw: int, sh: int) -> None:
        for row in range(height):
            for col in range(width):
                if y + row >= self.target_height or x + col >= self.target_width:
                    continue
                src = ((row * sh // height) * self.source_width + col * sw // width) * 4
                dst = ((y + row) * self.target_width + x + col) * 4
                self.target[dst : dst + 4] = self.source[src : src + 4]

    def _bit_blt(
        self,
        dc: object,
        x: int,
        y: int,
        w: int,
        h: int,
        source: object,
        sx: int,
        sy: int,
        rop: int,
    ) -> int:
        self.calls.append(("bit", x, y, w, h, sx, sy, rop))
        self._copy(x, y, w, h, w, h)
        return 1

    def _stretch_blt(
        self,
        dc: object,
        x: int,
        y: int,
        w: int,
        h: int,
        source: object,
        sx: int,
        sy: int,
        sw: int,
        sh: int,
        rop: int,
    ) -> int:
        self.calls.append(("stretch", x, y, w, h, sx, sy, sw, sh, rop))
        self._copy(x, y, w, h, sw, sh)
        return 1


class RasterTarget:
    def __init__(self, gdi: RasterGDI) -> None:
        self.gdi = gdi
        self.releases = 0
        self.destroyed = False

    def create_target(self, x: int, y: int, width: int, height: int) -> int:
        self.gdi.resize(width, height)
        return 1

    def client_size(self, target: int) -> tuple[int, int]:
        return self.gdi.target_width, self.gdi.target_height

    def acquire_dc(self, target: int) -> int:
        return 2

    def release_dc(self, target: int, dc: int) -> None:
        self.releases += 1

    def destroy_target(self, target: int) -> None:
        self.destroyed = True


def composed_frame(width: int, height: int) -> PresentationVideoFrame:
    frame = PresentationVideoFrame(
        payload=memoryview(bytes(width * height * 4)),
        width=width,
        height=height,
        stride_bytes=width * 4,
        pixel_format=PixelFormat.BGRX,
        source_elapsed_ms=17,
    )
    return (
        BoundedDetectionOverlayRenderer(border_width=2)
        .render(
            frame,
            (DetectionOverlayObservation("person", 0.9, 0.8, 0.1, 0.95, 0.5),),
        )
        .frame
    )


def test_composed_frame_fits_smaller_client_without_clipping_right_hand_box() -> None:
    async def scenario() -> None:
        gdi = RasterGDI()
        target = BoundedWindowsPresentationTarget(native_api=RasterTarget(gdi))
        surface = BoundedWindowsPresentationSurface(native_api=gdi)
        await target.open(128, 72)
        await surface.open()
        frame = composed_frame(192, 108)
        original = bytes(frame.payload)
        await surface.present(frame)
        try:
            await target.present(surface)
            assert gdi.target.count(_GREEN) > 0, "right-hand detection was clipped from viewport"
            assert ("stretch", 0, 0, 128, 72, 0, 0, 192, 108, 0x00CC0020) in gdi.calls
            assert bytes(frame.payload) == original
            assert surface.snapshot.presented_frames == 1
            assert surface.snapshot.blits == target.snapshot.presentations == 1
        finally:
            await target.close()
            await surface.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("source_size", "target_size", "expected"),
    [
        ((192, 108), (128, 72), (0, 0, 128, 72)),
        ((64, 36), (192, 108), (0, 0, 192, 108)),
        ((160, 90), (100, 100), (0, 22, 100, 56)),
        ((90, 160), (100, 100), (22, 0, 56, 100)),
        ((100, 100), (160, 90), (35, 0, 90, 90)),
        ((101, 57), (80, 79), (0, 17, 80, 45)),
        ((16_384, 1), (1, 1), (0, 0, 1, 1)),
        ((1, 16_384), (1, 1), (0, 0, 1, 1)),
    ],
)
def test_aspect_fit_uses_entire_source_and_clears_all_letterbox_pixels(
    source_size: tuple[int, int],
    target_size: tuple[int, int],
    expected: tuple[int, int, int, int],
) -> None:
    async def scenario() -> None:
        gdi = RasterGDI()
        target = BoundedWindowsPresentationTarget(native_api=RasterTarget(gdi))
        surface = BoundedWindowsPresentationSurface(native_api=gdi)
        await target.open(*target_size)
        await surface.open()
        await surface.present(composed_frame(*source_size))
        try:
            await target.present(surface)
            x, y, w, h = expected
            assert ("stretch", x, y, w, h, 0, 0, *source_size, 0x00CC0020) in gdi.calls
            tw, th = target_size
            for row in range(th):
                for col in range(tw):
                    if not (x <= col < x + w and y <= row < y + h):
                        offset = (row * tw + col) * 4
                        assert gdi.target[offset : offset + 4] == _BLACK
            assert gdi.mode == 1
            assert ("mode", 3) in gdi.calls and gdi.calls[-3] == ("mode", 1)
        finally:
            await target.close()
            await surface.close()

    asyncio.run(scenario())


def test_equal_size_has_byte_identity_and_no_scaling_or_clearing() -> None:
    async def scenario() -> None:
        gdi = RasterGDI()
        target = BoundedWindowsPresentationTarget(native_api=RasterTarget(gdi))
        surface = BoundedWindowsPresentationSurface(native_api=gdi)
        await target.open(20, 12)
        await surface.open()
        payload = bytes(range(240)) * 4
        frame = PresentationVideoFrame(
            payload=memoryview(payload),
            width=20,
            height=12,
            stride_bytes=80,
            pixel_format=PixelFormat.BGRX,
            source_elapsed_ms=23,
        )
        await surface.present(frame)
        try:
            await target.present(surface)
            assert bytes(gdi.target) == payload
            assert not any(call[0] in {"stretch", "clear", "mode"} for call in gdi.calls)
            assert ("bit", 0, 0, 20, 12, 0, 0, 0x00CC0020) in gdi.calls
        finally:
            await target.close()
            await surface.close()

    asyncio.run(scenario())


def test_resize_and_resolution_change_use_current_geometry_and_clear_old_bands() -> None:
    async def scenario() -> None:
        gdi = RasterGDI()
        target = BoundedWindowsPresentationTarget(native_api=RasterTarget(gdi))
        surface = BoundedWindowsPresentationSurface(native_api=gdi)
        await target.open(128, 72)
        await surface.open()
        try:
            await surface.present(composed_frame(192, 108))
            await target.present(surface)
            # OS client resize, independent of requested creation geometry.
            gdi.resize(100, 100)
            await target.present(surface)
            assert ("stretch", 0, 22, 100, 56, 0, 0, 192, 108, 0x00CC0020) in gdi.calls
            # The client is unchanged, but a new portrait source needs side bands.
            await surface.present(composed_frame(90, 160))
            await target.present(surface)
            assert ("stretch", 22, 0, 56, 100, 0, 0, 90, 160, 0x00CC0020) in gdi.calls
            assert gdi.target[: 22 * 4] == _BLACK * 22
            assert gdi.target[78 * 4 : 100 * 4] == _BLACK * 22
            assert surface.snapshot.surface_replacements == 1
            assert surface.snapshot.blits == target.snapshot.presentations == 3
        finally:
            await target.close()
            await surface.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "operation", ["clear", "mode", "stretch", "restore_mode", "restore_object", "delete_dc"]
)
def test_native_fit_failures_restore_resources_and_never_count_paint(operation: str) -> None:
    from k5vision.media.windows_presentation_surface import (
        WindowsPresentationSurfaceState,
    )
    from k5vision.media.windows_presentation_target import (
        WindowsPresentationTargetError,
        WindowsPresentationTargetState,
    )

    async def scenario() -> None:
        gdi = RasterGDI()
        native_target = RasterTarget(gdi)
        target = BoundedWindowsPresentationTarget(native_api=native_target)
        surface = BoundedWindowsPresentationSurface(native_api=gdi)
        await target.open(100, 100)
        await surface.open()
        await surface.present(composed_frame(192, 108))
        method = {
            "clear": "_pat_blt",
            "mode": "_set_stretch_blt_mode",
            "stretch": "_stretch_blt",
            "restore_mode": "_set_stretch_blt_mode",
            "restore_object": "_select_object",
            "delete_dc": "_delete_dc",
        }[operation]
        original = getattr(gdi, method)
        calls = 0

        def fail(*args: object) -> int:
            nonlocal calls
            calls += 1
            if operation in {"restore_mode", "restore_object"} and calls == 1:
                return original(*args)
            original(*args)
            return 0

        setattr(gdi, method, fail)
        with pytest.raises(WindowsPresentationTargetError):
            await target.present(surface)
        assert target.snapshot.presentations == surface.snapshot.blits == 0
        assert target.snapshot.state is WindowsPresentationTargetState.FAILED
        assert surface.snapshot.state is WindowsPresentationSurfaceState.FAILED
        assert native_target.releases == 1 and native_target.destroyed
        assert ("delete_dc",) in gdi.calls
        assert any(call[0] == "destroy" for call in gdi.calls)
        if operation == "stretch":
            assert gdi.mode == 1
        await target.close()
        await surface.close()

    asyncio.run(scenario())


def test_minimize_multiple_frames_restore_and_relayout_keep_truthful_counts() -> None:
    from types import SimpleNamespace

    from k5vision.media.analytics_overlay_delivery import BoundedAnalyticsOverlayDelivery
    from k5vision.media.live_presentation import LivePresentationSnapshot, LivePresentationState
    from k5vision.media.viewport_geometry import ViewportGeometry, ViewportLayout, ViewportPlacement
    from k5vision.media.windows_viewport_layout import BoundedWindowsViewportLayout
    from k5vision.media.windows_viewport_runtime import (
        BoundedWindowsViewportRuntime,
        WindowsViewportRuntimeState,
    )

    def layout(width: int, height: int) -> ViewportLayout:
        return ViewportLayout(
            placements=(
                ViewportPlacement(
                    logical_slot=0,
                    geometry=ViewportGeometry(x=0, y=0, width=width, height=height),
                ),
            )
        )

    async def scenario() -> None:
        gdi = RasterGDI()
        targets: list[BoundedWindowsPresentationTarget] = []
        layouts: list[BoundedWindowsViewportLayout] = []
        surface = BoundedWindowsPresentationSurface(native_api=gdi)

        def target_factory() -> BoundedWindowsPresentationTarget:
            target = BoundedWindowsPresentationTarget(native_api=RasterTarget(gdi))
            targets.append(target)
            return target

        def layout_factory(value: ViewportLayout) -> BoundedWindowsViewportLayout:
            result = BoundedWindowsViewportLayout(value, target_factory=target_factory)
            layouts.append(result)
            return result

        viewport = BoundedWindowsViewportRuntime(
            layout(128, 72),
            surface_factory=lambda: surface,
            layout_factory=layout_factory,
        )
        painted: list[int] = []

        class Runner:
            async def run(self, source: str, consumer: object) -> LivePresentationSnapshot:
                for index in range(6):
                    if index == 1:
                        gdi.resize(0, 0)
                    if index == 4:
                        gdi.resize(100, 100)
                    await consumer(composed_frame(192, 108))
                    painted.append(viewport.snapshot.presentations)
                    assert viewport.snapshot.state is WindowsViewportRuntimeState.OPEN
                    await asyncio.sleep(0.001)
                return LivePresentationSnapshot(
                    state=LivePresentationState.COMPLETE,
                    decoder_initialized=True,
                    accepted_packets=0,
                    rtp_valid_packets=0,
                    rtp_invalid_packets=0,
                    rtp_delivered_bytes=0,
                    delivered_frames=6,
                    delivered_frame_bytes=6 * 192 * 108 * 4,
                    source_span_ms=17,
                )

        async def provider(frame: PresentationVideoFrame) -> tuple[object, ...]:
            return (
                SimpleNamespace(
                    track_id="generated",
                    category="person",
                    confidence=0.9,
                    box=SimpleNamespace(x_min=0.8, y_min=0.1, x_max=0.95, y_max=0.5),
                ),
            )

        delivery = BoundedAnalyticsOverlayDelivery(Runner(), provider)
        await viewport.open()
        try:
            result = await delivery.run("inert-generated", viewport.bindings[0].consumer)
            assert painted == [1, 1, 1, 1, 2, 3]
            assert result.delivered_frames == delivery.snapshot.processed_frames == 6
            assert delivery.snapshot.analytics_failures == 0
            # Composed boxes remain truthful and distinct from painted frames.
            assert delivery.snapshot.rendered_boxes == 5
            assert surface.snapshot.presented_frames == 6
            assert surface.snapshot.blits == targets[0].snapshot.presentations == 3
            assert layouts[0].snapshot.presentations == viewport.snapshot.presentations == 3
            assert gdi.target.count(_GREEN) > 0
            await viewport.relayout(layout(160, 90))
            await viewport.present(0, composed_frame(192, 108))
            assert len(targets) == 2
            assert viewport.snapshot.presentations == layouts[0].snapshot.presentations == 4
            assert targets[1].snapshot.presentations == 1
            assert ("stretch", 0, 0, 160, 90, 0, 0, 192, 108, 0x00CC0020) in gdi.calls
            # Close is valid after another deferred frame; no retry/background loop.
            gdi.resize(0, 0)
            await viewport.present(0, composed_frame(192, 108))
            assert viewport.snapshot.presentations == 4
        finally:
            await viewport.close()
        assert viewport.snapshot.state is WindowsViewportRuntimeState.CLOSED
        assert not any(target.snapshot.target_open for target in targets)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "dimensions", [(0, 1), (1, 0), (-1, 1), (True, 1), (1, None), (None, 1), (16_385, 1)]
)
def test_surface_rejects_invalid_destination_without_native_calls(
    dimensions: tuple[object, object],
) -> None:
    from k5vision.media.windows_presentation_surface import WindowsPresentationSurfaceError

    async def scenario() -> None:
        gdi = RasterGDI()
        surface = BoundedWindowsPresentationSurface(native_api=gdi)
        await surface.open()
        await surface.present(composed_frame(20, 10))
        with pytest.raises(WindowsPresentationSurfaceError):
            await surface.blit(2, target_width=dimensions[0], target_height=dimensions[1])
        assert surface.snapshot.blits == 0
        assert not any(call[0] == "create_dc" for call in gdi.calls)
        await surface.close()

    asyncio.run(scenario())


def test_new_win32_functions_have_pointer_safe_signatures(monkeypatch: pytest.MonkeyPatch) -> None:
    import k5vision.media.windows_presentation_surface as surface_module
    import k5vision.media.windows_presentation_target as target_module

    class Function:
        pass

    class Library:
        def __init__(self) -> None:
            self.functions: dict[str, Function] = {}

        def __getattr__(self, name: str) -> Function:
            return self.functions.setdefault(name, Function())

    libraries: dict[str, Library] = {}

    def load(name: str, **_kwargs: object) -> Library:
        return libraries.setdefault(name, Library())

    monkeypatch.setattr(surface_module.sys, "platform", "win32")
    monkeypatch.setattr(ctypes, "WinDLL", load, raising=False)
    surface_module._Win32DibSurfaceApi()
    target_module._Win32WindowTargetApi()
    gdi, user = libraries["gdi32"].functions, libraries["user32"].functions
    assert gdi["StretchBlt"].argtypes == [
        ctypes.c_void_p,
        *([ctypes.c_int] * 4),
        ctypes.c_void_p,
        *([ctypes.c_int] * 4),
        ctypes.c_uint32,
    ]
    assert gdi["StretchBlt"].restype is ctypes.c_int
    assert gdi["SetStretchBltMode"].argtypes == [ctypes.c_void_p, ctypes.c_int]
    assert gdi["PatBlt"].argtypes == [ctypes.c_void_p, *([ctypes.c_int] * 4), ctypes.c_uint32]
    assert user["GetClientRect"].argtypes == [
        ctypes.c_void_p,
        ctypes.POINTER(target_module._ClientRect),
    ]
    assert user["GetClientRect"].restype is ctypes.c_int
    assert user["GetAncestor"].argtypes == [ctypes.c_void_p, ctypes.c_uint32]
    assert user["GetAncestor"].restype is ctypes.c_void_p
    assert user["IsIconic"].argtypes == [ctypes.c_void_p]
    assert user["IsIconic"].restype is ctypes.c_int
