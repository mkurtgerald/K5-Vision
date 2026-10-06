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
from queue import Empty, Queue
from time import monotonic

_MARKER = 0x8000 + 0x325
_WM_QUIT = 0x0012
_PM_NOREMOVE = 0
_PM_REMOVE = 1
_PUMP_BOUND = 2
_MAX_CYCLES = 32
_COOKIE = 1 << 40
_SIGNED_COOKIE = -(1 << 40)
_OWNER_JOIN_SECONDS = 10.0
_OWNER_CLEANUP_SECONDS = 5.0
_SENTINEL_SETTLE_SECONDS = 1.0
_SENTINEL_SYSTEM_MESSAGE = 0x031F  # WM_DWMNCRENDERINGCHANGED, observed on exact 9769.
_GWLP_USERDATA = -21
_GW_CHILD = 5
_QUEUE_STATUS_MASK = 0x1DFF  # Documented input, paint, timer, send and all-posted bits.
_QUIT_DIAGNOSTIC_PROBES = (
    ("thread_quit", -1, _WM_QUIT, _WM_QUIT),
    ("all_quit", None, _WM_QUIT, _WM_QUIT),
    ("thread_head", -1, 0, 0),
    ("global_head", None, 0, 0),
)


def _collect_quit_diagnostics(validate, queue_status, peek) -> dict[str, object]:
    """Bounded non-removing observation after the original quit results are frozen.

    Four PeekMessage and two GetQueueStatus observations request neither removal
    nor explicit queued dispatch. Native internals can process events, dispatch
    sent callbacks, generate virtual messages and alter queue-status change flags.
    The count is bounded, not callback duration. Owner validity is checked after
    each observation; none can turn the already captured product failure into a pass.
    """

    def checked(call, *args):
        validate()
        result = call(*args)
        validate()
        return result

    before = checked(queue_status)
    probes = {}
    for label, selector, minimum, maximum in _QUIT_DIAGNOSTIC_PROBES:
        probes[label] = checked(peek, selector, minimum, maximum, _PM_NOREMOVE)
    after = checked(queue_status)
    return {
        "queue_status_before": before,
        "queue_status_after": after,
        "probes": probes,
        "peek_calls": len(_QUIT_DIAGNOSTIC_PROBES),
        "removal_requested": False,
        "queued_dispatch_requested": False,
        "observed_after_product_results_frozen": True,
        "sent_callbacks_may_run": True,
        "virtual_messages_may_be_generated": True,
        "queue_status_change_flags_may_be_cleared": True,
        "observations_are_sequential_not_atomic": True,
    }


def _settle_sentinel_queue(validate, take, dispatch, *, clock=monotonic):
    """Settle only the generated sentinel's observed notification, with hard bounds.

    Bounds limit calls and elapsed observation, not the duration of native sent
    callbacks. The outer owner-thread join remains a hard qualification deadline.
    """
    deadline = clock() + _SENTINEL_SETTLE_SECONDS
    kinds = []
    for _ in range(_MAX_CYCLES):
        validate()
        if clock() >= deadline:
            raise RuntimeError("sentinel settling exceeded deadline")
        message = take()
        validate()
        if clock() >= deadline:
            raise RuntimeError("sentinel settling exceeded deadline")
        if message is None:
            return tuple(kinds)
        dispatch(message)
        validate()
        if clock() >= deadline:
            raise RuntimeError("sentinel settling exceeded deadline")
        kinds.append(int(message.message))
    raise RuntimeError("sentinel settling exceeded message bound")


def _run_on_owner_thread(
    work, *, join_seconds=_OWNER_JOIN_SECONDS, cleanup_seconds=_OWNER_CLEANUP_SECONDS
):
    """Own a disposable test GUI queue without touching pytest's ambient queue.

    Cooperative cancellation permits cleanup on the creator thread. A native call
    that cannot return within both waits is a hard qualification failure; never
    report cleanup or leak freedom for that path. No thread is forcibly stopped.
    """
    if not 0 < join_seconds <= 30 or not 0 < cleanup_seconds <= 30:
        raise ValueError("owner-thread observation bounds are invalid")
    caller = threading.current_thread()
    stopped = threading.Event()
    result = Queue(maxsize=1)

    def execute():
        try:
            if threading.current_thread() is caller:
                raise RuntimeError("native witness did not isolate its creator thread")
            receipt = work(stopped)
        except BaseException as error:
            result.put_nowait((False, error))
        else:
            result.put_nowait((True, receipt))

    owner = threading.Thread(target=execute, name="k5-owned-routing-witness", daemon=True)
    cleanup_deadline = None
    timed_out = False

    def join_cleanup():
        nonlocal cleanup_deadline
        stopped.set()
        if cleanup_deadline is None:
            cleanup_deadline = monotonic() + cleanup_seconds
        remaining = max(0.0, cleanup_deadline - monotonic())
        if owner.is_alive() and remaining:
            owner.join(remaining)

    try:
        owner.start()
        owner.join(join_seconds)
        if owner.is_alive():
            timed_out = True
            join_cleanup()
    except BaseException:
        # A parent interruption must request creator-thread cleanup too. If it
        # interrupted cleanup, spend only the original grace's remaining time.
        stopped.set()
        try:
            join_cleanup()
        except BaseException:
            pass  # Preserve the original interruption; never claim clean success.
        raise
    if timed_out:
        if owner.is_alive():
            raise RuntimeError(
                "native witness owner thread is still running; cleanup is unverified"
            )
        raise TimeoutError("native witness exceeded its deadline; owner thread joined after stop")
    try:
        successful, value = result.get_nowait()
    except Empty:
        raise RuntimeError("native witness owner thread returned no result") from None
    if not successful:
        raise value
    return value


def run_owned_queued_message_routing_on_owner_thread(shell_kind: str) -> dict[str, object]:
    """Run the complete unchanged native lifecycle on a fresh thread-local queue."""
    if sys.platform != "win32":
        raise RuntimeError("queued routing qualification requires Windows")
    receipt = _run_on_owner_thread(
        lambda stopped: run_owned_queued_message_routing(shell_kind, _stop_event=stopped)
    )
    return {
        **receipt,
        "isolated_owner_thread": True,
        "owner_thread_joined": True,
        "parent_thread_queue_polled": False,
        "media_execution": False,
        "python_window_callbacks_created": 0,
    }


def run_owned_queued_message_routing(
    shell_kind: str, *, _stop_event: threading.Event | None = None
) -> dict[str, object]:
    """Exercise real create/pump/destroy APIs; assert outcomes after owner cleanup."""
    if sys.platform != "win32":
        raise RuntimeError("queued routing qualification requires Windows")

    import json

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
    get_window = bind(user32, "GetWindow", [ctypes.c_void_p, ctypes.c_uint32], ctypes.c_void_p)
    get_user_data = bind(
        user32, "GetWindowLongPtrW", [ctypes.c_void_p, ctypes.c_int], ctypes.c_ssize_t
    )
    set_user_data = bind(
        user32,
        "SetWindowLongPtrW",
        [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t],
        ctypes.c_ssize_t,
    )
    window_thread = bind(
        user32,
        "GetWindowThreadProcessId",
        [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)],
        ctypes.c_uint32,
    )
    get_thread = bind(kernel32, "GetCurrentThreadId", [], ctypes.c_uint32)
    show_window = bind(user32, "ShowWindow", [ctypes.c_void_p, ctypes.c_int], ctypes.c_int)
    get_queue_status = bind(user32, "GetQueueStatus", [ctypes.c_uint32], ctypes.c_uint32)
    raw_peek = apis["a"]._peek_message
    raw_destroy = apis["a"]._destroy_window
    raw_dispatch = apis["a"]._dispatch_message
    native_thread = int(get_thread())
    shells: dict[str, int] = {}
    extra_windows: list[int] = []
    all_windows: list[int] = []
    consumed: dict[str, list[tuple[int, int, int, int]]] = {"a": [], "b": []}
    dispatched: dict[str, list[tuple[int, int, int, int]]] = {"a": [], "b": []}
    filters: dict[str, list[tuple[int, int, int, int]]] = {"a": [], "b": []}
    pump_results: dict[str, list[tuple[int, bool]]] = {"a": [], "b": []}
    cleanup_errors: list[str] = []
    cleanup_markers: list[tuple[int, int, int, int]] = []
    quit_posted = False
    sentinel = 0
    sentinel_generation = object()  # Retain the full-width opaque tag through cleanup.
    sentinel_settled_kinds = ()
    registrations: dict[str, object] = {}
    quit_diagnostics = None
    post_quit_thread_matches = False
    cleanup_started = False
    quit_cleanup_result = None
    post_quit_abi_verified = (
        isinstance(post_quit, ctypes._CFuncPtr)
        and post_quit.argtypes == [ctypes.c_int]
        and post_quit.restype is None
    )

    def same_thread() -> None:
        if threading.get_ident() != owner_thread or int(get_thread()) != native_thread:
            raise RuntimeError("queued-routing witness changed owner thread")
        if not cleanup_started and _stop_event is not None and _stop_event.is_set():
            raise RuntimeError("queued-routing witness owner stop requested")

    def require_owned(hwnd: int) -> None:
        same_thread()
        if (
            hwnd not in all_windows
            or not is_window(ctypes.c_void_p(hwnd))
            or int(window_thread(ctypes.c_void_p(hwnd), None)) != native_thread
        ):
            raise RuntimeError("queued-routing witness refused a non-owned HWND")

    def validate_diagnostic_owners() -> None:
        same_thread()
        for name, shell in shells.items():
            apis[name]._message_route.require(shell, registration=registrations[name])
            require_owned(shell)

    def require_sentinel() -> None:
        # Cleanup depends only on this sentinel, never on peer route health.
        same_thread()
        require_owned(sentinel)
        if (
            sentinel not in extra_windows
            or int(get_parent(ctypes.c_void_p(sentinel)) or 0) != 0
            or ctypes.c_void_p(get_user_data(ctypes.c_void_p(sentinel), _GWLP_USERDATA)).value
            != id(sentinel_generation)
        ):
            raise RuntimeError("sentinel identity or generation changed")
        require_owned(sentinel)

    def validate_sentinel_phase() -> None:
        validate_diagnostic_owners()
        require_sentinel()
        # An HWND filter also selects children. This fixture creates none beneath
        # its standalone STATIC sentinel and installs no subclass callback there.
        if get_window(ctypes.c_void_p(sentinel), _GW_CHILD):
            raise RuntimeError("sentinel unexpectedly has children")
        require_sentinel()

    def take_sentinel_message():
        # Both PeekMessage calls may execute sent callbacks. Observation/removal
        # is non-atomic: a second-call target mutation fails before dispatch but
        # cannot undo native removal. Never claim universal queue preservation.
        # Only the observed DWM notification is requested; WM_QUIT can bypass the
        # range filter and must be rejected at the non-removing observation.
        validate_sentinel_phase()
        message = _Win32Message()
        available = bool(
            raw_peek(
                ctypes.byref(message),
                ctypes.c_void_p(sentinel),
                _SENTINEL_SYSTEM_MESSAGE,
                _SENTINEL_SYSTEM_MESSAGE,
                _PM_NOREMOVE,
            )
        )
        validate_sentinel_phase()
        if not available:
            return None
        if int(message.hwnd or 0) != sentinel or message.message != _SENTINEL_SYSTEM_MESSAGE:
            raise RuntimeError("sentinel observation refused unexpected message ownership or kind")
        validate_sentinel_phase()
        removed = bool(
            raw_peek(
                ctypes.byref(message),
                ctypes.c_void_p(sentinel),
                _SENTINEL_SYSTEM_MESSAGE,
                _SENTINEL_SYSTEM_MESSAGE,
                _PM_REMOVE,
            )
        )
        validate_sentinel_phase()
        if (
            not removed
            or int(message.hwnd or 0) != sentinel
            or message.message != _SENTINEL_SYSTEM_MESSAGE
        ):
            raise RuntimeError("sentinel removal changed message ownership or kind")
        return message

    def dispatch_sentinel_message(message):
        validate_sentinel_phase()
        if int(message.hwnd or 0) != sentinel or message.message != _SENTINEL_SYSTEM_MESSAGE:
            raise RuntimeError("sentinel dispatch refused unexpected message ownership or kind")
        raw_dispatch(ctypes.byref(message))
        validate_sentinel_phase()

    def diagnostic_queue_status():
        result = int(get_queue_status(_QUEUE_STATUS_MASK))
        return {"current": result >> 16, "changed": result & 0xFFFF}

    def diagnostic_peek(selector, minimum, maximum, flags):
        if flags != _PM_NOREMOVE:
            raise RuntimeError("quit diagnostic refused a removing observation")
        message = _Win32Message()
        available = bool(
            raw_peek(
                ctypes.byref(message),
                None if selector is None else ctypes.c_void_p(selector),
                minimum,
                maximum,
                flags,
            )
        )
        if not available:
            return {"available": False}
        hwnd = int(message.hwnd or 0)
        if hwnd == 0:
            scope = "thread"
        elif hwnd in shells.values():
            scope = "owned_shell"
        elif hwnd in all_windows:
            scope = "owned_generated_child_or_sentinel"
        else:
            scope = "foreign_window"
        # Never retain HWND, WPARAM, LPARAM, text or source identity in diagnostics.
        return {"available": True, "message": int(message.message), "scope": scope}

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
            filters[name].append((int(getattr(hwnd, "value", hwnd) or 0), minimum, maximum, flags))
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
            registrations[name] = api._message_route.registration(shell)
            all_windows.append(shell)
            require_owned(shell)
            show_window(ctypes.c_void_p(shell), 0)  # Hide immediately; no activation/capture APIs.
            all_windows.extend(getattr(api, "_ui_handles", ()))
            observe(api, name)
        children = {name: create_extra(shell) for name, shell in shells.items()}
        grandchild = create_extra(children["a"])
        sentinel = create_extra()
        require_owned(sentinel)
        if get_user_data(ctypes.c_void_p(sentinel), _GWLP_USERDATA) != 0:
            raise RuntimeError("new sentinel already has an identity tag")
        previous_tag = set_user_data(
            ctypes.c_void_p(sentinel), _GWLP_USERDATA, ctypes.c_ssize_t(id(sentinel_generation))
        )
        if previous_tag != 0:
            raise RuntimeError("sentinel identity tag changed during creation")
        # SetWindowLongPtr's zero result is ambiguous; verify installation itself.
        # If it failed, cleanup refuses an unverified generation and this run fails.
        require_sentinel()
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
        a_call_count = len(pump_results["a"])

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

        if not (sentinel_after_a == sentinel_after_b == sentinel_removed == sentinel_marker):
            raise RuntimeError("sentinel preservation proof failed before settling")
        # Isolation removed the ambient thread's blocker but was insufficient:
        # exact 9769 retained this owned sentinel's DWM notification. Settle only
        # that kind after the original preservation proof, before generated quit.
        sentinel_settled_kinds = _settle_sentinel_queue(
            validate_sentinel_phase, take_sentinel_message, dispatch_sentinel_message
        )

        # One generated thread quit must fan out to both live shell owners.
        same_thread()
        thread_before_post = int(get_thread())
        post_quit(0)
        thread_after_post = int(get_thread())
        post_quit_thread_matches = thread_before_post == thread_after_post == native_thread
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
        # Freeze every original result before additional diagnostics: observations
        # cannot satisfy the product's earlier quit/preservation assertions.
        quit_diagnostics = _collect_quit_diagnostics(
            validate_diagnostic_owners, diagnostic_queue_status, diagnostic_peek
        )
        quit_diagnostics.update(
            {
                "sentinel_settled_count": len(sentinel_settled_kinds),
                "sentinel_settled_message_types": sorted(set(sentinel_settled_kinds)),
                "post_quit_thread_matches": post_quit_thread_matches,
                "post_quit_abi_verified": post_quit_abi_verified,
                "thread_selector_is_pointer_width_all_ones": (
                    ctypes.c_void_p(-1).value == (1 << (8 * ctypes.sizeof(ctypes.c_void_p))) - 1
                ),
                "product_quit_results": tuple(quit_results),
                "product_sticky_quit": sticky_quit,
                "product_thread_quit_consumed": sum(
                    value[1] == _WM_QUIT for value in consumed["a"]
                ),
            }
        )
    finally:
        cleanup_started = True
        # Cleanup is owner-local, best-effort for *every* resource, and runs
        # before behavior assertions even when create/post/pump raises.
        if quit_posted and not any(
            value[1] == _WM_QUIT for values in consumed.values() for value in values
        ):
            try:
                same_thread()
                message = _Win32Message()
                removed = bool(
                    raw_peek(
                        ctypes.byref(message), ctypes.c_void_p(-1), _WM_QUIT, _WM_QUIT, _PM_REMOVE
                    )
                )
                quit_cleanup_result = {
                    "removed": removed,
                    "was_thread_quit": removed and message.message == _WM_QUIT and not message.hwnd,
                }
            except Exception:
                cleanup_errors.append("generated thread quit cleanup failed")
        # Remove only generated marker messages addressed to our exact HWNDs.
        # DestroyWindow alone is not evidence that queued markers were removed.
        for hwnd in (*reversed(extra_windows), *reversed(tuple(shells.values()))):
            try:
                same_thread()
                if not is_window(ctypes.c_void_p(hwnd)):
                    continue
                if hwnd == sentinel:
                    require_sentinel()
                for _ in range(_MAX_CYCLES):
                    if hwnd == sentinel:
                        require_sentinel()
                    message = _Win32Message()
                    if not raw_peek(
                        ctypes.byref(message), ctypes.c_void_p(hwnd), _MARKER, _MARKER, _PM_REMOVE
                    ):
                        break
                    if hwnd == sentinel:
                        require_sentinel()
                    cleanup_markers.append(event(message))
                else:
                    cleanup_errors.append("generated marker cleanup exceeded bound")
            except Exception:
                cleanup_errors.append("generated marker cleanup raised")
        for hwnd in reversed(extra_windows):
            try:
                same_thread()
                if hwnd == sentinel and is_window(ctypes.c_void_p(hwnd)):
                    require_sentinel()
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
        remaining_registrations = sum(
            api._message_route._shell is not None for api in apis.values()
        )

    if quit_diagnostics is not None:
        quit_diagnostics["existing_quit_cleanup_result"] = quit_cleanup_result
        # Failed pytest cases retain this bounded source/handle-free snapshot.
        print("K5_NATIVE_QUIT_DIAGNOSTIC=" + json.dumps(quit_diagnostics, sort_keys=True))
    assert not cleanup_errors, cleanup_errors
    assert not remaining_windows, "queued-routing witness left an owned HWND alive"
    assert remaining_registrations == 0, "queued-routing witness left an owned registration alive"
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
    # Each native retrieval selects either this exact HWND/children or only
    # thread WM_QUIT. Never accept an all-window or unrestricted thread poll.
    thread_quit_filter = (int(ctypes.c_void_p(-1).value), _WM_QUIT, _WM_QUIT, _PM_REMOVE)
    assert all(
        filters[name]
        and (shells[name], 0, 0, _PM_REMOVE) in filters[name]
        and thread_quit_filter in filters[name]
        and set(filters[name]) <= {(shells[name], 0, 0, _PM_REMOVE), thread_quit_filter}
        for name in apis
    )
    assert all(
        0 <= count <= _PUMP_BOUND and not close
        for values in routing_results.values()
        for count, close in values
    )
    assert all(0 <= count <= 1 for count, _ in quit_results)
    assert quit_a[1] and quit_b == (0, True), {
        "quit_a": quit_a,
        "quit_b": quit_b,
        "quit_results": quit_results,
        "thread_quit_consumed": sum(value[1] == _WM_QUIT for value in consumed["a"]),
        "diagnostic": quit_diagnostics,
    }
    assert sticky_quit == ((0, True), (0, True))
    assert sum(value[1] == _WM_QUIT for value in consumed["a"]) == 1
    assert not any(value[1] == _WM_QUIT for value in consumed["b"])
    assert not any(value[1] == _WM_QUIT for values in dispatched.values() for value in values)
    return {
        "schema_version": "1",
        "qualification": "owned-generated-queued-message-routing",
        "quit_diagnostics": quit_diagnostics,
        "shell_kind": shell_kind,
        "native_pointer_bits": 64,
        "concurrent_operator_shells": 2,
        "generated_children": 2,
        "generated_grandchildren": 1,
        "generated_foreign_sentinels": 1,
        "shell_and_child_markers_per_owner": 4,
        "foreign_sentinel_preserved": True,
        "sentinel_generation_verified": True,
        "sentinel_settled_count": len(sentinel_settled_kinds),
        "sentinel_settled_message_types": sorted(set(sentinel_settled_kinds)),
        "pump_message_bound": _PUMP_BOUND,
        "quit_message_bound": 1,
        "thread_quit_messages_consumed": 1,
        "owners_observing_quit": 2,
        "cleanup_complete": True,
        "remaining_route_registrations": remaining_registrations,
        "scope": "queued messages only; no native callback or user activation claim",
    }
