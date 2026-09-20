from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from k5vision.media.viewport_geometry import ViewportGeometry, ViewportLayout, ViewportPlacement
from k5vision.media.windows_viewport_layout import (
    BoundedWindowsViewportLayout,
    WindowsViewportLayoutError,
    WindowsViewportLayoutErrorCode,
    WindowsViewportLayoutState,
)


class FakeTarget:
    def __init__(self) -> None:
        self.opened: list[tuple[int, int, int, int]] = []
        self.presented: list[object] = []
        self.closed = 0
        self.fail_open = False
        self.fail_present = False
        self.fail_close = False

    async def open(
        self,
        width: int,
        height: int,
        *,
        x: int = 0,
        y: int = 0,
    ) -> object:
        if self.fail_open:
            raise RuntimeError("SECRET open detail")
        self.opened.append((x, y, width, height))
        return object()

    async def present(self, surface: object) -> object:
        if self.fail_present:
            raise RuntimeError("SECRET present detail")
        self.presented.append(surface)
        return object()

    async def close(self) -> object:
        self.closed += 1
        if self.fail_close:
            raise RuntimeError("SECRET cleanup detail")
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


def test_open_route_close_preserves_sparse_arbitrary_geometry_without_identity_snapshot() -> None:
    targets: list[FakeTarget] = []

    def factory() -> FakeTarget:
        target = FakeTarget()
        targets.append(target)
        return target

    coordinator = BoundedWindowsViewportLayout(_layout(), target_factory=factory)
    surface = object()

    async def scenario() -> object:
        opened = await coordinator.open()
        assert opened.state == WindowsViewportLayoutState.OPEN
        assert opened.target_count == 2
        assert opened.open_target_count == 2
        await coordinator.present(4095, surface)
        return await coordinator.close()

    closed = asyncio.run(scenario())
    assert targets[0].opened == [(17, 29, 613, 347)]
    assert targets[1].opened == [(701, 41, 211, 719)]
    assert targets[0].presented == []
    assert targets[1].presented == [surface]
    assert [target.closed for target in targets] == [1, 1]
    assert closed.state == WindowsViewportLayoutState.CLOSED
    assert closed.open_target_count == 0
    assert closed.presentations == 1

    serialized = closed.model_dump_json().casefold()
    for forbidden in ("4095", "logical_slot", "rtsp://", "source_id", "recording_id", "handle", "pointer"):
        assert forbidden not in serialized


def test_partial_open_failure_closes_prior_targets_and_is_sanitized() -> None:
    targets: list[FakeTarget] = []

    def factory() -> FakeTarget:
        target = FakeTarget()
        if targets:
            target.fail_open = True
        targets.append(target)
        return target

    coordinator = BoundedWindowsViewportLayout(_layout(), target_factory=factory)
    with pytest.raises(WindowsViewportLayoutError) as exc_info:
        asyncio.run(coordinator.open())

    assert exc_info.value.code == WindowsViewportLayoutErrorCode.TARGET_OPEN_FAILURE
    assert "secret" not in str(exc_info.value).casefold()
    assert coordinator.snapshot.state == WindowsViewportLayoutState.FAILED
    assert coordinator.snapshot.open_target_count == 0
    assert targets[0].closed == 1


def test_presentation_failure_fails_closed_and_unknown_slot_does_not() -> None:
    targets: list[FakeTarget] = []

    def factory() -> FakeTarget:
        target = FakeTarget()
        targets.append(target)
        return target

    coordinator = BoundedWindowsViewportLayout(_layout(), target_factory=factory)

    async def scenario() -> None:
        await coordinator.open()
        with pytest.raises(WindowsViewportLayoutError) as unknown_exc:
            await coordinator.present(1234, object())
        assert unknown_exc.value.code == WindowsViewportLayoutErrorCode.UNKNOWN_SLOT
        assert coordinator.snapshot.state == WindowsViewportLayoutState.OPEN

        targets[0].fail_present = True
        with pytest.raises(WindowsViewportLayoutError) as failure_exc:
            await coordinator.present(7, object())
        assert failure_exc.value.code == WindowsViewportLayoutErrorCode.PRESENTATION_FAILURE
        assert "secret" not in str(failure_exc.value).casefold()

    asyncio.run(scenario())
    assert coordinator.snapshot.state == WindowsViewportLayoutState.FAILED
    assert coordinator.snapshot.open_target_count == 0
    assert [target.closed for target in targets] == [1, 1]


def test_cleanup_failure_attempts_all_targets_and_is_sanitized() -> None:
    targets: list[FakeTarget] = []

    def factory() -> FakeTarget:
        target = FakeTarget()
        targets.append(target)
        return target

    coordinator = BoundedWindowsViewportLayout(_layout(), target_factory=factory)

    async def scenario() -> None:
        await coordinator.open()
        targets[0].fail_close = True
        with pytest.raises(WindowsViewportLayoutError) as exc_info:
            await coordinator.close()
        assert exc_info.value.code == WindowsViewportLayoutErrorCode.CLEANUP_FAILURE
        assert "secret" not in str(exc_info.value).casefold()

    asyncio.run(scenario())
    assert [target.closed for target in targets] == [1, 1]
    assert coordinator.snapshot.state == WindowsViewportLayoutState.FAILED
    assert coordinator.snapshot.open_target_count == 0


def test_open_is_idempotent_and_closed_layout_cannot_reopen() -> None:
    targets: list[FakeTarget] = []

    def factory() -> FakeTarget:
        target = FakeTarget()
        targets.append(target)
        return target

    coordinator = BoundedWindowsViewportLayout(_layout(), target_factory=factory)

    async def scenario() -> None:
        first = await coordinator.open()
        second = await coordinator.open()
        assert first == second
        assert len(targets) == 2
        assert (await coordinator.close()).state == WindowsViewportLayoutState.CLOSED
        assert (await coordinator.close()).state == WindowsViewportLayoutState.CLOSED
        with pytest.raises(WindowsViewportLayoutError) as exc_info:
            await coordinator.open()
        assert exc_info.value.code == WindowsViewportLayoutErrorCode.INVALID_STATE

    asyncio.run(scenario())


def test_configuration_and_presentation_bounds() -> None:
    with pytest.raises(WindowsViewportLayoutError) as exc_info:
        BoundedWindowsViewportLayout(object())  # type: ignore[arg-type]
    assert exc_info.value.code == WindowsViewportLayoutErrorCode.INVALID_CONFIGURATION

    for limit in (0, 1_000_001):
        with pytest.raises(ValueError):
            BoundedWindowsViewportLayout(_layout(), max_presentations=limit)

    target = FakeTarget()
    coordinator = BoundedWindowsViewportLayout(
        ViewportLayout(
            placements=(
                ViewportPlacement(
                    logical_slot=1,
                    geometry=ViewportGeometry(x=1, y=2, width=3, height=4),
                ),
            )
        ),
        target_factory=typing_cast_factory(lambda: target),
        max_presentations=1,
    )

    async def scenario() -> None:
        await coordinator.open()
        await coordinator.present(1, object())
        with pytest.raises(WindowsViewportLayoutError) as exc_info:
            await coordinator.present(1, object())
        assert exc_info.value.code == WindowsViewportLayoutErrorCode.PRESENTATION_FAILURE

    asyncio.run(scenario())
    assert coordinator.snapshot.state == WindowsViewportLayoutState.FAILED
    assert target.closed == 1


def typing_cast_factory(factory: Callable[[], FakeTarget]) -> Callable[[], FakeTarget]:
    return factory
