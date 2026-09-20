from __future__ import annotations

import asyncio
from collections import deque

import pytest

from k5vision.media.viewport_geometry import ViewportGeometry, ViewportLayout, ViewportPlacement
from k5vision.media.windows_operator_application import (
    WindowsOperatorApplicationSnapshot,
    WindowsOperatorApplicationState,
    _NativeShellError,
    _NativeShellFailure,
)
from k5vision.media.windows_operator_control import BoundedWindowsOperatorControl
from k5vision.media.windows_operator_interaction import (
    WindowsPointerEvent,
    WindowsPointerEventKind,
    _InteractiveWin32OperatorShellApi,
)


def _layout() -> ViewportLayout:
    return ViewportLayout(
        placements=(
            ViewportPlacement(
                logical_slot=7,
                geometry=ViewportGeometry(x=10, y=10, width=200, height=200, z_index=1),
            ),
            ViewportPlacement(
                logical_slot=4095,
                geometry=ViewportGeometry(x=50, y=50, width=100, height=100, z_index=3),
            ),
        )
    )


def _event(kind: WindowsPointerEventKind, x: int = 0, y: int = 0) -> WindowsPointerEvent:
    return WindowsPointerEvent(kind=kind, x=x, y=y)


def _native_capture_api(*, captured: int = 71, released: bool = True) -> object:
    native = object.__new__(_InteractiveWin32OperatorShellApi)
    native._pointer_events = deque()
    native._capture_active = False
    native._set_capture = lambda _shell: 0
    native._get_capture = lambda: captured
    native._release_capture = lambda: int(released)
    return native


def test_native_capture_acquires_verifies_and_releases() -> None:
    native = _native_capture_api()

    native._acquire_pointer_capture(71)
    assert native._capture_active is True

    native._release_pointer_capture()
    assert native._capture_active is False
    native._release_pointer_capture()


def test_native_capture_verification_and_release_fail_closed() -> None:
    wrong_owner = _native_capture_api(captured=72)
    with pytest.raises(_NativeShellError) as acquire_error:
        wrong_owner._acquire_pointer_capture(71)
    assert acquire_error.value.failure == _NativeShellFailure.PUMP
    assert wrong_owner._capture_active is False

    failed_release = _native_capture_api(released=False)
    failed_release._capture_active = True
    with pytest.raises(_NativeShellError) as release_error:
        failed_release._release_pointer_capture()
    assert release_error.value.failure == _NativeShellFailure.PUMP
    assert failed_release._capture_active is False


def test_cancel_and_capture_loss_emit_ephemeral_cancel_events() -> None:
    native = _native_capture_api()
    native._capture_active = True
    native._cancel_pointer_capture(71)
    assert native._capture_active is False
    assert native.drain_pointer_events(1) == (_event(WindowsPointerEventKind.CANCEL),)

    native._capture_active = True
    native._capture_was_lost(71)
    assert native._capture_active is False
    assert native.drain_pointer_events(1) == (_event(WindowsPointerEventKind.CANCEL),)


class _CaptureFakeApplication:
    def __init__(self, batches: tuple[tuple[WindowsPointerEvent, ...], ...]) -> None:
        self.state = WindowsOperatorApplicationState.READY
        self.generation = 0
        self.pumps = 0
        self.batches = deque(batches)
        self.relayouts: list[ViewportLayout] = []
        self.wait_entered = asyncio.Event()
        self.wait_release = asyncio.Event()

    @property
    def snapshot(self) -> WindowsOperatorApplicationSnapshot:
        return WindowsOperatorApplicationSnapshot(
            state=self.state,
            shell_open=self.state != WindowsOperatorApplicationState.CLOSED,
            pump_cycles=self.pumps,
            pumped_messages=self.pumps,
            generation=self.generation,
            viewport_count=2 if self.generation else 0,
            open_surface_count=(2 if self.state == WindowsOperatorApplicationState.RUNNING else 0),
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
        self, _layout: ViewportLayout, _streams: object
    ) -> WindowsOperatorApplicationSnapshot:
        self.generation += 1
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
        assert 1 <= max_messages <= 256
        self.pumps += 1
        if self.pumps >= 3 and not self.batches and not self.relayouts:
            self.state = WindowsOperatorApplicationState.CLOSED
        await asyncio.sleep(0)
        return self.snapshot

    def drain_pointer_events(self, *, max_events: int = 64) -> tuple[WindowsPointerEvent, ...]:
        if not self.batches:
            return ()
        batch = self.batches.popleft()
        assert len(batch) <= max_events
        return batch

    async def close(self) -> WindowsOperatorApplicationSnapshot:
        self.state = WindowsOperatorApplicationState.CLOSED
        self.wait_release.set()
        return self.snapshot


def test_capture_loss_cancels_drag_without_publishing_geometry() -> None:
    app = _CaptureFakeApplication(
        (
            (
                _event(WindowsPointerEventKind.DOWN, 60, 60),
                _event(WindowsPointerEventKind.CANCEL),
            ),
        )
    )
    control = BoundedWindowsOperatorControl(
        application_factory=lambda: app,
        poll_interval_seconds=0,
        max_cycles=20,
    )
    original = _layout()

    result = asyncio.run(control.run(width=900, height=700, layout=original, streams=()))

    assert app.relayouts == []
    assert result.active_layout == original
    assert result.interaction_edits == 0
    assert result.cancelled_interactions == 1
    assert result.session.generation == 1
    serialized = result.model_dump_json().casefold()
    assert "pointer" not in serialized


def test_captured_drag_can_finish_outside_origin_without_generation_change() -> None:
    app = _CaptureFakeApplication(
        (
            (
                _event(WindowsPointerEventKind.DOWN, 60, 60),
                _event(WindowsPointerEventKind.UP, 200, 200),
            ),
        )
    )
    control = BoundedWindowsOperatorControl(
        application_factory=lambda: app,
        poll_interval_seconds=0,
        max_cycles=200,
    )

    async def scenario() -> object:
        task = asyncio.create_task(control.run(width=900, height=700, layout=_layout(), streams=()))
        await app.wait_entered.wait()
        for _ in range(40):
            await asyncio.sleep(0)
            if app.relayouts:
                break
        assert len(app.relayouts) == 1
        moved = app.relayouts[0].by_slot()[4095]
        assert (moved.x, moved.y, moved.width, moved.height, moved.z_index) == (
            190,
            190,
            100,
            100,
            3,
        )
        control.request_stop()
        return await task

    result = asyncio.run(scenario())
    assert result.interaction_edits == 1
    assert result.cancelled_interactions == 0
    assert result.viewport_edits == 1
    assert result.session.generation == 1
