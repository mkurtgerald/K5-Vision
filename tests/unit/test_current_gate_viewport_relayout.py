from __future__ import annotations

import asyncio

import pytest

from k5vision.media.viewport_geometry import ViewportGeometry, ViewportLayout, ViewportPlacement
from k5vision.media.windows_viewport_layout import (
    BoundedWindowsViewportLayout,
    WindowsViewportLayoutError,
    WindowsViewportLayoutErrorCode,
    WindowsViewportLayoutState,
)
from k5vision.media.windows_viewport_runtime import (
    BoundedWindowsViewportRuntime,
    WindowsViewportRuntimeError,
    WindowsViewportRuntimeErrorCode,
    WindowsViewportRuntimeState,
)


def _layout(*, offset: int = 0, slots: tuple[int, int] = (7, 4095)) -> ViewportLayout:
    return ViewportLayout(
        placements=(
            ViewportPlacement(
                logical_slot=slots[0],
                geometry=ViewportGeometry(
                    x=17 + offset,
                    y=29 + offset,
                    width=613 - offset,
                    height=347 + offset,
                    z_index=2,
                ),
            ),
            ViewportPlacement(
                logical_slot=slots[1],
                geometry=ViewportGeometry(
                    x=701 - offset,
                    y=41 + offset,
                    width=211 + offset,
                    height=719 - offset,
                    z_index=1,
                ),
            ),
        )
    )


class FakeTarget:
    def __init__(self, *, fail_open: bool = False) -> None:
        self.fail_open = fail_open
        self.opened: list[tuple[int, int, int, int]] = []
        self.presented: list[object] = []
        self.closed = 0

    async def open(
        self,
        width: int,
        height: int,
        *,
        x: int = 0,
        y: int = 0,
    ) -> object:
        if self.fail_open:
            raise RuntimeError("SECRET replacement target open detail")
        self.opened.append((x, y, width, height))
        return object()

    async def present(self, surface: object) -> object:
        self.presented.append(surface)
        return object()

    async def close(self) -> object:
        self.closed += 1
        return object()


class FakeSurface:
    def __init__(self) -> None:
        self.opened = 0
        self.closed = 0

    async def open(self) -> object:
        self.opened += 1
        return object()

    async def present(self, frame: object) -> None:
        return None

    async def close(self) -> object:
        self.closed += 1
        return object()


class FakeRelayoutBoundary:
    def __init__(self, layout: ViewportLayout, *, fail_relayout: bool = False) -> None:
        self.layout = layout
        self.fail_relayout = fail_relayout
        self.opened = 0
        self.closed = 0
        self.relayouts: list[ViewportLayout] = []

    async def open(self) -> object:
        self.opened += 1
        return object()

    async def relayout(self, layout: ViewportLayout) -> object:
        if self.fail_relayout:
            raise RuntimeError("SECRET relayout detail")
        self.relayouts.append(layout)
        self.layout = layout
        return object()

    async def present(self, logical_slot: int, surface: object) -> object:
        return object()

    async def close(self) -> object:
        self.closed += 1
        return object()


def test_layout_relayout_replaces_only_targets_and_preserves_sparse_slot_routing() -> None:
    targets: list[FakeTarget] = []

    def factory() -> FakeTarget:
        target = FakeTarget()
        targets.append(target)
        return target

    coordinator = BoundedWindowsViewportLayout(_layout(), target_factory=factory)
    replacement = _layout(offset=37)
    surface = object()

    async def scenario() -> None:
        await coordinator.open()
        assert len(targets) == 2
        await coordinator.relayout(replacement)
        assert coordinator.snapshot.state == WindowsViewportLayoutState.OPEN
        assert coordinator.snapshot.target_count == 2
        assert coordinator.snapshot.open_target_count == 2
        await coordinator.present(4095, surface)

    asyncio.run(scenario())

    assert len(targets) == 4
    assert [target.closed for target in targets[:2]] == [1, 1]
    assert targets[2].opened == [(54, 66, 576, 384)]
    assert targets[3].opened == [(664, 78, 248, 682)]
    assert targets[2].presented == []
    assert targets[3].presented == [surface]

    retained = coordinator.snapshot.model_dump_json().casefold()
    for forbidden in (
        "rtsp://",
        "credential",
        "password",
        "source_id",
        "recording_id",
        "path",
        "payload",
        "handle",
        "pointer",
    ):
        assert forbidden not in retained


def test_layout_relayout_rejects_slot_set_change_without_touching_active_targets() -> None:
    targets: list[FakeTarget] = []

    def factory() -> FakeTarget:
        target = FakeTarget()
        targets.append(target)
        return target

    coordinator = BoundedWindowsViewportLayout(_layout(), target_factory=factory)

    async def scenario() -> None:
        await coordinator.open()
        with pytest.raises(WindowsViewportLayoutError) as exc_info:
            await coordinator.relayout(_layout(offset=5, slots=(7, 4000)))
        assert exc_info.value.code == WindowsViewportLayoutErrorCode.INVALID_CONFIGURATION
        assert coordinator.snapshot.state == WindowsViewportLayoutState.OPEN

    asyncio.run(scenario())
    assert len(targets) == 2
    assert [target.closed for target in targets] == [0, 0]


def test_layout_relayout_partial_open_failure_fails_closed_and_sanitizes() -> None:
    targets: list[FakeTarget] = []

    def factory() -> FakeTarget:
        target = FakeTarget(fail_open=len(targets) == 3)
        targets.append(target)
        return target

    coordinator = BoundedWindowsViewportLayout(_layout(), target_factory=factory)

    async def scenario() -> None:
        await coordinator.open()
        with pytest.raises(WindowsViewportLayoutError) as exc_info:
            await coordinator.relayout(_layout(offset=11))
        assert exc_info.value.code == WindowsViewportLayoutErrorCode.TARGET_OPEN_FAILURE
        assert "secret" not in str(exc_info.value).casefold()

    asyncio.run(scenario())
    assert coordinator.snapshot.state == WindowsViewportLayoutState.FAILED
    assert coordinator.snapshot.open_target_count == 0
    assert len(targets) == 4
    assert [target.closed for target in targets] == [1, 1, 1, 1]


def test_runtime_relayout_preserves_surfaces_and_binding_slots() -> None:
    surfaces: list[FakeSurface] = []
    layouts: list[FakeRelayoutBoundary] = []

    def surface_factory() -> FakeSurface:
        surface = FakeSurface()
        surfaces.append(surface)
        return surface

    def layout_factory(layout: ViewportLayout) -> FakeRelayoutBoundary:
        boundary = FakeRelayoutBoundary(layout)
        layouts.append(boundary)
        return boundary

    runtime = BoundedWindowsViewportRuntime(
        _layout(),
        surface_factory=surface_factory,
        layout_factory=layout_factory,
    )
    replacement = _layout(offset=37)

    async def scenario() -> None:
        await runtime.open()
        before_slots = tuple(binding.slot for binding in runtime.bindings)
        snapshot = await runtime.relayout(replacement)
        after_slots = tuple(binding.slot for binding in runtime.bindings)
        assert before_slots == (7, 4095)
        assert after_slots == before_slots
        assert snapshot.state == WindowsViewportRuntimeState.OPEN
        assert snapshot.open_surface_count == 2
        assert snapshot.presentations == 0

    asyncio.run(scenario())
    assert len(surfaces) == 2
    assert [surface.opened for surface in surfaces] == [1, 1]
    assert [surface.closed for surface in surfaces] == [0, 0]
    assert layouts[0].relayouts == [replacement]


def test_runtime_relayout_rejects_slot_change_without_failing_active_runtime() -> None:
    surfaces: list[FakeSurface] = []
    layouts: list[FakeRelayoutBoundary] = []

    def surface_factory() -> FakeSurface:
        surface = FakeSurface()
        surfaces.append(surface)
        return surface

    def layout_factory(layout: ViewportLayout) -> FakeRelayoutBoundary:
        boundary = FakeRelayoutBoundary(layout)
        layouts.append(boundary)
        return boundary

    runtime = BoundedWindowsViewportRuntime(
        _layout(),
        surface_factory=surface_factory,
        layout_factory=layout_factory,
    )

    async def scenario() -> None:
        await runtime.open()
        with pytest.raises(WindowsViewportRuntimeError) as exc_info:
            await runtime.relayout(_layout(offset=9, slots=(7, 4000)))
        assert exc_info.value.code == WindowsViewportRuntimeErrorCode.INVALID_CONFIGURATION
        assert runtime.snapshot.state == WindowsViewportRuntimeState.OPEN

    asyncio.run(scenario())
    assert layouts[0].relayouts == []
    assert [surface.closed for surface in surfaces] == [0, 0]


def test_runtime_relayout_failure_closes_surfaces_and_sanitizes() -> None:
    surfaces: list[FakeSurface] = []
    boundary: FakeRelayoutBoundary | None = None

    def surface_factory() -> FakeSurface:
        surface = FakeSurface()
        surfaces.append(surface)
        return surface

    def layout_factory(layout: ViewportLayout) -> FakeRelayoutBoundary:
        nonlocal boundary
        boundary = FakeRelayoutBoundary(layout, fail_relayout=True)
        return boundary

    runtime = BoundedWindowsViewportRuntime(
        _layout(),
        surface_factory=surface_factory,
        layout_factory=layout_factory,
    )

    async def scenario() -> None:
        await runtime.open()
        with pytest.raises(WindowsViewportRuntimeError) as exc_info:
            await runtime.relayout(_layout(offset=13))
        assert exc_info.value.code == WindowsViewportRuntimeErrorCode.RELAYOUT_FAILURE
        assert "secret" not in str(exc_info.value).casefold()

    asyncio.run(scenario())
    assert runtime.snapshot.state == WindowsViewportRuntimeState.FAILED
    assert runtime.snapshot.open_surface_count == 0
    assert [surface.closed for surface in surfaces] == [1, 1]
    assert boundary is not None
    assert boundary.closed == 1
