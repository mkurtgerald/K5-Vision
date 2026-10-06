"""Pure subclass fakes: no Win32 library, native callback, or message execution."""

from __future__ import annotations

import gc
import threading
import weakref
from types import SimpleNamespace

import pytest

from k5vision.media import windows_operator_message_routing as routing
from k5vision.media import windows_operator_window_procedure as procedure

_SHELL = 71
_PREVIOUS = 9876
_CLOSE = 0x0010
_COMMAND = 0x0111
_NCDESTROY = 0x0082
_PAINT = 0x000F


class Function:
    """Python callable with the same configurable signature fields as ctypes."""

    def __init__(self, target):
        self.target = target

    def __call__(self, *args):
        return self.target(*args)


class Callback:
    def __init__(self, handler):
        self.handler = handler

    def __call__(self, *args):
        return self.handler(*args)


class User32:
    def __init__(self):
        self.current = _PREVIOUS
        self.callbacks = {}
        self.set_calls = []
        self.get_calls = []
        self.forwarded = []
        self.set_hook = None
        self.forward_hook = None
        self.forward_result = 123
        self.fail_set = False
        self.raise_set = False
        self.GetWindowLongPtrW = self.GetWindowLongW = Function(self.get)
        self.SetWindowLongPtrW = self.SetWindowLongW = Function(self.set)
        self.CallWindowProcW = Function(self.forward)

    def callback(self, handler):
        callback = Callback(handler)
        address = 0x1000 + len(self.callbacks)
        # Real user32 stores only the address, never a Python strong reference.
        self.callbacks[address] = weakref.ref(callback)
        return callback, address

    def get(self, hwnd, index):
        self.get_calls.append((hwnd.value, index))
        return self.current

    def set(self, hwnd, index, value):
        self.set_calls.append((hwnd.value, index, value))
        if self.raise_set:
            raise RuntimeError("private native details")
        if self.fail_set:
            return 0
        prior, self.current = self.current, value
        if self.set_hook:
            hook, self.set_hook = self.set_hook, None
            hook()
        return prior

    def forward(self, prior, hwnd, message, wparam, lparam):
        self.forwarded.append((prior.value, hwnd.value, message, wparam, lparam))
        if self.forward_hook:
            self.forward_hook()
        return self.forward_result

    def send(self, message, wparam=0, lparam=0, *, hwnd=_SHELL, address=None):
        callback = self.callbacks[address or self.current]()
        assert callback is not None, "native callback address must remain alive"
        return callback(hwnd, message, wparam, lparam)


@pytest.fixture(autouse=True)
def isolated_boundary(monkeypatch):
    monkeypatch.setattr(routing, "_ROUTES", routing._ThreadRoutes())
    monkeypatch.setattr(procedure, "_PINNED_PROCEDURES", set())


@pytest.fixture
def setup_boundary(monkeypatch):
    user32 = User32()
    monkeypatch.setattr(procedure, "_make_callback", user32.callback)
    route = routing.OwnedShellMessageRoute()
    route.register(_SHELL)

    def create(handler=lambda *_: None):
        boundary = procedure.OwnedWindowProcedure(user32, _SHELL, route, handler)
        return boundary

    return user32, route, create


def test_construction_is_native_free_and_install_has_pointer_sized_signature(setup_boundary):
    user32, route, create = setup_boundary
    boundary = create()
    assert not user32.get_calls and not user32.set_calls and not user32.callbacks
    assert not boundary.installed and not boundary.failed
    boundary.install()
    assert boundary.installed and boundary in procedure._PINNED_PROCEDURES
    assert user32.get_calls == [(_SHELL, -4)]
    assert user32.set_calls == [(_SHELL, -4, user32.current)]
    assert user32.SetWindowLongPtrW.restype is procedure.ctypes.c_ssize_t
    assert user32.CallWindowProcW.restype is procedure.ctypes.c_ssize_t
    route.require(_SHELL)


def test_callback_factory_uses_stdcall_and_pointer_width_without_loading_native(monkeypatch):
    captured = []
    callback = object()

    def factory(*signature):
        captured.append(signature)

        def wrap(handler):
            captured.append(handler)
            return callback

        return wrap

    monkeypatch.setattr(procedure.ctypes, "WINFUNCTYPE", factory, raising=False)
    monkeypatch.setattr(
        procedure.ctypes, "cast", lambda value, kind: SimpleNamespace(value=0x12345678)
    )

    def handler(*_):
        return 0

    assert procedure._make_callback(handler) == (callback, 0x12345678)
    assert captured == [
        (
            procedure.ctypes.c_ssize_t,
            procedure.ctypes.c_void_p,
            procedure.ctypes.c_uint32,
            procedure.ctypes.c_size_t,
            procedure.ctypes.c_ssize_t,
        ),
        handler,
    ]


def test_unhandled_messages_forward_parameters_and_result_to_original_procedure(setup_boundary):
    user32, _, create = setup_boundary
    calls = []
    boundary = create(lambda *args: calls.append(args))
    boundary.install()
    assert user32.send(_PAINT, 2**40, -(2**35)) == 123
    assert calls == [(_SHELL, _PAINT, 2**40, -(2**35))]
    assert user32.forwarded == [(_PREVIOUS, _SHELL, _PAINT, 2**40, -(2**35))]
    assert not boundary.failed


def test_sent_close_and_command_are_consumed_on_creator_thread_without_queue(setup_boundary):
    user32, _, create = setup_boundary
    received = []

    def handler(hwnd, message, wparam, lparam):
        received.append((threading.current_thread(), hwnd, message, wparam, lparam))
        return 0 if message in {_CLOSE, _COMMAND} else None

    boundary = create(handler)
    boundary.install()
    assert user32.send(_COMMAND, 42, 700) == 0
    assert user32.send(_CLOSE) == 0
    assert [entry[2:] for entry in received] == [(_COMMAND, 42, 700), (_CLOSE, 0, 0)]
    assert all(entry[0] is threading.current_thread() for entry in received)
    assert not user32.forwarded and not boundary.destroyed


def test_synchronous_callback_during_install_is_pinned_and_has_predecessor(setup_boundary):
    user32, _, create = setup_boundary
    boundary = create()
    observed = []

    def during_install():
        observed.append(boundary in procedure._PINNED_PROCEDURES)
        observed.append(user32.send(_PAINT))

    user32.set_hook = during_install
    boundary.install()
    assert observed == [True, 123]


def test_nested_sent_callbacks_do_not_overwrite_outer_result(setup_boundary):
    user32, _, create = setup_boundary
    received = []

    def handler(hwnd, message, wparam, lparam):
        received.append(message)
        if message == _COMMAND:
            assert user32.send(_CLOSE) == 0
            return 9
        return 0

    boundary = create(handler)
    boundary.install()
    assert user32.send(_COMMAND) == 9
    assert received == [_COMMAND, _CLOSE]
    assert not boundary.failed


@pytest.mark.parametrize("error", [RuntimeError, KeyboardInterrupt, SystemExit])
def test_no_handler_exception_can_escape_native_callback(setup_boundary, error):
    user32, _, create = setup_boundary

    def handler(*_):
        raise error("private callback details")

    boundary = create(handler)
    boundary.install()
    assert user32.send(_COMMAND) == 0
    assert boundary.failed and not user32.forwarded
    assert user32.send(_CLOSE) == 0
    assert not user32.forwarded
    boundary.detach()
    assert boundary.failed and not boundary.installed


@pytest.mark.parametrize("result", [True, "0", 1 << 128, -(1 << 128)])
def test_invalid_lresult_is_latched_before_ctypes_conversion(setup_boundary, result):
    user32, _, create = setup_boundary
    boundary = create(lambda *_: result)
    boundary.install()
    assert user32.send(_COMMAND) == 0
    assert boundary.failed and not user32.forwarded


def test_native_forwarding_exception_is_latched(setup_boundary):
    user32, _, create = setup_boundary
    boundary = create()
    boundary.install()

    def fail_forward():
        raise RuntimeError("private native details")

    user32.forward_hook = fail_forward
    assert user32.send(_PAINT) == 0
    assert boundary.failed


def test_wrong_hwnd_is_neither_handled_nor_forwarded(setup_boundary):
    user32, _, create = setup_boundary
    received = []
    boundary = create(lambda *args: received.append(args))
    boundary.install()
    assert user32.send(_COMMAND, hwnd=72) == 0
    assert boundary.failed and not received and not user32.forwarded


@pytest.mark.parametrize("action", ["install", "detach", "callback"])
def test_wrong_thread_cannot_install_detach_handle_or_forward(setup_boundary, action):
    user32, _, create = setup_boundary
    received = []
    boundary = create(lambda *args: received.append(args))
    if action != "install":
        boundary.install()
    before = len(user32.get_calls), len(user32.set_calls)
    failures = []

    def run():
        try:
            if action == "callback":
                assert user32.send(_COMMAND) == 0
            else:
                with pytest.raises(procedure.WindowsOperatorWindowProcedureError):
                    getattr(boundary, action)()
        except BaseException as error:
            failures.append(error)

    worker = threading.Thread(target=run)
    worker.start()
    worker.join(timeout=2)
    assert not worker.is_alive() and not failures
    assert (len(user32.get_calls), len(user32.set_calls)) == before
    assert boundary.failed and not received and not user32.forwarded


@pytest.mark.parametrize("reuse_same_route", [False, True])
def test_stale_registration_cannot_handle_forward_or_restore_reused_hwnd(
    setup_boundary, reuse_same_route
):
    user32, route, create = setup_boundary
    received = []
    boundary = create(lambda *args: received.append(args))
    boundary.install()
    route.unregister(_SHELL)
    replacement = route if reuse_same_route else routing.OwnedShellMessageRoute()
    replacement.register(_SHELL)
    before = len(user32.get_calls), len(user32.set_calls)
    assert user32.send(_COMMAND) == 0
    with pytest.raises(procedure.WindowsOperatorWindowProcedureError):
        boundary.detach()
    assert not received and not user32.forwarded and boundary.failed
    assert (len(user32.get_calls), len(user32.set_calls)) == before
    assert boundary in procedure._PINNED_PROCEDURES
    replacement.require(_SHELL)


def test_handler_replacing_registration_cannot_forward_outer_message(setup_boundary):
    user32, route, create = setup_boundary

    def replace(*_):
        route.unregister(_SHELL)
        route.register(_SHELL)

    boundary = create(replace)
    boundary.install()
    assert user32.send(_PAINT) == 0
    assert boundary.failed and not user32.forwarded


def test_terminal_destroy_forwards_then_retires_route_but_keeps_pin_until_detach(setup_boundary):
    user32, route, create = setup_boundary
    received = []
    boundary = create(lambda *args: received.append(args))
    boundary.install()
    address = user32.current
    assert user32.send(_NCDESTROY) == 123
    assert user32.forwarded == [(_PREVIOUS, _SHELL, _NCDESTROY, 0, 0)]
    assert boundary.destroyed and not boundary.installed and not boundary.failed
    assert not received and boundary in procedure._PINNED_PROCEDURES
    with pytest.raises(routing.WindowsOperatorMessageRoutingError):
        route.require(_SHELL)
    replacement = routing.OwnedShellMessageRoute()
    replacement.register(_SHELL)
    before = len(user32.get_calls), len(user32.set_calls)
    boundary.detach()
    assert (len(user32.get_calls), len(user32.set_calls)) == before
    assert boundary not in procedure._PINNED_PROCEDURES
    assert user32.send(_COMMAND, address=address) == 0
    replacement.require(_SHELL)


def test_terminal_destroy_retires_even_when_predecessor_raises(setup_boundary):
    user32, route, create = setup_boundary
    boundary = create()
    boundary.install()

    def fail_forward():
        raise SystemExit("private native details")

    user32.forward_hook = fail_forward
    assert user32.send(_NCDESTROY) == 0
    assert boundary.destroyed and boundary.failed
    with pytest.raises(routing.WindowsOperatorMessageRoutingError):
        route.require(_SHELL)
    boundary.detach()
    assert boundary not in procedure._PINNED_PROCEDURES


def test_terminal_destroy_never_retires_registration_reused_by_predecessor(setup_boundary):
    user32, route, create = setup_boundary
    boundary = create()
    boundary.install()

    def reuse():
        route.unregister(_SHELL)
        route.register(_SHELL)

    user32.forward_hook = reuse
    assert user32.send(_NCDESTROY) == 123
    assert boundary.destroyed and boundary.failed
    route.require(_SHELL)


def test_detach_restores_original_without_retiring_shell_and_is_idempotent(setup_boundary):
    user32, route, create = setup_boundary
    boundary = create()
    boundary.install()
    address = user32.current
    boundary.detach()
    assert user32.current == _PREVIOUS
    assert user32.set_calls[-1] == (_SHELL, -4, _PREVIOUS)
    assert not boundary.installed and boundary not in procedure._PINNED_PROCEDURES
    route.require(_SHELL)
    before = len(user32.get_calls), len(user32.set_calls)
    boundary.detach()
    assert (len(user32.get_calls), len(user32.set_calls)) == before
    assert user32.send(_COMMAND, address=address) == 0
    assert not user32.forwarded


@pytest.mark.parametrize("mode", ["zero", "exception", "other_subclass"])
def test_restoration_failure_keeps_callback_alive_after_owner_drops_it(setup_boundary, mode):
    user32, _, create = setup_boundary
    boundary = create()
    boundary.install()
    address = user32.current
    if mode == "zero":
        user32.fail_set = True
    elif mode == "exception":
        user32.raise_set = True
    else:
        user32.current = 5555
    before = len(user32.set_calls)
    with pytest.raises(procedure.WindowsOperatorWindowProcedureError) as error:
        boundary.detach()
    assert str(error.value) == "operator shell window procedure failed"
    if mode == "other_subclass":
        assert len(user32.set_calls) == before and user32.current == 5555
    assert boundary.failed and boundary in procedure._PINNED_PROCEDURES
    reference = weakref.ref(boundary)
    del boundary
    gc.collect()
    assert reference() is not None and user32.callbacks[address]() is not None
    assert user32.send(_COMMAND, address=address) == 0


def test_failed_install_can_detach_when_native_procedure_was_not_changed(setup_boundary):
    user32, route, create = setup_boundary
    boundary = create()
    user32.fail_set = True
    with pytest.raises(procedure.WindowsOperatorWindowProcedureError):
        boundary.install()
    assert boundary.failed and boundary in procedure._PINNED_PROCEDURES
    boundary.detach()
    assert boundary not in procedure._PINNED_PROCEDURES
    assert user32.current == _PREVIOUS
    route.require(_SHELL)


def test_null_predecessor_never_installs_or_pins_callback(setup_boundary):
    user32, _, create = setup_boundary
    boundary = create()
    user32.current = 0
    with pytest.raises(procedure.WindowsOperatorWindowProcedureError):
        boundary.install()
    assert boundary.failed and not boundary.installed
    assert not user32.set_calls and not user32.callbacks
    assert boundary not in procedure._PINNED_PROCEDURES


def test_install_reentrant_destruction_is_sanitized_and_terminal_pin_can_be_released(
    setup_boundary,
):
    user32, _, create = setup_boundary
    boundary = create()
    user32.set_hook = lambda: user32.send(_NCDESTROY)
    with pytest.raises(procedure.WindowsOperatorWindowProcedureError):
        boundary.install()
    assert boundary.destroyed and boundary.failed
    assert boundary in procedure._PINNED_PROCEDURES
    before = len(user32.get_calls), len(user32.set_calls)
    boundary.detach()
    assert (len(user32.get_calls), len(user32.set_calls)) == before
    assert boundary not in procedure._PINNED_PROCEDURES


def test_detach_from_inside_handler_fails_safely_without_native_restoration(setup_boundary):
    user32, _, create = setup_boundary
    boundary = create(lambda *_: boundary.detach())
    boundary.install()
    before = len(user32.set_calls)
    assert user32.send(_COMMAND) == 0
    assert boundary.failed and boundary in procedure._PINNED_PROCEDURES
    assert len(user32.set_calls) == before
    boundary.detach()
    assert user32.current == _PREVIOUS


def test_duplicate_install_cannot_replace_another_lifetime(setup_boundary):
    user32, _, create = setup_boundary
    boundary = create()
    boundary.install()
    before = len(user32.set_calls)
    with pytest.raises(procedure.WindowsOperatorWindowProcedureError):
        boundary.install()
    assert len(user32.set_calls) == before
    boundary.detach()
    with pytest.raises(procedure.WindowsOperatorWindowProcedureError):
        boundary.install()
    assert user32.current == _PREVIOUS
