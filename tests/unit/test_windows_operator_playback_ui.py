"""Pure playback UI input tests; native acceptance is a separate Windows gate."""

from __future__ import annotations

import asyncio
from collections import deque
from types import SimpleNamespace

import pytest
from test_windows_operator_settled_resize import Host, Native, layout

from k5vision.media import windows_operator_message_routing as routing
from k5vision.media.playback_control import PlaybackControlState, PlaybackPauseControl
from k5vision.media.windows_operator_application import _NativeShellError, _Win32Message
from k5vision.media.windows_operator_playback_ui import (
    _BUTTONS,
    _MAX_COMMANDS,
    _PAUSE_BUTTON_ID,
    _RESUME_BUTTON_ID,
    BoundedPlaybackWindowsOperatorApplication,
    BoundedPlaybackWindowsOperatorControl,
    WindowsPlaybackCommand,
    _PlaybackWin32OperatorShellApi,
)


@pytest.fixture(autouse=True)
def isolate_routes(monkeypatch):
    monkeypatch.setattr(routing, "_ROUTES", routing._ThreadRoutes())


def shell_api():
    api = object.__new__(_PlaybackWin32OperatorShellApi)
    api._message_route = routing.OwnedShellMessageRoute()
    api._message_route.register(71)
    api._shell_registration = api._message_route.registration(71)
    api._playback_commands = deque()
    api._button_handles = {
        identifier: 100 + index for index, (identifier, *_) in enumerate(_BUTTONS)
    }
    api._close_requested = False
    api._display_state = None
    api._window_procedure = SimpleNamespace(failed=False, destroyed=False)
    api._geometry_change_pending = False
    api._enable_window = lambda *_: 1
    return api


def test_valid_commands_are_source_free_and_unknown_or_stale_input_is_inert():
    api = shell_api()
    for identifier, _label, expected in _BUTTONS:
        handle = api._button_handles[identifier]
        assert api._receive_window_message(71, 0x111, identifier, handle) == 0
        assert api.drain_playback_commands(1) == (expected,)
        for wparam, lparam in (
            (identifier, 0),
            (identifier, handle + 99),
            (identifier | 0x10000, handle),
            (0xFFFF, handle),
        ):
            assert api._receive_window_message(71, 0x111, wparam, lparam) == 0
            assert api.drain_playback_commands(1) == ()


def test_close_is_latched_and_does_not_mutate_thread_quit_or_another_shell():
    api = shell_api()
    peer = routing.OwnedShellMessageRoute()
    peer.register(72)
    for _ in range(2):
        assert api._receive_window_message(71, 0x10, 0, 0) == 0
    assert api._thread_quit_requested(71)
    assert not peer.quit_requested(72)
    assert not api._message_route.quit_requested(71)
    api._receive_window_message(71, 0x111, _PAUSE_BUTTON_ID, 100)
    assert api.drain_playback_commands(4) == ()


def test_command_queue_is_bounded_and_overflow_is_a_callback_failure():
    api = shell_api()
    for _ in range(_MAX_COMMANDS):
        api._receive_window_message(71, 0x111, _PAUSE_BUTTON_ID, 100)
    with pytest.raises(_NativeShellError):
        api._receive_window_message(71, 0x111, _PAUSE_BUTTON_ID, 100)
    assert len(api.drain_playback_commands(4)) == 4
    assert len(api._playback_commands) == _MAX_COMMANDS - 4
    for bound in (0, 65, True):
        with pytest.raises(_NativeShellError):
            api.drain_playback_commands(bound)


def test_button_mouse_input_is_dispatched_instead_of_becoming_a_tile_drag():
    api = shell_api()
    api._is_child = lambda *_: 1
    message = _Win32Message()
    message.hwnd = 100
    message.message = 0x201
    assert not api._pointer_message_for_shell(71, message)
    message.hwnd = 171
    assert api._pointer_message_for_shell(71, message)


def test_button_state_tracks_pause_object_without_repeated_native_mutation():
    api = shell_api()
    changes = []
    api._enable_window = lambda hwnd, value: changes.append((hwnd.value, value))
    api.set_playback_state(71, PlaybackControlState.RUNNING)
    assert changes == [(100, 1), (101, 0), (102, 1)]
    api.set_playback_state(71, PlaybackControlState.RUNNING)
    assert len(changes) == 3
    api.set_playback_state(71, PlaybackControlState.PAUSED)
    assert changes[-3:] == [(100, 0), (101, 1), (102, 1)]
    api._window_procedure.failed = True
    with pytest.raises(_NativeShellError):
        api.set_playback_state(71, PlaybackControlState.RUNNING)


class PlaybackNative(Native):
    def __init__(self):
        super().__init__()
        self.commands = deque()
        self.states = []
        self.close_requested = False
        self.close_count = 0

    def drain_playback_commands(self, bound):
        result = []
        while self.commands and len(result) < bound:
            result.append(self.commands.popleft())
        return tuple(result)

    def set_playback_state(self, shell, state):
        self.states.append(state)

    def pump_messages(self, shell, limit):
        return 1, self.close_requested

    def destroy_shell(self, shell):
        self.close_count += 1
        super().destroy_shell(shell)


class WaitingHost(Host):
    def __init__(self, *, fail=None):
        super().__init__()
        self.finish = asyncio.Event()
        self.close_count = 0
        self.fail = fail

    async def start(self, selected, streams):
        if self.fail == "start":
            raise RuntimeError("private source")
        return await super().start(selected, streams)

    async def wait(self):
        await self.finish.wait()
        if self.fail == "runtime":
            raise RuntimeError("private source")
        from k5vision.media.windows_operator_host import WindowsOperatorHostState

        self.state = WindowsOperatorHostState.COMPLETE
        return self.snapshot

    async def close(self):
        self.close_count += 1
        value = await super().close()
        if self.fail == "cleanup":
            raise RuntimeError("private source")
        return value


def application(control, native, host):
    return BoundedPlaybackWindowsOperatorApplication(
        pause_control=control,
        native_api=native,
        host_factory=lambda _: host,
    )


def test_toolbar_survives_resize_minimize_and_keeps_media_generation():
    async def scenario():
        control, native, host = PlaybackPauseControl(), PlaybackNative(), Host()
        app = application(control, native, host)
        await app.open(1280, 720)
        await app.start(layout(), ())
        try:
            assert all(p.geometry.y >= 40 for p in host.layouts[-1].placements)
            native.size = (1, 1)
            await app.pump()
            assert native.size == (272, 232)
            assert all(p.geometry.y >= 40 for p in host.relayouts[-1].placements)
            count = len(host.relayouts)
            native.size = (0, 0)
            await app.pump()
            assert len(host.relayouts) == count
            native.size = (1280, 720)
            await app.pump()
            assert len(host.layouts) == 1 and app.snapshot.generation == 1
        finally:
            await app.close()

    asyncio.run(scenario())


async def wait_until(predicate):
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("source-only session did not reach expected state")


def test_native_commands_and_api_share_gate_and_stop_is_not_natural_completion():
    async def scenario():
        pause, native, host = PlaybackPauseControl(), PlaybackNative(), WaitingHost()
        app = application(pause, native, host)
        session = BoundedPlaybackWindowsOperatorControl(
            pause_control=pause,
            application_factory=lambda: app,
            poll_interval_seconds=0,
        )
        task = asyncio.create_task(session.run(width=1280, height=720, layout=layout(), streams=()))
        await wait_until(lambda: bool(host.layouts))
        native.commands.extend((WindowsPlaybackCommand.PAUSE, WindowsPlaybackCommand.PAUSE))
        await wait_until(lambda: pause.snapshot.pause_count == 1)
        assert session.control_snapshot.processed_controls == 1
        await pause.resume()  # Represents the coordinator's exact shared object.
        await wait_until(lambda: native.states[-1] is PlaybackControlState.RUNNING)
        native.commands.extend(
            (
                WindowsPlaybackCommand.RESUME,
                WindowsPlaybackCommand.PAUSE,
                WindowsPlaybackCommand.STOP,
            )
        )
        result = await task
        assert result.session.state.value == "user_closed"
        assert result.processed_controls == 3
        assert pause.snapshot.pause_count == 2 and pause.snapshot.resume_count == 1
        assert host.close_count == native.close_count == 1
        assert not result.session.shell_open and result.session.open_surface_count == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("terminal", ["close", "cancel", "complete"])
def test_pause_before_start_and_terminal_cleanup(terminal):
    async def scenario():
        pause, native, host = PlaybackPauseControl(), PlaybackNative(), WaitingHost()
        await pause.pause()
        app = application(pause, native, host)
        session = BoundedPlaybackWindowsOperatorControl(
            pause_control=pause,
            application_factory=lambda: app,
            poll_interval_seconds=0,
        )
        task = asyncio.create_task(session.run(width=1280, height=720, layout=layout(), streams=()))
        await wait_until(lambda: bool(host.layouts))
        assert native.states[-1] is PlaybackControlState.PAUSED
        if terminal == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            if terminal == "close":
                native.close_requested = True
            else:
                host.finish.set()
            result = await task
            assert result.session.state.value == (
                "user_closed" if terminal == "close" else "complete"
            )
            assert result.session.delivered_frames == 9
        assert host.close_count == native.close_count == 1
        assert pause.snapshot.pause_count == 1 and pause.snapshot.resume_count == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", [None, "second_button", "install"])
def test_partial_native_construction_restores_callback_and_releases_owned_shell(
    monkeypatch, failure
):
    from test_windows_operator_window_procedure import User32

    from k5vision.media import windows_operator_window_procedure as procedure
    from k5vision.media.windows_operator_interaction import _InteractiveWin32OperatorShellApi

    monkeypatch.setattr(procedure, "_PINNED_PROCEDURES", set())
    api = shell_api()
    api._message_route.unregister(71)
    api._shell_registration = None
    api._window_procedure = None
    user = User32()
    api._user32 = user
    monkeypatch.setattr(procedure, "_make_callback", user.callback)
    api._button_handles.clear()
    api._get_module_handle = lambda _: 1
    creates, destroys = [], []

    def create(*args):
        creates.append(args)
        return 0 if failure == "second_button" and len(creates) == 2 else 100 + len(creates)

    def create_shell(self, width, height):
        self._message_route.register(71)
        return 71

    api._create_window = create
    api._destroy_window = lambda hwnd: destroys.append(hwnd.value) or 1
    monkeypatch.setattr(_InteractiveWin32OperatorShellApi, "create_shell", create_shell)
    user.fail_set = failure == "install"
    if failure:
        with pytest.raises(_NativeShellError):
            api.create_shell(1280, 720)
    else:
        assert api.create_shell(1280, 720) == 71
        assert len(api._button_handles) == 3
        api.destroy_shell(71)
    assert destroys == [71]
    assert not procedure._PINNED_PROCEDURES
    assert not routing._ROUTES.active
    assert not api._button_handles


def test_sent_callback_and_queued_command_each_enter_once_and_close_stays_deferred(monkeypatch):
    from test_windows_operator_window_procedure import User32

    from k5vision.media import windows_operator_window_procedure as procedure

    monkeypatch.setattr(procedure, "_PINNED_PROCEDURES", set())
    api = shell_api()
    user = User32()
    monkeypatch.setattr(procedure, "_make_callback", user.callback)
    boundary = procedure.OwnedWindowProcedure(
        user, 71, api._message_route, api._receive_window_message
    )
    api._window_procedure = boundary
    boundary.install()
    try:
        user.send(0x111, _PAUSE_BUTTON_ID, 100)
        assert api.drain_playback_commands(4) == (WindowsPlaybackCommand.PAUSE,)
        queue = deque([(0x111, _RESUME_BUTTON_ID, 101)])

        def peek(pointer, owner, *filters):
            import ctypes

            if owner.value == ctypes.c_void_p(-1).value:
                assert filters == (0x0012, 0x0012, 1)
                return 0

            if not queue:
                return 0
            message = ctypes.cast(pointer, ctypes.POINTER(_Win32Message)).contents
            message.hwnd = 71
            message.message, message.wParam, message.lParam = queue.popleft()
            return 1

        def dispatch(pointer):
            import ctypes

            message = ctypes.cast(pointer, ctypes.POINTER(_Win32Message)).contents
            return user.send(int(message.message), int(message.wParam), int(message.lParam))

        api._peek_message = peek
        api._translate_message = lambda *_: 1
        api._dispatch_message = dispatch
        api._pointer_events = deque()
        api._pointer_time_fence = None
        api._capture_active = False
        assert api.pump_messages(71, 4) == (1, False)
        assert api.drain_playback_commands(4) == (WindowsPlaybackCommand.RESUME,)
        user.send(0x10)
        assert api.pump_messages(71, 4) == (0, True)
        assert not boundary.destroyed
        api._message_route.require(71)
    finally:
        boundary.detach()
        api._message_route.unregister(71)
    assert not procedure._PINNED_PROCEDURES


@pytest.mark.parametrize("unexpected", [True, False])
def test_teardown_releases_terminal_pin_without_touching_reused_handle(monkeypatch, unexpected):
    from test_windows_operator_window_procedure import User32

    from k5vision.media import windows_operator_window_procedure as procedure

    monkeypatch.setattr(procedure, "_PINNED_PROCEDURES", set())
    api = shell_api()
    user = User32()
    monkeypatch.setattr(procedure, "_make_callback", user.callback)
    boundary = procedure.OwnedWindowProcedure(
        user, 71, api._message_route, api._receive_window_message
    )
    api._window_procedure = boundary
    boundary.install()
    destroys = []
    if unexpected:
        user.send(0x82)
        replacement = routing.OwnedShellMessageRoute()
        replacement.register(71)
        api._destroy_window = lambda hwnd: destroys.append(hwnd.value) or 1
    else:
        user.fail_set = True

        def destroy(hwnd):
            destroys.append(hwnd.value)
            user.send(0x82)
            return 1

        api._destroy_window = destroy
    with pytest.raises(_NativeShellError):
        api.destroy_shell(71)
    assert destroys == ([] if unexpected else [71])
    assert not procedure._PINNED_PROCEDURES
    if unexpected:
        replacement.require(71)
        replacement.unregister(71)


def test_signed_high_bit_button_handle_is_normalized():
    import ctypes

    api = shell_api()
    handle = 1 << (8 * ctypes.sizeof(ctypes.c_void_p) - 1)
    api._button_handles[_PAUSE_BUTTON_ID] = handle
    api._receive_window_message(71, 0x111, _PAUSE_BUTTON_ID, ctypes.c_ssize_t(handle).value)
    assert api.drain_playback_commands(4) == (WindowsPlaybackCommand.PAUSE,)


def test_stale_registration_never_destroys_new_same_hwnd_even_on_detach_failure(monkeypatch):
    api = shell_api()
    calls = []
    api._destroy_window = lambda hwnd: calls.append(hwnd.value) or 1
    from k5vision.media.windows_operator_window_procedure import WindowsOperatorWindowProcedureError

    def reject_stale_detach():
        raise WindowsOperatorWindowProcedureError()

    api._window_procedure.detach = reject_stale_detach
    api._message_route.unregister(71)
    api._message_route.register(71)
    current = api._message_route.registration(71)
    with pytest.raises(_NativeShellError):
        api.destroy_shell(71)
    assert not calls
    api._message_route.require(71, registration=current)


@pytest.mark.parametrize("callback_result", ["destroy", "reuse", "failed"])
def test_enable_window_revalidates_before_touching_another_child(callback_result):
    api = shell_api()
    changes = []

    def enable(hwnd, value):
        changes.append(hwnd.value)
        if callback_result == "failed":
            api._window_procedure.failed = True
        elif callback_result == "destroy":
            api._window_procedure.destroyed = True
            api._message_route.unregister(71)
        else:
            api._message_route.unregister(71)
            api._message_route.register(71)
        return 1

    api._enable_window = enable
    with pytest.raises(_NativeShellError):
        api.set_playback_state(71, PlaybackControlState.PAUSED)
    assert changes == [100]
    assert api._display_state is None


def test_child_creation_revalidates_after_synchronous_terminal_callback(monkeypatch):
    from test_windows_operator_window_procedure import User32

    from k5vision.media import windows_operator_window_procedure as procedure
    from k5vision.media.windows_operator_interaction import _InteractiveWin32OperatorShellApi

    monkeypatch.setattr(procedure, "_PINNED_PROCEDURES", set())
    api = shell_api()
    api._message_route.unregister(71)
    api._shell_registration = None
    api._window_procedure = None
    api._button_handles.clear()
    user = User32()
    api._user32 = user
    monkeypatch.setattr(procedure, "_make_callback", user.callback)
    api._get_module_handle = lambda _: 1
    creates, destroys = [], []

    def create(*args):
        creates.append(args)
        user.send(0x82)
        return 100

    def create_shell(self, width, height):
        self._message_route.register(71)
        return 71

    api._create_window = create
    api._destroy_window = lambda hwnd: destroys.append(hwnd.value) or 1
    monkeypatch.setattr(_InteractiveWin32OperatorShellApi, "create_shell", create_shell)
    with pytest.raises(_NativeShellError):
        api.create_shell(1280, 720)
    assert len(creates) == 1 and not destroys
    assert not api._button_handles and not procedure._PINNED_PROCEDURES


@pytest.mark.parametrize("terminal", ["reuse", "destroy"])
def test_detach_sent_callback_never_destroys_replaced_registration(monkeypatch, terminal):
    from test_windows_operator_window_procedure import User32

    from k5vision.media import windows_operator_window_procedure as procedure

    monkeypatch.setattr(procedure, "_PINNED_PROCEDURES", set())
    api = shell_api()
    user = User32()
    monkeypatch.setattr(procedure, "_make_callback", user.callback)
    boundary = procedure.OwnedWindowProcedure(
        user, 71, api._message_route, api._receive_window_message
    )
    api._window_procedure = boundary
    boundary.install()
    address = user.current
    calls = []
    api._destroy_window = lambda hwnd: calls.append(hwnd.value) or 1

    def during_restore():
        if terminal == "destroy":
            user.send(0x82, address=address)
        else:
            api._message_route.unregister(71)
            api._message_route.register(71)

    user.set_hook = during_restore
    with pytest.raises(_NativeShellError):
        api.destroy_shell(71)
    assert not calls
    if terminal == "reuse":
        api._message_route.require(71)
        # An uncertain native lifetime stays pinned rather than risking use-after-free.
        assert boundary in procedure._PINNED_PROCEDURES
    else:
        assert not procedure._PINNED_PROCEDURES
