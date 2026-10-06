"""Generated-state settled-resize regressions; no native execution or media sources."""

from __future__ import annotations

import asyncio

from k5vision.media.viewport_geometry import ViewportGeometry, ViewportLayout, ViewportPlacement
from k5vision.media.windows_operator_application import BoundedWindowsOperatorApplication
from k5vision.media.windows_operator_host import (
    WindowsOperatorHostSnapshot,
    WindowsOperatorHostState,
)


def layout() -> ViewportLayout:
    return ViewportLayout(
        placements=tuple(
            ViewportPlacement(
                logical_slot=slot,
                geometry=ViewportGeometry(x=index * 640, y=0, width=640, height=720),
            )
            for index, slot in enumerate((7, 4095))
        )
    )


class Native:
    def __init__(self) -> None:
        self.size = (1280, 720)
        self.minimum_calls = []
        self.closed = False

    def create_shell(self, width, height):
        return 71

    def client_size(self, shell):
        return self.size

    def ensure_client_size(self, shell, width, height):
        self.minimum_calls.append((width, height))
        self.size = (max(width, self.size[0]), max(height, self.size[1]))
        return self.size

    def pump_messages(self, shell, limit):
        return 1, False

    def destroy_shell(self, shell):
        self.closed = True


class Host:
    def __init__(self) -> None:
        self.layouts = []
        self.relayouts = []
        self.streams = None
        self.closed = False
        self.state = WindowsOperatorHostState.READY

    @property
    def snapshot(self):
        return WindowsOperatorHostSnapshot(
            state=self.state,
            generation=1,
            generation_limit=16,
            completed_generations=0,
            stopped_generations=0,
            failures=0,
            viewport_count=2,
            open_surface_count=0 if self.closed else 2,
            stream_count=2,
            delivered_frames=9,
            presentations=9,
        )

    async def start(self, selected, streams):
        self.layouts.append(selected)
        self.streams = streams
        self.state = WindowsOperatorHostState.RUNNING
        return self.snapshot

    async def replace(self, selected, streams):
        return await self.start(selected, streams)

    async def relayout(self, selected):
        self.relayouts.append(selected)
        return self.snapshot

    async def wait(self):
        return self.snapshot

    async def stop(self):
        self.state = WindowsOperatorHostState.STOPPED
        return self.snapshot

    async def close(self):
        self.closed = True
        self.state = WindowsOperatorHostState.CLOSED
        return self.snapshot


def test_settled_shell_resize_keeps_both_camera_tiles_inside_client() -> None:
    async def scenario():
        native, host = Native(), Host()
        app = BoundedWindowsOperatorApplication(native_api=native, host_factory=lambda _shell: host)
        canonical = layout()
        original = canonical.model_dump_json()
        streams = (object(), object())
        await app.open(1280, 720)
        await app.start(canonical, streams)
        native.size = (640, 360)
        try:
            await app.pump()
            assert len(host.relayouts) == 1
            assert [
                (p.logical_slot, p.geometry.x, p.geometry.y, p.geometry.width, p.geometry.height)
                for p in host.relayouts[-1].placements
            ] == [(7, 0, 0, 320, 360), (4095, 320, 0, 320, 360)]
            assert canonical.model_dump_json() == original
            assert host.streams is streams
            assert app.snapshot.generation == 1
            assert app.snapshot.delivered_frames == app.snapshot.presentations == 9
        finally:
            await app.close()

    asyncio.run(scenario())


def test_resize_is_coalesced_and_minimize_restore_never_changes_canonical_state():
    async def scenario():
        native, host = Native(), Host()
        app = BoundedWindowsOperatorApplication(native_api=native, host_factory=lambda _: host)
        canonical = layout()
        before = canonical.model_dump_json()
        await app.open(1280, 720)
        await app.start(canonical, ())
        try:
            for size in ((777, 431), (640, 360), (777, 431), (1280, 720)):
                native.size = size
                await app.pump()
                count = len(host.relayouts)
                for _ in range(3):
                    await app.pump()
                assert len(host.relayouts) == count
            assert host.relayouts[-1] == canonical
            count = len(host.relayouts)
            native.size = (0, 0)
            for _ in range(3):
                await app.pump()
            assert len(host.relayouts) == count
            assert not native.minimum_calls
            native.size = (1280, 720)
            await app.pump()
            assert len(host.relayouts) == count
            assert canonical.model_dump_json() == before
            assert app.snapshot.generation == 1
        finally:
            await app.close()

    asyncio.run(scenario())


def test_tiny_positive_client_is_clamped_once_without_resize_loop():
    async def scenario():
        native, host = Native(), Host()
        app = BoundedWindowsOperatorApplication(native_api=native, host_factory=lambda _: host)
        await app.open(1280, 720)
        await app.start(layout(), ())
        try:
            native.size = (1, 2)
            await app.pump()
            assert native.size == (192, 192)
            assert native.minimum_calls == [(192, 192)]
            count = len(host.relayouts)
            for _ in range(10):
                await app.pump()
            assert len(host.relayouts) == count == 1
            assert native.minimum_calls == [(192, 192)]
            assert not host.closed
        finally:
            await app.close()

    asyncio.run(scenario())


def test_catalog_minimum_reserves_chrome_and_refreshes_after_child_relayout():
    from k5vision.media.windows_operator_catalog_ui import BoundedCatalogWindowsOperatorApplication

    async def scenario():
        native, host = Native(), Host()
        calls = []
        native.after_viewport_layout = lambda: calls.append(len(host.relayouts))
        app = BoundedCatalogWindowsOperatorApplication(
            native_api=native, host_factory=lambda _: host
        )
        await app.open(1280, 720)
        await app.start(layout(), ())
        try:
            assert all(p.geometry.y >= 40 for p in host.layouts[-1].placements)
            native.size = (1, 1)
            await app.pump()
            assert native.size == (608, 232)
            assert all(p.geometry.y >= 40 for p in host.relayouts[-1].placements)
            assert calls == [0, 1]
        finally:
            await app.close()

    asyncio.run(scenario())


class PointerNative(Native):
    def __init__(self):
        super().__init__()
        self.events = []
        self.discards = 0
        self.capture = False

    def drain_pointer_events(self, limit):
        result = tuple(self.events[:limit])
        del self.events[:limit]
        return result

    def discard_pointer_events(self, shell):
        self.events.clear()
        self.discards += 1
        self.capture = False


def test_resize_cancels_released_but_undrained_drag_and_selection_before_reinterpretation():
    from k5vision.media.windows_operator_interaction import (
        BoundedInteractiveWindowsOperatorApplication,
        WindowsPointerEvent,
    )
    from k5vision.media.windows_operator_interaction import (
        WindowsPointerEventKind as Kind,
    )
    from k5vision.media.windows_operator_selection import BoundedSelectableWindowsOperatorControl

    async def scenario():
        native, host = PointerNative(), Host()
        app = BoundedInteractiveWindowsOperatorApplication(
            native_api=native, host_factory=lambda _: host
        )
        canonical = layout()
        control = BoundedSelectableWindowsOperatorControl()
        control._active_layout = canonical
        await app.open(1280, 720)
        await app.start(canonical, ())
        app.drain_pointer_events()  # Initial geometry epoch CANCEL.
        native.events = [WindowsPointerEvent(kind=Kind.DOWN, x=700, y=100)]
        control._consume_pointer_events(app.drain_pointer_events())
        assert control._pointer_drag is not None and control._selection_candidate is not None
        native.events = [WindowsPointerEvent(kind=Kind.UP, x=700, y=100)]
        native.capture = False  # Native UP was already processed before geometry sampling.
        native.size = (640, 360)
        try:
            await app.pump()
            events = app.drain_pointer_events()
            assert len(events) == 1 and events[0].kind is Kind.CANCEL
            control._consume_pointer_events(events)
            assert control._pointer_drag is None and control._selection_candidate is None
            assert control._controls.empty()
            assert not native.events
            # A new click in the second physical tile maps to the same logical camera.
            native.events = [
                WindowsPointerEvent(kind=Kind.DOWN, x=350, y=100),
                WindowsPointerEvent(kind=Kind.UP, x=350, y=100),
            ]
            control._consume_pointer_events(app.drain_pointer_events())
            assert control._selected_logical_slot == 4095
            native.size = (0, 0)
            native.events = [WindowsPointerEvent(kind=Kind.DOWN, x=30, y=30)]
            await app.pump()
            assert app.drain_pointer_events()[0].kind is Kind.CANCEL
            assert not native.events
        finally:
            await app.close()

    asyncio.run(scenario())


def test_save_apply_undo_redo_remain_in_canonical_coordinates_across_resizes():
    from k5vision.media.viewport_catalog import serialize_viewport_catalog
    from k5vision.media.viewport_editor import ViewportMove
    from k5vision.media.windows_operator_catalog_commands import (
        BoundedCommandWindowsOperatorControl,
        WindowsOperatorCatalogCommand,
    )
    from k5vision.media.windows_operator_catalog_commands import (
        WindowsOperatorCatalogCommandKind as Command,
    )
    from k5vision.media.windows_operator_interaction import (
        BoundedInteractiveWindowsOperatorApplication,
    )
    from k5vision.media.windows_operator_session import WindowsOperatorSessionState

    async def scenario():
        native, host = PointerNative(), Host()
        app = BoundedInteractiveWindowsOperatorApplication(
            native_api=native, host_factory=lambda _: host
        )
        canonical = layout()
        streams = (object(), object())
        await app.open(1280, 720)
        await app.start(canonical, streams)
        control = BoundedCommandWindowsOperatorControl()
        control._application = app
        control._state = WindowsOperatorSessionState.RUNNING
        control._active_layout = canonical
        try:
            control.request_edit(ViewportMove(logical_slot=7, dx=17, dy=11))
            await control._process_controls(app, None)
            edited = control.control_snapshot.active_layout
            assert edited != canonical
            control.dispatch_catalog_command(
                WindowsOperatorCatalogCommand(kind=Command.SAVE, view_id=9)
            )
            saved = serialize_viewport_catalog(control._catalog)
            before = control.history_control_snapshot
            for size in ((640, 360), (777, 431), (0, 0), (1280, 720)):
                native.size = size
                await app.pump()
                control._consume_pointer_events(app.drain_pointer_events())
                after = control.history_control_snapshot
                assert (
                    after.undo_depth,
                    after.redo_depth,
                    after.history_operations,
                    after.history_rebases,
                ) == (
                    before.undo_depth,
                    before.redo_depth,
                    before.history_operations,
                    before.history_rebases,
                )
                assert control.control_snapshot.active_layout == edited
                assert serialize_viewport_catalog(control._catalog) == saved
            control.request_undo()
            await control._process_controls(app, None)
            assert control.control_snapshot.active_layout == canonical
            assert control.history_control_snapshot.redo_depth == 1
            native.size = (640, 360)
            await app.pump()
            assert control.history_control_snapshot.redo_depth == 1
            control.request_redo()
            await control._process_controls(app, None)
            assert control.control_snapshot.active_layout == edited
            control.request_undo()
            await control._process_controls(app, None)
            control.dispatch_catalog_command(
                WindowsOperatorCatalogCommand(kind=Command.APPLY, view_id=9)
            )
            await control._process_controls(app, None)
            assert control.control_snapshot.active_layout == edited
            assert serialize_viewport_catalog(control._catalog) == saved
            assert control.history_control_snapshot.history_rebases == 1  # Explicit Apply only.
            assert host.streams is streams and app.snapshot.generation == 1
        finally:
            await app.close()

    asyncio.run(scenario())


def test_input_is_fenced_while_async_child_relayout_is_in_progress():
    from k5vision.media.windows_operator_interaction import (
        BoundedInteractiveWindowsOperatorApplication,
        WindowsPointerEvent,
    )
    from k5vision.media.windows_operator_interaction import (
        WindowsPointerEventKind as Kind,
    )

    async def scenario():
        native, host = PointerNative(), Host()
        app = BoundedInteractiveWindowsOperatorApplication(
            native_api=native, host_factory=lambda _: host
        )
        await app.open(1280, 720)
        await app.start(layout(), ())
        app.drain_pointer_events()
        entered, release = asyncio.Event(), asyncio.Event()
        original = host.relayout

        async def delayed(candidate):
            entered.set()
            await release.wait()
            return await original(candidate)

        host.relayout = delayed
        native.events = [WindowsPointerEvent(kind=Kind.UP, x=700, y=100)]
        native.capture = True
        native.size = (640, 360)
        task = asyncio.create_task(app.pump())
        try:
            await entered.wait()
            assert not native.capture and not native.events
            assert app.drain_pointer_events()[0].kind is Kind.CANCEL
            native.events = [WindowsPointerEvent(kind=Kind.DOWN, x=350, y=100)]
            assert app.drain_pointer_events()[0].kind is Kind.CANCEL
            release.set()
            await task
            native.events = [WindowsPointerEvent(kind=Kind.DOWN, x=350, y=100)]
            assert app.drain_pointer_events()[0].x == 701
        finally:
            release.set()
            await task
            await app.close()

    asyncio.run(scenario())


def test_start_minimized_then_restore_same_prepared_size_is_nonfatal():
    from k5vision.media.windows_operator_interaction import (
        BoundedInteractiveWindowsOperatorApplication,
    )

    async def scenario():
        native, host = PointerNative(), Host()
        native.size = (0, 0)
        app = BoundedInteractiveWindowsOperatorApplication(
            native_api=native, host_factory=lambda _: host
        )
        await app.open(1280, 720)
        await app.start(layout(), ())
        assert not native.minimum_calls
        assert app._pointer_projection_suspended
        try:
            native.size = (192, 192)
            await app.pump()
            assert not app._pointer_projection_suspended
            assert not host.relayouts
            assert not host.closed
            assert app.drain_pointer_events()[0].kind.value == "cancel"
        finally:
            await app.close()

    asyncio.run(scenario())


def test_failed_child_relayout_postcondition_fails_closed_without_accepting_projection():
    import pytest

    from k5vision.media.windows_operator_application import WindowsOperatorApplicationError

    async def scenario():
        native, host = Native(), Host()
        app = BoundedWindowsOperatorApplication(native_api=native, host_factory=lambda _: host)
        await app.open(1280, 720)
        await app.start(layout(), ())
        original = app._projection

        async def failed(candidate):
            host.state = WindowsOperatorHostState.FAILED
            return host.snapshot

        host.relayout = failed
        native.size = (640, 360)
        with pytest.raises(WindowsOperatorApplicationError, match="client relayout failed"):
            await app.pump()
        assert app._projection == original
        assert host.closed and native.closed
        assert app.snapshot.state.value == "failed"

    asyncio.run(scenario())


def test_pointer_discard_failure_is_sanitized_and_closes_owned_resources():
    import pytest

    from k5vision.media.windows_operator_application import WindowsOperatorApplicationError
    from k5vision.media.windows_operator_interaction import (
        BoundedInteractiveWindowsOperatorApplication,
    )

    async def scenario():
        native, host = PointerNative(), Host()
        app = BoundedInteractiveWindowsOperatorApplication(
            native_api=native, host_factory=lambda _: host
        )
        await app.open(1280, 720)
        await app.start(layout(), ())

        def failed(_shell):
            raise OSError("private capture failure detail")

        native.discard_pointer_events = failed
        native.size = (640, 360)
        with pytest.raises(WindowsOperatorApplicationError) as error:
            await app.pump()
        assert "private" not in str(error.value)
        assert host.closed and native.closed
        assert not host.relayouts

    asyncio.run(scenario())


def test_failed_minimum_clamp_is_attempted_once_then_sanitized():
    import pytest

    from k5vision.media.windows_operator_application import WindowsOperatorApplicationError

    async def scenario():
        native, host = Native(), Host()
        app = BoundedWindowsOperatorApplication(native_api=native, host_factory=lambda _: host)
        await app.open(1280, 720)
        await app.start(layout(), ())
        calls = []

        def refuse(shell, width, height):
            calls.append((width, height))
            return (1, 1)

        native.ensure_client_size = refuse
        native.size = (1, 1)
        with pytest.raises(WindowsOperatorApplicationError):
            await app.pump()
        assert calls == [(192, 192)]
        assert host.closed and native.closed

    asyncio.run(scenario())


def test_native_minimum_uses_client_frame_delta_and_no_retry_or_minimized_restore():
    import ctypes

    from k5vision.media.windows_operator_application import _ShellRect, _Win32OperatorShellApi

    api = object.__new__(_Win32OperatorShellApi)
    size = [1, 2]
    minimized = False
    calls = []

    def client(_handle, pointer):
        rect = ctypes.cast(pointer, ctypes.POINTER(_ShellRect)).contents
        rect.left = rect.top = 0
        rect.right, rect.bottom = size
        return 1

    def outer(_handle, pointer):
        rect = ctypes.cast(pointer, ctypes.POINTER(_ShellRect)).contents
        rect.left, rect.top = 100, 100
        rect.right, rect.bottom = 117, 142  # 16px/40px nonclient additions.
        return 1

    def resize(handle, after, x, y, width, height, flags):
        calls.append((handle.value, after, x, y, width, height, flags))
        size[:] = [width - 16, height - 40]
        return 1

    api._get_client_rect, api._get_window_rect = client, outer
    api._is_iconic = lambda _handle: int(minimized)
    api._set_window_pos = resize
    assert ctypes.sizeof(_ShellRect) == 16
    assert api.ensure_client_size(71, 608, 232) == (608, 232)
    assert calls == [(71, None, 0, 0, 624, 272, 0x16)]
    assert api.ensure_client_size(71, 608, 232) == (608, 232)
    assert len(calls) == 1
    minimized = True
    assert api.ensure_client_size(71, 608, 232) == (0, 0)
    assert len(calls) == 1


def test_native_geometry_change_releases_only_this_shell_capture():
    from collections import deque

    from k5vision.media.windows_operator_interaction import (
        WindowsPointerEvent,
        _InteractiveWin32OperatorShellApi,
    )
    from k5vision.media.windows_operator_interaction import (
        WindowsPointerEventKind as Kind,
    )

    for captured in (71, 88, 0):
        api = object.__new__(_InteractiveWin32OperatorShellApi)
        api._pointer_events = deque([WindowsPointerEvent(kind=Kind.UP, x=8, y=9)])
        api._capture_active = True
        api._get_tick_count = lambda: 200
        released = []
        api._get_capture = lambda captured=captured: captured
        api._release_capture = lambda released=released: released.append(71) or 1
        api.discard_pointer_events(71)
        assert not api._capture_active and not api._pointer_events
        assert released == ([71] if captured == 71 else [])


def test_old_win32_pointer_backlog_cannot_cross_a_geometry_fence(request):
    import ctypes
    from collections import deque

    from k5vision.media.windows_operator_application import _Win32Message
    from k5vision.media.windows_operator_interaction import _InteractiveWin32OperatorShellApi

    api = object.__new__(_InteractiveWin32OperatorShellApi)
    api._pointer_events = deque()
    api._capture_active = False
    api._get_tick_count = lambda: 200
    captured = [0]
    api._set_capture = lambda handle: captured.__setitem__(0, handle.value) or 0
    api._get_capture = lambda captured=captured: captured[0]
    api._release_capture = lambda: captured.__setitem__(0, 0) or 1
    api._map_window_points = lambda *_args: 0
    queue = deque([(0, 100), (0x0201, 150), (0x0202, 160), (0x0201, 220), (0x0202, 230)])

    def peek(pointer, *_args):
        if not queue:
            return 0
        kind, timestamp = queue.popleft()
        message = ctypes.cast(pointer, ctypes.POINTER(_Win32Message)).contents
        message.hwnd, message.message, message.time = 71, kind, timestamp
        message.lParam = (100 << 16) | 350
        return 1

    from k5vision.media.windows_operator_message_routing import OwnedShellMessageRoute

    api._message_route = OwnedShellMessageRoute()
    api._message_route.register(71)
    request.addfinalizer(lambda: api._message_route.unregister(71))
    api._peek_message = peek
    api._translate_message = lambda *_args: 1
    api._dispatch_message = lambda *_args: 0
    api.pump_messages(71, 1)  # Geometry changes while old pointer messages remain in the OS queue.
    api.discard_pointer_events(71)
    api.pump_messages(71, 2)
    assert api.drain_pointer_events(16) == ()
    api.pump_messages(71, 2)
    assert [e.kind.value for e in api.drain_pointer_events(16)] == ["down", "up"]


def test_pointer_fence_handles_equal_time_and_dword_wrap():
    from k5vision.media.windows_operator_application import _Win32Message
    from k5vision.media.windows_operator_interaction import _InteractiveWin32OperatorShellApi

    api = object.__new__(_InteractiveWin32OperatorShellApi)
    api._pointer_time_fence = 0xFFFFFFF0
    message = _Win32Message()
    message.message = 0x0201
    for timestamp, stale in (
        (0xFFFFFFE0, True),
        (0xFFFFFFF0, True),
        (0xFFFFFFF1, False),
        (0x10, False),
    ):
        message.time = timestamp
        assert api._stale_pointer_message(message) is stale
    message.message = 0x0111  # Catalog command, never a pointer-fence subject.
    message.time = 0
    assert api._stale_pointer_message(message) is False


def test_resize_intent_invalidates_same_size_input_without_relayout():
    from k5vision.media.windows_operator_interaction import (
        BoundedInteractiveWindowsOperatorApplication,
        WindowsPointerEvent,
    )
    from k5vision.media.windows_operator_interaction import (
        WindowsPointerEventKind as Kind,
    )

    async def scenario():
        native, host = PointerNative(), Host()
        pending = [False]

        def take():
            value = pending[0]
            pending[0] = False
            return value

        native.take_geometry_change = take
        app = BoundedInteractiveWindowsOperatorApplication(
            native_api=native, host_factory=lambda _: host
        )
        await app.open(1280, 720)
        await app.start(layout(), ())
        app.drain_pointer_events()
        try:
            native.events = [WindowsPointerEvent(kind=Kind.UP, x=700, y=100)]
            pending[0] = True  # Border drag returned to A after an intermediate B.
            await app.pump()
            assert not host.relayouts
            assert app.drain_pointer_events()[0].kind is Kind.CANCEL
            await app.pump()
            assert app.drain_pointer_events() == ()
        finally:
            await app.close()

    asyncio.run(scenario())


def test_native_geometry_intent_is_scoped_to_own_shell():
    from k5vision.media.windows_operator_application import _Win32Message, _Win32OperatorShellApi

    api = object.__new__(_Win32OperatorShellApi)
    message = _Win32Message()
    for hwnd, kind, value, expected in (
        (72, 0x0005, 0, False),
        (71, 0x0111, 0, False),
        (71, 0x00A1, 11, True),
        (71, 0x0112, 0xF030, True),
        (71, 0x0112, 0xF060, False),
        (71, 0x0005, 0, True),
    ):
        message.hwnd, message.message, message.wParam = hwnd, kind, value
        api._note_geometry_message(71, message)
        assert api.take_geometry_change() is expected
        assert api.take_geometry_change() is False


def test_pointer_fence_preserves_catalog_and_other_window_messages(request):
    import ctypes
    from collections import deque

    from k5vision.media.windows_operator_application import _Win32Message
    from k5vision.media.windows_operator_catalog_ui import _CatalogWin32OperatorShellApi

    api = object.__new__(_CatalogWin32OperatorShellApi)
    api._pointer_events = deque()
    api._pointer_time_fence = 200
    api._capture_active = False
    api._ui_handles = {99}
    api._is_child = lambda parent, child: int(child.value == 99)
    queue = deque([(71, 0x0201), (99, 0x0201), (88, 0x0201), (71, 0x0111)])
    dispatched, commands = [], []

    def peek(pointer, owner, *_args):
        assert owner.value == 71
        for index, (hwnd, kind) in enumerate(queue):
            if hwnd not in {71, 99}:
                continue
            del queue[index]
            message = ctypes.cast(pointer, ctypes.POINTER(_Win32Message)).contents
            message.hwnd, message.message, message.time = hwnd, kind, 100
            return 1
        return 0

    def dispatch(pointer):
        message = ctypes.cast(pointer, ctypes.POINTER(_Win32Message)).contents
        dispatched.append((message.hwnd, message.message))
        return 0

    from k5vision.media.windows_operator_message_routing import OwnedShellMessageRoute

    api._message_route = OwnedShellMessageRoute()
    api._message_route.register(71)
    request.addfinalizer(lambda: api._message_route.unregister(71))
    api._peek_message = peek
    api._translate_message = lambda *_args: 1
    api._dispatch_message = dispatch
    api._consume_catalog_command_message = lambda *args: commands.append(args) or True
    assert api.pump_messages(71, 4) == (3, False)
    assert api.drain_pointer_events(16) == ()
    assert dispatched == [(99, 0x0201)]
    assert tuple(queue) == ((88, 0x0201),)
    assert len(commands) == 1
    assert not api._capture_active


def test_inverse_mapping_supports_large_canonical_coordinates_without_widening_raw_input():
    import pytest
    from pydantic import ValidationError

    from k5vision.media.windows_operator_interaction import (
        BoundedInteractiveWindowsOperatorApplication,
        WindowsPointerEvent,
    )
    from k5vision.media.windows_operator_interaction import (
        WindowsPointerEventKind as Kind,
    )

    async def scenario():
        native, host = PointerNative(), Host()
        canonical = ViewportLayout(
            placements=(
                ViewportPlacement(
                    logical_slot=7, geometry=ViewportGeometry(x=0, y=0, width=500_000, height=1000)
                ),
                ViewportPlacement(
                    logical_slot=4095,
                    geometry=ViewportGeometry(x=500_000, y=0, width=500_000, height=1000),
                ),
            )
        )
        app = BoundedInteractiveWindowsOperatorApplication(
            native_api=native, host_factory=lambda _: host
        )
        await app.open(1280, 720)
        await app.start(canonical, ())
        app.drain_pointer_events()
        try:
            native.events = [WindowsPointerEvent(kind=Kind.DOWN, x=1000, y=100)]
            projected = app.drain_pointer_events()[0]
            assert isinstance(projected, WindowsPointerEvent)
            assert projected.x > 500_000
            with pytest.raises(ValidationError):
                WindowsPointerEvent(kind=Kind.DOWN, x=projected.x, y=0)
        finally:
            await app.close()

    asyncio.run(scenario())


def test_close_during_minimized_epoch_preserves_media_counters_and_closes_once():
    from k5vision.media.windows_operator_interaction import (
        BoundedInteractiveWindowsOperatorApplication,
    )

    async def scenario():
        native, host = PointerNative(), Host()
        app = BoundedInteractiveWindowsOperatorApplication(
            native_api=native, host_factory=lambda _: host
        )
        await app.open(1280, 720)
        await app.start(layout(), ())
        native.size = (0, 0)
        await app.pump()
        before = app.snapshot
        await app.close()
        assert host.closed and native.closed
        assert app.snapshot.delivered_frames == before.delivered_frames
        assert app.snapshot.presentations == before.presentations
        assert (await app.close()).state.value == "closed"

    asyncio.run(scenario())
