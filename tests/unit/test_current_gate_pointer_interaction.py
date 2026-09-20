from __future__ import annotations

import asyncio
from collections import deque

import pytest

from k5vision.media.viewport_geometry import ViewportGeometry, ViewportLayout, ViewportPlacement
from k5vision.media.windows_operator_application import (
    WindowsOperatorApplicationError,
    WindowsOperatorApplicationErrorCode,
    WindowsOperatorApplicationSnapshot,
    WindowsOperatorApplicationState,
    _NativeShellError,
    _NativeShellFailure,
)
from k5vision.media.windows_operator_control import (
    BoundedWindowsOperatorControl,
    WindowsOperatorControlError,
    WindowsOperatorControlErrorCode,
)
from k5vision.media.windows_operator_interaction import (
    BoundedInteractiveWindowsOperatorApplication,
    WindowsPointerEvent,
    WindowsPointerEventKind,
    _InteractiveWin32OperatorShellApi,
    _signed_word,
)


def _layout() -> ViewportLayout:
    return ViewportLayout(
        placements=(
            ViewportPlacement(
                logical_slot=7,
                geometry=ViewportGeometry(
                    x=10,
                    y=10,
                    width=200,
                    height=200,
                    z_index=1,
                ),
            ),
            ViewportPlacement(
                logical_slot=4095,
                geometry=ViewportGeometry(
                    x=50,
                    y=50,
                    width=100,
                    height=100,
                    z_index=3,
                ),
            ),
        )
    )


def _event(kind: WindowsPointerEventKind, x: int, y: int) -> WindowsPointerEvent:
    return WindowsPointerEvent(kind=kind, x=x, y=y)


class _InteractiveFakeApplication:
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


def test_drag_hit_tests_topmost_overlap_and_moves_without_generation_change() -> None:
    app = _InteractiveFakeApplication(
        (
            (
                _event(WindowsPointerEventKind.DOWN, 60, 60),
                _event(WindowsPointerEventKind.MOVE, 70, 75),
                _event(WindowsPointerEventKind.UP, 80, 90),
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
        untouched = app.relayouts[0].by_slot()[7]
        assert (moved.x, moved.y, moved.width, moved.height, moved.z_index) == (
            70,
            80,
            100,
            100,
            3,
        )
        assert untouched == _layout().by_slot()[7]
        assert app.generation == 1
        control.request_stop()
        return await task

    result = asyncio.run(scenario())
    assert result.interaction_edits == 1
    assert result.viewport_edits == 1
    assert result.relayouts == 1
    assert result.replacements == 0
    assert result.session.generation == 1


def test_bottom_right_drag_resizes_without_changing_position_or_z_order() -> None:
    app = _InteractiveFakeApplication(
        (
            (
                _event(WindowsPointerEventKind.DOWN, 205, 205),
                _event(WindowsPointerEventKind.UP, 225, 220),
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
        changed = app.relayouts[0].by_slot()[7]
        assert (changed.x, changed.y, changed.width, changed.height, changed.z_index) == (
            10,
            10,
            220,
            215,
            1,
        )
        control.request_stop()
        return await task

    result = asyncio.run(scenario())
    assert result.interaction_edits == 1
    assert result.viewport_edits == 1
    assert result.session.generation == 1


def test_pointer_batch_cannot_overrun_control_queue() -> None:
    app = _InteractiveFakeApplication(
        (
            (
                _event(WindowsPointerEventKind.DOWN, 20, 20),
                _event(WindowsPointerEventKind.UP, 25, 25),
                _event(WindowsPointerEventKind.DOWN, 30, 30),
                _event(WindowsPointerEventKind.UP, 35, 35),
            ),
        )
    )
    control = BoundedWindowsOperatorControl(
        application_factory=lambda: app,
        poll_interval_seconds=0,
        max_cycles=200,
        max_pending_controls=1,
    )
    original = _layout()

    async def scenario() -> None:
        task = asyncio.create_task(control.run(width=900, height=700, layout=original, streams=()))
        with pytest.raises(WindowsOperatorControlError) as exc_info:
            await task
        assert exc_info.value.code == WindowsOperatorControlErrorCode.CONTROL_LIMIT

    asyncio.run(scenario())
    assert app.relayouts == []
    assert control.control_snapshot.active_layout == original
    assert control.control_snapshot.interaction_edits == 0
    assert control.control_snapshot.viewport_edits == 0


def test_pointer_events_are_ephemeral_not_retained_in_control_snapshot() -> None:
    app = _InteractiveFakeApplication(())
    control = BoundedWindowsOperatorControl(application_factory=lambda: app)
    serialized = control.control_snapshot.model_dump_json().casefold()

    assert "pointer" not in serialized
    for forbidden in (
        "rtsp://",
        "credential",
        "password",
        "source_id",
        "recording_id",
        "payload",
        "handle",
    ):
        assert forbidden not in serialized


def test_signed_pointer_words_preserve_win32_coordinates() -> None:
    assert _signed_word(0x0001) == 1
    assert _signed_word(0xFFFF) == -1
    assert _signed_word(0x8000) == -32_768


def test_native_pointer_buffer_decodes_and_bounds_ephemeral_events() -> None:
    native = object.__new__(_InteractiveWin32OperatorShellApi)
    native._pointer_events = deque()

    native._append_pointer_event(
        kind=WindowsPointerEventKind.DOWN,
        shell=71,
        hwnd=0,
        lparam=(0xFFFE << 16) | 0x0003,
    )
    assert native.drain_pointer_events(1) == (
        WindowsPointerEvent(kind=WindowsPointerEventKind.DOWN, x=3, y=-2),
    )

    with pytest.raises(_NativeShellError) as invalid_batch:
        native.drain_pointer_events(0)
    assert invalid_batch.value.failure == _NativeShellFailure.PUMP

    event = WindowsPointerEvent(kind=WindowsPointerEventKind.MOVE, x=1, y=1)
    native._pointer_events = deque([event] * 256)
    with pytest.raises(_NativeShellError) as overflow:
        native._append_pointer_event(
            kind=WindowsPointerEventKind.UP,
            shell=71,
            hwnd=0,
            lparam=0,
        )
    assert overflow.value.failure == _NativeShellFailure.PUMP


class _DrainNative:
    def __init__(self, result: object) -> None:
        self.result = result
        self.max_events = 0

    def drain_pointer_events(self, max_events: int) -> object:
        self.max_events = max_events
        return self.result


class _FailingDrainNative:
    def drain_pointer_events(self, _max_events: int) -> object:
        raise _NativeShellError(_NativeShellFailure.PUMP)


def _interactive_app(native: object) -> BoundedInteractiveWindowsOperatorApplication:
    return BoundedInteractiveWindowsOperatorApplication(
        native_api=native,
        host_factory=lambda _parent: object(),
    )


def test_interactive_application_drains_valid_events_and_missing_boundary() -> None:
    event = WindowsPointerEvent(kind=WindowsPointerEventKind.UP, x=17, y=29)
    native = _DrainNative((event,))
    app = _interactive_app(native)

    assert app.drain_pointer_events(max_events=3) == (event,)
    assert native.max_events == 3
    assert _interactive_app(object()).drain_pointer_events() == ()


def test_interactive_application_rejects_invalid_pointer_batches() -> None:
    app = _interactive_app(_DrainNative(()))
    with pytest.raises(WindowsOperatorApplicationError) as invalid_bound:
        app.drain_pointer_events(max_events=0)
    assert invalid_bound.value.code == WindowsOperatorApplicationErrorCode.INVALID_CONFIGURATION

    with pytest.raises(WindowsOperatorApplicationError) as native_failure:
        _interactive_app(_FailingDrainNative()).drain_pointer_events()
    assert native_failure.value.code == WindowsOperatorApplicationErrorCode.PUMP_FAILURE

    with pytest.raises(WindowsOperatorApplicationError) as invalid_result:
        _interactive_app(_DrainNative([object()])).drain_pointer_events()
    assert invalid_result.value.code == WindowsOperatorApplicationErrorCode.PUMP_FAILURE

    with pytest.raises(WindowsOperatorApplicationError) as invalid_item:
        _interactive_app(_DrainNative((object(),))).drain_pointer_events()
    assert invalid_item.value.code == WindowsOperatorApplicationErrorCode.PUMP_FAILURE
