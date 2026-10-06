"""Pure owned-queue regressions; no native API loading or real message/input calls."""

from __future__ import annotations

import ctypes
import threading
from collections import deque

import pytest

from k5vision.media import windows_operator_message_routing as routing
from k5vision.media.windows_operator_application import (
    _NativeShellError,
    _NativeShellFailure,
    _Win32Message,
    _Win32OperatorShellApi,
)
from k5vision.media.windows_operator_catalog_ui import (
    _SAVE_BUTTON_ID,
    _CatalogWin32OperatorShellApi,
)
from k5vision.media.windows_operator_interaction import _InteractiveWin32OperatorShellApi

_KINDS = (_Win32OperatorShellApi, _InteractiveWin32OperatorShellApi, _CatalogWin32OperatorShellApi)
_QUIT = 0x0012
_CLOSE = 0x0010
_MOVE = 0x0200


@pytest.fixture(autouse=True)
def isolated_routes(monkeypatch):
    monkeypatch.setattr(routing, "_ROUTES", routing._ThreadRoutes())


class Queue:
    def __init__(self, messages=()):
        self.messages = deque(messages)
        self.children = {171: 71, 172: 72, 271: 71, 272: 72}
        self.dispatched = []
        self.filters = []
        self.callbacks = deque()
        self.captured = 0
        self.acquisitions = []
        self.releases = []

    def peek(self, pointer, owner, first, last, removal):
        assert (first, last, removal) == (0, 0, 1)
        self.filters.append(owner.value)
        # Sent callbacks are independent of the HWND queued-message filter.
        if self.callbacks:
            self.callbacks.popleft()()
        for index, item in enumerate(self.messages):
            hwnd, kind, *parameters = item
            if kind != _QUIT and hwnd != owner.value and self.children.get(hwnd) != owner.value:
                continue
            del self.messages[index]
            message = ctypes.cast(pointer, ctypes.POINTER(_Win32Message)).contents
            message.hwnd, message.message = hwnd, kind
            message.wParam = parameters[0] if parameters else 0
            message.lParam = parameters[1] if len(parameters) > 1 else 0
            message.time = 100
            return 1
        return 0

    def dispatch(self, pointer):
        message = ctypes.cast(pointer, ctypes.POINTER(_Win32Message)).contents
        self.dispatched.append((int(message.hwnd or 0), int(message.message)))
        return 0

    def set_capture(self, hwnd):
        prior = self.captured
        self.captured = hwnd.value
        self.acquisitions.append(hwnd.value)
        return prior

    def release_capture(self):
        self.releases.append(self.captured)
        self.captured = 0
        return 1


def api_for(kind, shell, queue):
    api = object.__new__(kind)
    api._message_route = routing.OwnedShellMessageRoute()
    api._message_route.register(shell)
    api._geometry_change_pending = False
    api._peek_message = queue.peek
    api._translate_message = lambda *_: 1
    api._dispatch_message = queue.dispatch
    api._destroy_window = lambda *_: 1
    api._pointer_events = deque()
    api._pointer_time_fence = None
    api._capture_active = False
    api._is_child = lambda parent, child: int(queue.children.get(child.value) == parent.value)
    api._map_window_points = lambda *_: 0
    api._set_capture = queue.set_capture
    api._get_capture = lambda: queue.captured
    api._release_capture = queue.release_capture
    api._catalog_commands = deque()
    api._catalog_rejections = 0
    api._button_handles = {_SAVE_BUTTON_ID: shell + 200}
    api._ui_handles = {shell + 200}
    api._view_editor = 0
    api._read_view_id = lambda: 4
    return api


@pytest.mark.parametrize("first_kind", _KINDS)
@pytest.mark.parametrize("second_kind", _KINDS)
def test_two_pumps_preserve_owner_children_foreign_messages_and_capacity(first_kind, second_kind):
    queue = Queue([(72, 0x0005), (999, _MOVE), (171, _MOVE), (172, _MOVE), (71, _CLOSE)])
    a, b = api_for(first_kind, 71, queue), api_for(second_kind, 72, queue)
    assert a.pump_messages(71, 64) == (2, True)
    assert tuple(queue.messages) == ((72, 0x0005), (999, _MOVE), (172, _MOVE))
    assert b.pump_messages(72, 64) == (2, False)
    assert tuple(queue.messages) == ((999, _MOVE),)
    assert b.take_geometry_change()
    assert not a.take_geometry_change()
    assert not any(hwnd == 999 for hwnd, _ in queue.dispatched)
    assert set(queue.filters) == {71, 72}
    assert len(routing._ROUTES.active) == 2


@pytest.mark.parametrize("kind", _KINDS)
def test_each_owner_has_independent_bounded_fifo_work(kind):
    queue = Queue([(71, 0x0400 + i) for i in range(10)] + [(72, _CLOSE), (999, _CLOSE)])
    a, b = api_for(kind, 71, queue), api_for(kind, 72, queue)
    assert a.pump_messages(71, 2) == (2, False)
    assert queue.dispatched == [(71, 0x0400), (71, 0x0401)]
    assert b.pump_messages(72, 2) == (1, True)
    assert (999, _CLOSE) in queue.messages
    assert len(queue.messages) == 9


@pytest.mark.parametrize("kind", _KINDS)
def test_one_quit_fans_out_to_all_owned_shells_with_empty_peer_queue(kind):
    queue = Queue([(0, _QUIT)])
    a, b = api_for(kind, 71, queue), api_for(kind, 72, queue)
    assert a.pump_messages(71, 64) == (1, True)
    assert b.pump_messages(72, 64) == (0, True)
    assert not queue.messages and not queue.dispatched
    with pytest.raises(routing.WindowsOperatorMessageRoutingError):
        routing.OwnedShellMessageRoute().register(73)
    a.destroy_shell(71)
    assert b.pump_messages(72, 64) == (0, True)
    b.destroy_shell(72)
    c = api_for(kind, 71, queue)
    assert c.pump_messages(71, 64) == (0, False)


def test_quit_registry_is_thread_local_and_registration_identity_is_not_reused():
    route = routing.OwnedShellMessageRoute()
    route.register(71)
    failures = []
    results = []

    def another_thread():
        try:
            other = routing.OwnedShellMessageRoute()
            other.register(71)
            other.observe_quit(71)
            results.append(other.quit_requested(71))
            other.unregister(71)
            with pytest.raises(routing.WindowsOperatorMessageRoutingError):
                route.unregister(71)
        except BaseException as exc:
            failures.append(exc)

    worker = threading.Thread(target=another_thread)
    worker.start()
    worker.join(timeout=2)
    assert not worker.is_alive() and not failures and results == [True]
    assert not route.quit_requested(71)
    route.unregister(71)
    new = routing.OwnedShellMessageRoute()
    new.register(71)
    with pytest.raises(routing.WindowsOperatorMessageRoutingError):
        route.require(71)
    assert not new.quit_requested(71)


@pytest.mark.parametrize("method", ["pump", "destroy"])
def test_wrong_thread_calls_cannot_poll_destroy_or_release_registration(method):
    queue = Queue()
    api = api_for(_Win32OperatorShellApi, 71, queue)
    native_calls = []
    api._destroy_window = lambda *_: native_calls.append("destroy") or 1
    failures = []

    def another_thread():
        try:
            with pytest.raises(_NativeShellError) as error:
                api.pump_messages(71, 1) if method == "pump" else api.destroy_shell(71)
            assert error.value.failure == (
                _NativeShellFailure.PUMP if method == "pump" else _NativeShellFailure.DESTROY
            )
        except BaseException as exc:
            failures.append(exc)

    worker = threading.Thread(target=another_thread)
    worker.start()
    worker.join(timeout=2)
    assert not worker.is_alive() and not failures
    assert not native_calls and not queue.filters
    api._message_route.require(71)
    api.destroy_shell(71)
    assert native_calls == ["destroy"] and not routing._ROUTES.active


@pytest.mark.parametrize("kind", _KINDS)
def test_sent_callback_for_other_shell_runs_even_when_owned_queue_is_empty(kind):
    queue = Queue()
    a, b = api_for(kind, 71, queue), api_for(kind, 72, queue)
    callbacks = []
    queue.callbacks.append(lambda: callbacks.append(72))
    assert a.pump_messages(71, 1) == (0, False)
    assert callbacks == [72]
    assert b.pump_messages(72, 1) == (0, False)
    assert not queue.dispatched


@pytest.mark.parametrize("kind", _KINDS)
def test_sent_callback_quit_is_seen_after_empty_peek(kind):
    queue = Queue()
    a, b = api_for(kind, 71, queue), api_for(kind, 72, queue)
    queue.callbacks.append(lambda: b._message_route.observe_quit(72))
    assert a.pump_messages(71, 1) == (0, True)
    assert b.pump_messages(72, 1) == (0, True)


@pytest.mark.parametrize("kind", _KINDS)
def test_sent_callback_release_and_hwnd_reuse_fail_closed_before_dispatch(kind):
    queue = Queue([(71, _MOVE)])
    api = api_for(kind, 71, queue)
    replacements = []

    def replace_registration():
        api._message_route.unregister(71)
        new = routing.OwnedShellMessageRoute()
        new.register(71)
        replacements.append(new)

    queue.callbacks.append(replace_registration)
    with pytest.raises(_NativeShellError) as error:
        api.pump_messages(71, 1)
    assert error.value.failure == _NativeShellFailure.PUMP
    assert not queue.dispatched and not api._pointer_events
    replacements[0].require(71)


@pytest.mark.parametrize("kind", [_InteractiveWin32OperatorShellApi, _CatalogWin32OperatorShellApi])
def test_stale_capture_state_and_sent_callback_never_release_foreign_capture(kind):
    queue = Queue([(71, _CLOSE)])
    api = api_for(kind, 71, queue)
    api._capture_active = True
    queue.captured = 71
    queue.callbacks.append(lambda: setattr(queue, "captured", 999))
    assert api.pump_messages(71, 1) == (1, True)
    assert queue.captured == 999 and not queue.releases and not queue.acquisitions
    assert not api._capture_active


def test_foreign_capture_is_neither_stolen_nor_released_but_owner_release_remains_valid():
    queue = Queue()
    api = api_for(_InteractiveWin32OperatorShellApi, 71, queue)
    queue.captured = 72
    with pytest.raises(_NativeShellError):
        api._acquire_pointer_capture(71)
    assert queue.captured == 72 and not queue.acquisitions
    api._capture_active = True
    api._release_pointer_capture(71)
    assert queue.captured == 72 and not queue.releases
    queue.captured = 0
    api._acquire_pointer_capture(71)
    api._release_pointer_capture(71)
    assert queue.acquisitions == [71] and queue.releases == [71]


def test_catalog_command_requires_shell_target_and_exact_button_membership():
    queue = Queue(
        [
            (72, 0x0111, _SAVE_BUTTON_ID, 272),
            (171, 0x0111, _SAVE_BUTTON_ID, 271),
            (71, 0x0111, _SAVE_BUTTON_ID, 272),
            (71, 0x0111, _SAVE_BUTTON_ID, 271),
        ]
    )
    a, b = (
        api_for(_CatalogWin32OperatorShellApi, 71, queue),
        api_for(_CatalogWin32OperatorShellApi, 72, queue),
    )
    assert a.pump_messages(71, 64) == (3, False)
    assert len(a.drain_catalog_commands(16)) == 1
    assert len(b.drain_catalog_commands(16)) == 0
    assert b.pump_messages(72, 64) == (1, False)
    assert len(b.drain_catalog_commands(16)) == 1


def test_registry_rejects_invalid_duplicate_and_excess_registration():
    route = routing.OwnedShellMessageRoute()
    for invalid in (0, -1, True, None, "71"):
        with pytest.raises(routing.WindowsOperatorMessageRoutingError):
            route.register(invalid)
    route.register(71)
    with pytest.raises(routing.WindowsOperatorMessageRoutingError):
        route.register(72)
    with pytest.raises(routing.WindowsOperatorMessageRoutingError):
        routing.OwnedShellMessageRoute().register(71)
    for index in range(routing._MAX_OWNED_SHELLS - 1):
        routing.OwnedShellMessageRoute().register(1000 + index)
    with pytest.raises(routing.WindowsOperatorMessageRoutingError):
        routing.OwnedShellMessageRoute().register(99)


def creating_api():
    api = object.__new__(_Win32OperatorShellApi)
    api._message_route = routing.OwnedShellMessageRoute()
    api._get_module_handle = lambda _: 1
    api._create_window = lambda *_: 71
    api._destroy_window = lambda *_: 1
    return api


def test_create_registers_once_and_duplicate_create_does_not_allocate():
    api = creating_api()
    assert api.create_shell(640, 480) == 71
    api._create_window = lambda *_: pytest.fail("duplicate native creation")
    with pytest.raises(_NativeShellError) as error:
        api.create_shell(640, 480)
    assert error.value.failure == _NativeShellFailure.CREATE
    api.destroy_shell(71)
    assert not routing._ROUTES.active


@pytest.mark.parametrize("failure", [0, RuntimeError("private native failure")])
def test_failed_native_creation_never_registers(failure):
    api = creating_api()

    def create(*_):
        if isinstance(failure, Exception):
            raise failure
        return failure

    api._create_window = create
    with pytest.raises(_NativeShellError) as error:
        api.create_shell(640, 480)
    assert error.value.failure == _NativeShellFailure.CREATE
    assert not routing._ROUTES.active


def test_sent_quit_during_create_rolls_back_new_window_without_registering():
    old = routing.OwnedShellMessageRoute()
    old.register(72)
    api = creating_api()
    destroyed = []
    api._create_window = lambda *_: old.observe_quit(72) or 71
    api._destroy_window = lambda hwnd: destroyed.append(hwnd.value) or 1
    with pytest.raises(_NativeShellError):
        api.create_shell(640, 480)
    assert destroyed == [71] and tuple(routing._ROUTES.active) == (72,)


@pytest.mark.parametrize("failure", [0, RuntimeError("private native failure")])
def test_destroy_failure_remains_failure_but_removes_routing_registration(failure):
    api = api_for(_Win32OperatorShellApi, 71, Queue())

    def destroy(*_):
        if isinstance(failure, Exception):
            raise failure
        return failure

    api._destroy_window = destroy
    with pytest.raises(_NativeShellError) as error:
        api.destroy_shell(71)
    assert error.value.failure == _NativeShellFailure.DESTROY
    assert not routing._ROUTES.active


@pytest.mark.parametrize("kind", _KINDS)
def test_sent_callback_same_route_reuse_cannot_validate_stale_poll(kind):
    queue = Queue([(71, _MOVE)])
    api = api_for(kind, 71, queue)

    def reuse():
        api._message_route.unregister(71)
        api._message_route.register(71)

    queue.callbacks.append(reuse)
    with pytest.raises(_NativeShellError):
        api.pump_messages(71, 1)
    assert not queue.dispatched and not api._pointer_events
    api._message_route.require(71)


@pytest.mark.parametrize("kind", _KINDS)
def test_quit_removed_after_sent_callback_retires_poller_still_reaches_peer(kind):
    queue = Queue([(0, _QUIT)])
    a, b = api_for(kind, 71, queue), api_for(kind, 72, queue)
    queue.callbacks.append(lambda: a._message_route.unregister(71))
    with pytest.raises(_NativeShellError):
        a.pump_messages(71, 1)
    assert not queue.messages
    assert b.pump_messages(72, 1) == (0, True)


def test_wrong_thread_creation_is_rejected_before_native_allocation():
    api = creating_api()
    allocations = []
    api._create_window = lambda *_: allocations.append(71) or 71
    failures = []

    def another_thread():
        try:
            with pytest.raises(_NativeShellError) as error:
                api.create_shell(640, 480)
            assert error.value.failure == _NativeShellFailure.CREATE
        except BaseException as exc:
            failures.append(exc)

    worker = threading.Thread(target=another_thread)
    worker.start()
    worker.join(timeout=2)
    assert not worker.is_alive() and not failures
    assert not allocations and not routing._ROUTES.active


def test_partial_catalog_creation_releases_its_registration_and_shell():
    api = object.__new__(_CatalogWin32OperatorShellApi)
    api._message_route = routing.OwnedShellMessageRoute()
    api._get_module_handle = lambda _: 1
    allocations = []
    destroyed = []
    api._create_window = lambda *_: allocations.append(71) or (71 if len(allocations) == 1 else 0)
    api._destroy_window = lambda hwnd: destroyed.append(hwnd.value) or 1
    api._ui_handles = set()
    api._button_handles = {}
    api._view_editor = 0
    with pytest.raises(_NativeShellError) as error:
        api.create_shell(640, 480)
    assert error.value.failure == _NativeShellFailure.CREATE
    assert destroyed == [71] and not routing._ROUTES.active
    assert not api._ui_handles and not api._button_handles


@pytest.mark.parametrize("reuse", [False, True])
def test_destroy_callback_registration_change_is_sanitized_and_preserves_new_owner(reuse):
    api = api_for(_Win32OperatorShellApi, 71, Queue())

    def destroy(_):
        api._message_route.unregister(71)
        if reuse:
            api._message_route.register(71)
        return 1

    api._destroy_window = destroy
    with pytest.raises(_NativeShellError) as error:
        api.destroy_shell(71)
    assert error.value.failure == _NativeShellFailure.DESTROY
    if reuse:
        api._message_route.require(71)
    else:
        assert not routing._ROUTES.active


def test_catalog_wrong_owner_destruction_does_not_mutate_live_commands_or_handles():
    api = api_for(_CatalogWin32OperatorShellApi, 71, Queue())
    api._catalog_commands.append("sentinel")
    with pytest.raises(_NativeShellError):
        api.destroy_shell(72)
    assert list(api._catalog_commands) == ["sentinel"]
    assert api._ui_handles == {271} and api._button_handles == {_SAVE_BUTTON_ID: 271}
    api._message_route.require(71)


@pytest.mark.parametrize("kind", _KINDS)
@pytest.mark.parametrize("batch", [1, 2])
def test_dispatch_cannot_replace_pump_generation_even_on_final_batch_item(kind, batch):
    queue = Queue([(71, 0x0400), (71, 0x0401)])
    api = api_for(kind, 71, queue)

    def dispatch(pointer):
        queue.dispatch(pointer)
        api._message_route.unregister(71)
        api._message_route.register(71)
        return 0

    api._dispatch_message = dispatch
    with pytest.raises(_NativeShellError) as error:
        api.pump_messages(71, batch)
    assert error.value.failure == _NativeShellFailure.PUMP
    assert queue.dispatched == [(71, 0x0400)]
    assert tuple(queue.messages) == ((71, 0x0401),)
    api._message_route.require(71)
