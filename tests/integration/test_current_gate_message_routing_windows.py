"""Generated queued-routing witness, selected by the existing application gate.

Only disposable HWNDs on the calling thread are used. Inert WM_APP messages are
posted to owned shells, their children, and one owned non-operator sentinel.
This is not a native callback, button-activation, WM_CLOSE, or user-input witness.
No screen capture, pointer capture, keyboard injection, or external input is used.
"""

from __future__ import annotations

import ctypes
import sys
import threading

_MARKER = 0x8000 + 0x325
_WM_QUIT = 0x0012
_PM_NOREMOVE = 0
_PM_REMOVE = 1
_PUMP_BOUND = 2
_MAX_CYCLES = 32
_COOKIE = 1 << 40
_SIGNED_COOKIE = -(1 << 40)


def run_owned_queued_message_routing(shell_kind: str) -> dict[str, object]:
    """Exercise real create/pump/destroy APIs; assert outcomes after owner cleanup."""
    if sys.platform != "win32":
        raise RuntimeError("queued routing qualification requires Windows")

    from k5vision.media.windows_operator_application import (
        _Win32Message,
        _Win32OperatorShellApi,
    )
    from k5vision.media.windows_operator_catalog_ui import _CatalogWin32OperatorShellApi
    from k5vision.media.windows_operator_interaction import _InteractiveWin32OperatorShellApi

    factory = {
        "base": _Win32OperatorShellApi,
        "interactive": _InteractiveWin32OperatorShellApi,
        "catalog": _CatalogWin32OperatorShellApi,
    }[shell_kind]
    apis = {"a": factory(), "b": factory()}
    owner_thread = threading.get_ident()
    user32, kernel32 = apis["a"]._user32, apis["a"]._kernel32
    message_pointer = ctypes.POINTER(_Win32Message)

    # Assert the actual production boundary ABI before creating any resources.
    # The 64-bit cookies below additionally witness WPARAM/LPARAM preservation.
    assert ctypes.sizeof(ctypes.c_void_p) == 8, "this qualification requires Windows x64"
    assert ctypes.sizeof(_Win32Message) == 48
    assert (_Win32Message.wParam.offset, _Win32Message.lParam.offset) == (16, 24)
    create_signature = [
        ctypes.c_uint32,
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        *([ctypes.c_int] * 4),
        *([ctypes.c_void_p] * 4),
    ]
    peek_signature = [
        message_pointer,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
    ]
    for api in apis.values():
        for function, arguments, result in (
            (api._create_window, create_signature, ctypes.c_void_p),
            (api._get_module_handle, [ctypes.c_wchar_p], ctypes.c_void_p),
            (api._peek_message, peek_signature, ctypes.c_int),
            (api._translate_message, [message_pointer], ctypes.c_int),
            (api._dispatch_message, [message_pointer], ctypes.c_ssize_t),
            (api._destroy_window, [ctypes.c_void_p], ctypes.c_int),
        ):
            assert function.argtypes == arguments
            assert function.restype is result

    def bind(library, name, arguments, result):
        function = getattr(library, name)
        function.argtypes, function.restype = arguments, result
        return function

    post_message = bind(
        user32,
        "PostMessageW",
        [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_size_t, ctypes.c_ssize_t],
        ctypes.c_int,
    )
    post_quit = bind(user32, "PostQuitMessage", [ctypes.c_int], None)
    is_window = bind(user32, "IsWindow", [ctypes.c_void_p], ctypes.c_int)
    is_child = bind(user32, "IsChild", [ctypes.c_void_p, ctypes.c_void_p], ctypes.c_int)
    get_parent = bind(user32, "GetParent", [ctypes.c_void_p], ctypes.c_void_p)
    window_thread = bind(
        user32,
        "GetWindowThreadProcessId",
        [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)],
        ctypes.c_uint32,
    )
    get_thread = bind(kernel32, "GetCurrentThreadId", [], ctypes.c_uint32)
    show_window = bind(user32, "ShowWindow", [ctypes.c_void_p, ctypes.c_int], ctypes.c_int)
    raw_peek = apis["a"]._peek_message
    raw_destroy = apis["a"]._destroy_window
    native_thread = int(get_thread())
    shells: dict[str, int] = {}
    extra_windows: list[int] = []
    all_windows: list[int] = []
    consumed: dict[str, list[tuple[int, int, int, int]]] = {"a": [], "b": []}
    dispatched: dict[str, list[tuple[int, int, int, int]]] = {"a": [], "b": []}
    filters: dict[str, list[int]] = {"a": [], "b": []}
    pump_results: dict[str, list[tuple[int, bool]]] = {"a": [], "b": []}
    cleanup_errors: list[str] = []
    cleanup_markers: list[tuple[int, int, int, int]] = []
    quit_posted = False
    sentinel = 0

    def same_thread() -> None:
        if threading.get_ident() != owner_thread or int(get_thread()) != native_thread:
            raise RuntimeError("queued-routing witness changed owner thread")

    def require_owned(hwnd: int) -> None:
        same_thread()
        if (
            hwnd not in all_windows
            or not is_window(ctypes.c_void_p(hwnd))
            or int(window_thread(ctypes.c_void_p(hwnd), None)) != native_thread
        ):
            raise RuntimeError("queued-routing witness refused a non-owned HWND")

    def event(message) -> tuple[int, int, int, int]:
        return (
            int(message.hwnd or 0),
            int(message.message),
            int(message.wParam),
            int(message.lParam),
        )

    def observe(api, name: str) -> None:
        native_peek, native_dispatch = api._peek_message, api._dispatch_message

        def peek(pointer, hwnd, minimum, maximum, flags):
            same_thread()
            filters[name].append(int(getattr(hwnd, "value", hwnd) or 0))
            result = native_peek(pointer, hwnd, minimum, maximum, flags)
            if result:
                consumed[name].append(event(ctypes.cast(pointer, message_pointer).contents))
            return result

        def dispatch(pointer):
            same_thread()
            dispatched[name].append(event(ctypes.cast(pointer, message_pointer).contents))
            return native_dispatch(pointer)

        # Observe real native calls. Do not replace the product's selection,
        # message queue, translation, dispatch, or ownership implementation.
        api._peek_message, api._dispatch_message = peek, dispatch

    def create_extra(parent: int = 0) -> int:
        same_thread()
        if parent:
            require_owned(parent)
        hwnd = int(
            apis["a"]._create_window(
                0,
                "STATIC",
                "K5 generated routing witness",
                0x40000000 if parent else 0,  # Hidden child or hidden top-level sentinel.
                7,
                9,
                24,
                24,
                ctypes.c_void_p(parent) if parent else None,
                None,
                apis["a"]._get_module_handle(None),
                None,
            )
            or 0
        )
        if not hwnd:
            raise RuntimeError("could not create owned routing witness window")
        # Register for cleanup before doing any potentially failing validation.
        extra_windows.append(hwnd)
        all_windows.append(hwnd)
        require_owned(hwnd)
        if int(get_parent(ctypes.c_void_p(hwnd)) or 0) != parent:
            raise RuntimeError("generated routing witness parent differs")
        return hwnd

    def marker(hwnd: int, token: int) -> tuple[int, int, int, int]:
        return hwnd, _MARKER, _COOKIE + token, _SIGNED_COOKIE - token

    def post(hwnd: int, token: int) -> tuple[int, int, int, int]:
        require_owned(hwnd)
        value = marker(hwnd, token)
        if not post_message(ctypes.c_void_p(hwnd), *value[1:]):
            raise RuntimeError("could not post generated routing marker")
        return value

    def inspect_marker(hwnd: int):
        require_owned(hwnd)
        message = _Win32Message()
        if raw_peek(ctypes.byref(message), ctypes.c_void_p(hwnd), _MARKER, _MARKER, _PM_NOREMOVE):
            return event(message)
        return None

    def pump(name: str, bound: int = _PUMP_BOUND) -> tuple[int, bool]:
        require_owned(shells[name])
        result = apis[name].pump_messages(shells[name], bound)
        pump_results[name].append(result)
        return result

    def markers(events):
        return [value for value in events if value[1] == _MARKER]

    # Do not consume or replace a quit request belonging to the caller.
    existing_quit = _Win32Message()
    if raw_peek(ctypes.byref(existing_quit), ctypes.c_void_p(-1), _WM_QUIT, _WM_QUIT, _PM_NOREMOVE):
        raise RuntimeError("queued-routing witness requires a thread without a pending quit")

    try:
        for name, api in apis.items():
            same_thread()
            shell = api.create_shell(384, 240)
            shells[name] = shell
            all_windows.append(shell)
            require_owned(shell)
            show_window(ctypes.c_void_p(shell), 0)  # Hide immediately; no activation/capture APIs.
            all_windows.extend(getattr(api, "_ui_handles", ()))
            observe(api, name)
        children = {name: create_extra(shell) for name, shell in shells.items()}
        grandchild = create_extra(children["a"])
        sentinel = create_extra()
        sentinel_marker = post(sentinel, 1)
        expected_b = [post(shells["b"], 11), post(children["b"], 12)]
        expected_a = [post(shells["a"], 21), post(children["a"], 22)]
        expected_b.extend((post(shells["b"], 13), post(children["b"], 14)))
        expected_a.extend((post(shells["a"], 23), post(grandchild, 24)))

        # Foreign markers are earlier in the same real thread queue. A must
        # progress using bounded calls without consuming or dispatching them.
        for _ in range(_MAX_CYCLES):
            pump("a")
            if len(markers(consumed["a"])) >= len(expected_a):
                break
        a_after_a = tuple(markers(consumed["a"]))
        b_after_a = tuple(consumed["b"])
        b_shell_retained = inspect_marker(shells["b"])
        b_child_retained = inspect_marker(children["b"])
        sentinel_after_a = inspect_marker(sentinel)
        a_call_count = len(filters["a"])

        for _ in range(_MAX_CYCLES):
            pump("b")
            if len(markers(consumed["b"])) >= len(expected_b):
                break
        a_after_b = tuple(markers(consumed["a"]))
        sentinel_after_b = inspect_marker(sentinel)
        routing_results = {name: tuple(values) for name, values in pump_results.items()}
        owned_targets_only = {
            name: all(
                target == shells[name]
                or bool(is_child(ctypes.c_void_p(shells[name]), ctypes.c_void_p(target)))
                for target, kind, _, _ in consumed[name]
                if kind != _WM_QUIT
            )
            for name in apis
        }

        # PostQuitMessage is low-priority/coalesced. Finish the independent
        # preservation proof by taking only our own sentinel marker before the
        # quit phase; do not assume an unmatched foreign queue behaves as a deque.
        require_owned(sentinel)
        sentinel_message = _Win32Message()
        sentinel_removed = (
            event(sentinel_message)
            if raw_peek(
                ctypes.byref(sentinel_message),
                ctypes.c_void_p(sentinel),
                _MARKER,
                _MARKER,
                _PM_REMOVE,
            )
            else None
        )

        # One generated thread quit must fan out to both live shell owners.
        same_thread()
        post_quit(0)
        quit_posted = True
        quit_a = (0, False)
        quit_results = []
        for _ in range(_MAX_CYCLES):
            quit_a = pump("a", 1)
            quit_results.append(quit_a)
            if quit_a[1]:
                break
        quit_b = pump("b", 1)
        quit_results.append(quit_b)
        sticky_quit = (pump("a", 1), pump("b", 1))
        sentinel_after_quit = inspect_marker(sentinel)
    finally:
        # Cleanup is owner-local, best-effort for *every* resource, and runs
        # before behavior assertions even when create/post/pump raises.
        if quit_posted and not any(
            value[1] == _WM_QUIT for values in consumed.values() for value in values
        ):
            try:
                same_thread()
                message = _Win32Message()
                raw_peek(ctypes.byref(message), ctypes.c_void_p(-1), _WM_QUIT, _WM_QUIT, _PM_REMOVE)
            except Exception:
                cleanup_errors.append("generated thread quit cleanup failed")
        # Remove only generated marker messages addressed to our exact HWNDs.
        # DestroyWindow alone is not evidence that queued markers were removed.
        for hwnd in (*reversed(extra_windows), *reversed(tuple(shells.values()))):
            try:
                same_thread()
                if not is_window(ctypes.c_void_p(hwnd)):
                    continue
                for _ in range(_MAX_CYCLES):
                    message = _Win32Message()
                    if not raw_peek(
                        ctypes.byref(message), ctypes.c_void_p(hwnd), _MARKER, _MARKER, _PM_REMOVE
                    ):
                        break
                    cleanup_markers.append(event(message))
                else:
                    cleanup_errors.append("generated marker cleanup exceeded bound")
            except Exception:
                cleanup_errors.append("generated marker cleanup raised")
        for hwnd in reversed(extra_windows):
            try:
                same_thread()
                if is_window(ctypes.c_void_p(hwnd)) and not raw_destroy(ctypes.c_void_p(hwnd)):
                    cleanup_errors.append("generated child or sentinel cleanup failed")
            except Exception:
                cleanup_errors.append("generated child or sentinel cleanup raised")
        for name, hwnd in reversed(tuple(shells.items())):
            try:
                same_thread()
                apis[name].destroy_shell(hwnd)
            except Exception:
                cleanup_errors.append("operator shell cleanup raised")
                try:
                    # Recovery still acts only on this test's exact owned HWND.
                    if is_window(ctypes.c_void_p(hwnd)):
                        raw_destroy(ctypes.c_void_p(hwnd))
                except Exception:
                    cleanup_errors.append("owned shell fallback cleanup raised")
        remaining_windows = [hwnd for hwnd in all_windows if is_window(ctypes.c_void_p(hwnd))]

    assert not cleanup_errors, cleanup_errors
    assert not remaining_windows, "queued-routing witness left an owned HWND alive"
    assert not cleanup_markers
    assert a_after_a == tuple(expected_a)
    assert not b_after_a
    assert b_shell_retained == expected_b[0]
    assert b_child_retained == expected_b[1]
    assert sentinel_after_a == sentinel_after_b == sentinel_removed == sentinel_marker
    assert sentinel_after_quit is None
    assert a_after_b == a_after_a
    assert a_call_count > 1, "multiple bounded pumps must be required for A's markers"
    assert markers(consumed["a"]) == markers(dispatched["a"]) == expected_a
    assert markers(consumed["b"]) == markers(dispatched["b"]) == expected_b
    assert all(owned_targets_only.values())
    assert all(filters[name] and set(filters[name]) == {shells[name]} for name in apis)
    assert all(
        0 <= count <= _PUMP_BOUND and not close
        for values in routing_results.values()
        for count, close in values
    )
    assert all(0 <= count <= 1 for count, _ in quit_results)
    assert quit_a[1] and quit_b == (0, True)
    assert sticky_quit == ((0, True), (0, True))
    assert sum(value[1] == _WM_QUIT for value in consumed["a"]) == 1
    assert not any(value[1] == _WM_QUIT for value in consumed["b"])
    assert not any(value[1] == _WM_QUIT for values in dispatched.values() for value in values)
    return {
        "schema_version": "1",
        "qualification": "owned-generated-queued-message-routing",
        "shell_kind": shell_kind,
        "native_pointer_bits": 64,
        "concurrent_operator_shells": 2,
        "generated_children": 2,
        "generated_grandchildren": 1,
        "generated_foreign_sentinels": 1,
        "shell_and_child_markers_per_owner": 4,
        "foreign_sentinel_preserved": True,
        "pump_message_bound": _PUMP_BOUND,
        "quit_message_bound": 1,
        "thread_quit_messages_consumed": 1,
        "owners_observing_quit": 2,
        "cleanup_complete": True,
        "scope": "queued messages only; no native callback or user activation claim",
    }
