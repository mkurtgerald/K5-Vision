from __future__ import annotations

import asyncio

import pytest

from k5vision.media.presentation_frame import PixelFormat, PresentationVideoFrame
from k5vision.media.viewport_dispatch import BoundedViewportDispatcher
from k5vision.media.viewport_geometry import ViewportGeometry, ViewportLayout, ViewportPlacement
from k5vision.media.windows_viewport_runtime import (
    BoundedWindowsViewportRuntime,
    WindowsViewportRuntimeError,
    WindowsViewportRuntimeErrorCode,
    WindowsViewportRuntimeState,
)


class FakeSurface:
    def __init__(self) -> None:
        self.opened = 0
        self.presented: list[PresentationVideoFrame] = []
        self.closed = 0
        self.fail_open = False
        self.fail_present = False
        self.fail_close = False

    async def open(self) -> object:
        self.opened += 1
        if self.fail_open:
            raise RuntimeError("SECRET surface open detail")
        return object()

    async def present(self, frame: PresentationVideoFrame) -> None:
        if self.fail_present:
            raise RuntimeError("SECRET surface present detail")
        self.presented.append(frame)

    async def close(self) -> object:
        self.closed += 1
        if self.fail_close:
            raise RuntimeError("SECRET surface cleanup detail")
        return object()


class FakeLayout:
    def __init__(self, layout: ViewportLayout) -> None:
        self.layout = layout
        self.opened = 0
        self.presented: list[tuple[int, object]] = []
        self.closed = 0
        self.fail_open = False
        self.fail_present = False
        self.fail_close = False

    async def open(self) -> object:
        self.opened += 1
        if self.fail_open:
            raise RuntimeError("SECRET layout open detail")
        return object()

    async def present(self, logical_slot: int, surface: object) -> object:
        if self.fail_present:
            raise RuntimeError("SECRET layout present detail")
        self.presented.append((logical_slot, surface))
        return object()

    async def close(self) -> object:
        self.closed += 1
        if self.fail_close:
            raise RuntimeError("SECRET layout cleanup detail")
        return object()


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


def _frame() -> PresentationVideoFrame:
    return PresentationVideoFrame(
        payload=memoryview(bytearray([1, 2, 3, 0] * 4)),
        width=2,
        height=2,
        stride_bytes=8,
        pixel_format=PixelFormat.BGRX,
        source_elapsed_ms=9,
    )


def test_dispatch_bridge_routes_sparse_slot_without_fixed_grid_or_identity_snapshot() -> None:
    surfaces: list[FakeSurface] = []
    layouts: list[FakeLayout] = []
    requested_layout = _layout()

    def surface_factory() -> FakeSurface:
        surface = FakeSurface()
        surfaces.append(surface)
        return surface

    def layout_factory(layout: ViewportLayout) -> FakeLayout:
        target = FakeLayout(layout)
        layouts.append(target)
        return target

    runtime = BoundedWindowsViewportRuntime(
        requested_layout,
        surface_factory=surface_factory,
        layout_factory=layout_factory,
    )

    async def scenario() -> tuple[object, object]:
        opened = await runtime.open()
        dispatcher = BoundedViewportDispatcher(runtime.bindings)
        await dispatcher.dispatch(4095, _frame())
        dispatch_snapshot = await dispatcher.close()
        closed = await runtime.close()
        return dispatch_snapshot, closed

    dispatch_snapshot, closed = asyncio.run(scenario())
    assert layouts[0].layout is requested_layout
    assert [item.logical_slot for item in requested_layout.placements] == [7, 4095]
    assert [surface.opened for surface in surfaces] == [1, 1]
    assert surfaces[0].presented == []
    assert len(surfaces[1].presented) == 1
    assert layouts[0].presented == [(4095, surfaces[1])]
    assert dispatch_snapshot.delivered_frames == 1
    assert closed.state == WindowsViewportRuntimeState.CLOSED
    assert closed.open_surface_count == 0
    assert closed.presentations == 1

    serialized = closed.model_dump_json().casefold()
    for forbidden in (
        "4095",
        "logical_slot",
        "rtsp://",
        "source_id",
        "recording_id",
        "path",
        "handle",
        "pointer",
        "payload",
    ):
        assert forbidden not in serialized


def test_partial_surface_open_failure_fails_closed_and_sanitizes_error() -> None:
    surfaces: list[FakeSurface] = []

    def surface_factory() -> FakeSurface:
        surface = FakeSurface()
        if surfaces:
            surface.fail_open = True
        surfaces.append(surface)
        return surface

    runtime = BoundedWindowsViewportRuntime(_layout(), surface_factory=surface_factory)
    with pytest.raises(WindowsViewportRuntimeError) as exc_info:
        asyncio.run(runtime.open())

    assert exc_info.value.code == WindowsViewportRuntimeErrorCode.SURFACE_OPEN_FAILURE
    assert "secret" not in str(exc_info.value).casefold()
    assert runtime.snapshot.state == WindowsViewportRuntimeState.FAILED
    assert runtime.snapshot.open_surface_count == 0
    assert [surface.closed for surface in surfaces] == [1, 1]


def test_layout_open_failure_closes_surfaces_and_layout() -> None:
    surfaces: list[FakeSurface] = []
    layouts: list[FakeLayout] = []

    def surface_factory() -> FakeSurface:
        surface = FakeSurface()
        surfaces.append(surface)
        return surface

    def layout_factory(layout: ViewportLayout) -> FakeLayout:
        target = FakeLayout(layout)
        target.fail_open = True
        layouts.append(target)
        return target

    runtime = BoundedWindowsViewportRuntime(
        _layout(),
        surface_factory=surface_factory,
        layout_factory=layout_factory,
    )
    with pytest.raises(WindowsViewportRuntimeError) as exc_info:
        asyncio.run(runtime.open())

    assert exc_info.value.code == WindowsViewportRuntimeErrorCode.LAYOUT_OPEN_FAILURE
    assert "secret" not in str(exc_info.value).casefold()
    assert runtime.snapshot.state == WindowsViewportRuntimeState.FAILED
    assert [surface.closed for surface in surfaces] == [1, 1]
    assert layouts[0].closed == 1


def test_unknown_slot_does_not_fail_runtime_but_presentation_failure_does() -> None:
    surfaces: list[FakeSurface] = []
    layouts: list[FakeLayout] = []

    def surface_factory() -> FakeSurface:
        surface = FakeSurface()
        surfaces.append(surface)
        return surface

    def layout_factory(layout: ViewportLayout) -> FakeLayout:
        target = FakeLayout(layout)
        layouts.append(target)
        return target

    runtime = BoundedWindowsViewportRuntime(
        _layout(),
        surface_factory=surface_factory,
        layout_factory=layout_factory,
    )

    async def scenario() -> None:
        await runtime.open()
        with pytest.raises(WindowsViewportRuntimeError) as unknown_exc:
            await runtime.present(1234, _frame())
        assert unknown_exc.value.code == WindowsViewportRuntimeErrorCode.UNKNOWN_SLOT
        assert runtime.snapshot.state == WindowsViewportRuntimeState.OPEN

        layouts[0].fail_present = True
        with pytest.raises(WindowsViewportRuntimeError) as failure_exc:
            await runtime.present(7, _frame())
        assert failure_exc.value.code == WindowsViewportRuntimeErrorCode.PRESENTATION_FAILURE
        assert "secret" not in str(failure_exc.value).casefold()

    asyncio.run(scenario())
    assert runtime.snapshot.state == WindowsViewportRuntimeState.FAILED
    assert runtime.snapshot.open_surface_count == 0
    assert [surface.closed for surface in surfaces] == [1, 1]
    assert layouts[0].closed == 1


def test_cleanup_failure_attempts_all_resources_and_is_sanitized() -> None:
    surfaces: list[FakeSurface] = []
    layouts: list[FakeLayout] = []

    def surface_factory() -> FakeSurface:
        surface = FakeSurface()
        surfaces.append(surface)
        return surface

    def layout_factory(layout: ViewportLayout) -> FakeLayout:
        target = FakeLayout(layout)
        layouts.append(target)
        return target

    runtime = BoundedWindowsViewportRuntime(
        _layout(),
        surface_factory=surface_factory,
        layout_factory=layout_factory,
    )

    async def scenario() -> None:
        await runtime.open()
        surfaces[0].fail_close = True
        layouts[0].fail_close = True
        with pytest.raises(WindowsViewportRuntimeError) as exc_info:
            await runtime.close()
        assert exc_info.value.code == WindowsViewportRuntimeErrorCode.CLEANUP_FAILURE
        assert "secret" not in str(exc_info.value).casefold()

    asyncio.run(scenario())
    assert runtime.snapshot.state == WindowsViewportRuntimeState.FAILED
    assert runtime.snapshot.open_surface_count == 0
    assert [surface.closed for surface in surfaces] == [1, 1]
    assert layouts[0].closed == 1


def test_lifecycle_bindings_and_presentation_bounds() -> None:
    surfaces: list[FakeSurface] = []
    layouts: list[FakeLayout] = []

    def surface_factory() -> FakeSurface:
        surface = FakeSurface()
        surfaces.append(surface)
        return surface

    def layout_factory(layout: ViewportLayout) -> FakeLayout:
        target = FakeLayout(layout)
        layouts.append(target)
        return target

    runtime = BoundedWindowsViewportRuntime(
        _layout(),
        surface_factory=surface_factory,
        layout_factory=layout_factory,
        max_presentations=1,
    )

    with pytest.raises(WindowsViewportRuntimeError):
        _ = runtime.bindings

    async def scenario() -> None:
        first = await runtime.open()
        second = await runtime.open()
        assert first == second
        assert [binding.slot for binding in runtime.bindings] == [7, 4095]
        await runtime.present(7, _frame())
        with pytest.raises(WindowsViewportRuntimeError) as exc_info:
            await runtime.present(7, _frame())
        assert exc_info.value.code == WindowsViewportRuntimeErrorCode.PRESENTATION_LIMIT

    asyncio.run(scenario())
    assert runtime.snapshot.state == WindowsViewportRuntimeState.FAILED
    assert [surface.closed for surface in surfaces] == [1, 1]
    assert layouts[0].closed == 1
    with pytest.raises(WindowsViewportRuntimeError):
        _ = runtime.bindings

    for limit in (0, 1_000_001):
        with pytest.raises(ValueError):
            BoundedWindowsViewportRuntime(_layout(), max_presentations=limit)

    with pytest.raises(WindowsViewportRuntimeError) as invalid_exc:
        BoundedWindowsViewportRuntime(object())  # type: ignore[arg-type]
    assert invalid_exc.value.code == WindowsViewportRuntimeErrorCode.INVALID_CONFIGURATION
