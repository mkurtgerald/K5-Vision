from __future__ import annotations

import asyncio

import pytest

from k5vision.media.viewport_editor import ViewportMove
from k5vision.media.viewport_geometry import ViewportGeometry, ViewportLayout, ViewportPlacement
from k5vision.media.windows_operator_application import (
    WindowsOperatorApplicationSnapshot,
    WindowsOperatorApplicationState,
)
from k5vision.media.windows_operator_control import (
    WindowsOperatorControlError,
    WindowsOperatorControlErrorCode,
)
from k5vision.media.windows_operator_history_control import BoundedHistoryWindowsOperatorControl
from k5vision.media.windows_operator_selection import BoundedSelectableWindowsOperatorControl


def _layout(offset: int = 0) -> ViewportLayout:
    return ViewportLayout(
        placements=(
            ViewportPlacement(
                logical_slot=7,
                geometry=ViewportGeometry(
                    x=17 + offset,
                    y=29,
                    width=613,
                    height=347,
                    z_index=2,
                ),
            ),
        )
    )


class _Application:
    def __init__(self) -> None:
        self.state = WindowsOperatorApplicationState.READY
        self.generation = 0
        self.relayouts: list[ViewportLayout] = []
        self.replacements: list[ViewportLayout] = []
        self.wait_entered = asyncio.Event()
        self.wait_release = asyncio.Event()

    @property
    def snapshot(self) -> WindowsOperatorApplicationSnapshot:
        return WindowsOperatorApplicationSnapshot(
            state=self.state,
            shell_open=self.state != WindowsOperatorApplicationState.CLOSED,
            pump_cycles=0,
            pumped_messages=0,
            generation=self.generation,
            viewport_count=1 if self.generation else 0,
            open_surface_count=1 if self.state == WindowsOperatorApplicationState.RUNNING else 0,
            delivered_frames=0,
            presentations=0,
        )

    async def open(self, _width: int, _height: int) -> WindowsOperatorApplicationSnapshot:
        self.state = WindowsOperatorApplicationState.OPEN
        return self.snapshot

    async def start(
        self, _layout: ViewportLayout, _streams: object
    ) -> WindowsOperatorApplicationSnapshot:
        self.generation = 1
        self.state = WindowsOperatorApplicationState.RUNNING
        return self.snapshot

    async def replace(
        self,
        layout: ViewportLayout,
        _streams: object,
    ) -> WindowsOperatorApplicationSnapshot:
        self.replacements.append(layout)
        self.generation += 1
        self.state = WindowsOperatorApplicationState.RUNNING
        return self.snapshot

    async def relayout(self, layout: ViewportLayout) -> WindowsOperatorApplicationSnapshot:
        self.relayouts.append(layout)
        return self.snapshot

    async def wait(self) -> WindowsOperatorApplicationSnapshot:
        self.wait_entered.set()
        await self.wait_release.wait()
        return self.snapshot

    async def stop(self) -> WindowsOperatorApplicationSnapshot:
        self.state = WindowsOperatorApplicationState.STOPPED
        self.wait_release.set()
        return self.snapshot

    async def pump(self, *, max_messages: int = 64) -> WindowsOperatorApplicationSnapshot:
        assert max_messages > 0
        await asyncio.sleep(0)
        return self.snapshot

    async def close(self) -> WindowsOperatorApplicationSnapshot:
        self.state = WindowsOperatorApplicationState.CLOSED
        self.wait_release.set()
        return self.snapshot


async def _yield_until(predicate: object, *, cycles: int = 100) -> None:
    for _ in range(cycles):
        if callable(predicate) and predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition was not reached")


def test_live_operator_edit_undo_redo_and_explicit_relayout_rebase() -> None:
    async def scenario() -> None:
        app = _Application()
        control = BoundedHistoryWindowsOperatorControl(
            application_factory=lambda: app,
            poll_interval_seconds=0,
            max_cycles=1000,
        )
        original = _layout()
        task = asyncio.create_task(control.run(width=1280, height=720, layout=original, streams=()))
        await app.wait_entered.wait()

        control.request_edit(ViewportMove(logical_slot=7, dx=23, dy=0))
        await _yield_until(lambda: control.history_control_snapshot.undo_depth == 1)
        edited = control.control_snapshot.active_layout
        assert edited is not None and edited != original
        assert control.history_control_snapshot.history_operations == 1

        control.request_undo()
        await _yield_until(lambda: control.history_control_snapshot.undo_requests == 1)
        assert control.control_snapshot.active_layout == original
        assert control.history_control_snapshot.redo_depth == 1

        control.request_redo()
        await _yield_until(lambda: control.history_control_snapshot.redo_requests == 1)
        assert control.control_snapshot.active_layout == edited
        assert control.history_control_snapshot.undo_depth == 1
        assert control.history_control_snapshot.history_operations == 3

        explicit = _layout(offset=41)
        control.request_relayout(explicit)
        await _yield_until(lambda: control.history_control_snapshot.history_rebases == 1)
        assert control.control_snapshot.active_layout == explicit
        assert control.history_control_snapshot.undo_depth == 0
        assert control.history_control_snapshot.redo_depth == 0
        assert control.history_control_snapshot.history_operations == 3
        assert app.relayouts == [edited, original, edited, explicit]

        control.request_stop()
        await task

    asyncio.run(scenario())


def test_selectable_product_path_inherits_live_history_control() -> None:
    control = BoundedSelectableWindowsOperatorControl(
        application_factory=lambda: _Application(),
    )
    assert isinstance(control, BoundedHistoryWindowsOperatorControl)
    assert callable(control.request_undo)
    assert callable(control.request_redo)


def test_history_control_observability_is_source_free() -> None:
    snapshot = BoundedHistoryWindowsOperatorControl(
        application_factory=lambda: _Application(),
    ).history_control_snapshot
    payload = snapshot.model_dump_json().casefold()
    for forbidden in (
        "rtsp://",
        "credential",
        "password",
        "source_id",
        "recording_id",
        "source_uri",
        "payload",
        "handle",
        "pointer",
    ):
        assert forbidden not in payload


@pytest.mark.parametrize(
    "kwargs",
    (
        {"max_viewport_history": 0},
        {"max_viewport_history": 1025},
        {"max_viewport_history_operations": 0},
        {"max_viewport_history_operations": 1_000_001},
    ),
)
def test_history_control_bounds_are_explicit(kwargs: dict[str, int]) -> None:
    with pytest.raises(WindowsOperatorControlError) as caught:
        BoundedHistoryWindowsOperatorControl(
            application_factory=lambda: _Application(),
            **kwargs,
        )
    assert caught.value.code == WindowsOperatorControlErrorCode.INVALID_CONFIGURATION
