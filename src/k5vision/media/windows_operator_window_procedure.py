"""Owned, thread-affine Win32 subclass for synchronous shell messages.

Importing this module never loads a native library. The caller supplies its
already loaded user32 boundary and an exact live shell registration. A handler
only records bounded synchronous intent: return an integer to consume a message
or None to forward it. In particular, playback can consume WM_CLOSE and
WM_COMMAND without starting asynchronous work inside a native callback.

Call detach() on the creator thread before destroying the shell. Unexpected
WM_NCDESTROY retires the exact registration; detach() then releases the callback
pin without touching the dead HWND. Never drop a callback merely because native
cleanup failed: Windows may still hold its address.
"""

from __future__ import annotations

import ctypes
import threading
from collections.abc import Callable
from typing import Any

from k5vision.media.windows_operator_message_routing import OwnedShellMessageRoute

_GWLP_WNDPROC = -4
_WM_NCDESTROY = 0x0082
_RESULT_BITS = ctypes.sizeof(ctypes.c_ssize_t) * 8
_RESULT_MIN = -(1 << (_RESULT_BITS - 1))
_RESULT_MAX = (1 << (_RESULT_BITS - 1)) - 1

WindowMessageHandler = Callable[[int, int, int, int], int | None]
# A failed detach must survive even if the owner subsequently drops its object.
# Pins are removed only by an explicit detach outside a native callback.
_PINNED_PROCEDURES: set[OwnedWindowProcedure] = set()


class WindowsOperatorWindowProcedureError(RuntimeError):
    """Sanitized failure with no native identity or callback exception retained."""

    def __init__(self) -> None:
        super().__init__("operator shell window procedure failed")


def _make_callback(handler: Callable[[int, int, int, int], int]) -> tuple[Any, int]:
    """Create the stdcall callback only on explicit installation, never import."""
    callback_type = ctypes.WINFUNCTYPE(
        ctypes.c_ssize_t,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_size_t,
        ctypes.c_ssize_t,
    )
    callback = callback_type(handler)
    address = int(ctypes.cast(callback, ctypes.c_void_p).value or 0)
    if not address:
        raise WindowsOperatorWindowProcedureError()
    return callback, address


def _address(value: int) -> int:
    return int(ctypes.c_void_p(value).value or 0)


class OwnedWindowProcedure:
    """One subclass lifetime, never transferable to a reused HWND registration.

    The pump must check failed after any native operation that can dispatch sent
    messages, even an empty PeekMessage call. Callback exceptions are latched and
    consumed, rather than escaping the ctypes ABI. destroyed means an unexpected
    WM_NCDESTROY already retired this route; do not destroy that HWND again.
    """

    def __init__(
        self,
        user32: Any,
        shell: int,
        route: OwnedShellMessageRoute,
        on_message: WindowMessageHandler,
    ) -> None:
        self._thread = threading.current_thread()
        self._route = route
        self._shell = shell
        self._handler = on_message
        self._failed = False
        self._destroyed = False
        self._destroying = False
        self._installed = False
        self._attempted = False
        self._callback_depth = 0
        self._callback: Any = None
        self._callback_address = 0
        self._previous = 0
        try:
            self._registration = route.registration(shell)
            if not callable(on_message):
                raise WindowsOperatorWindowProcedureError()
            # The Ptr names are C macros mapping to Long on 32-bit Windows.
            suffix = "LongPtrW" if ctypes.sizeof(ctypes.c_void_p) == 8 else "LongW"
            self._get_procedure = getattr(user32, "GetWindow" + suffix)
            self._get_procedure.argtypes = [ctypes.c_void_p, ctypes.c_int]
            self._get_procedure.restype = ctypes.c_ssize_t
            self._set_procedure = getattr(user32, "SetWindow" + suffix)
            self._set_procedure.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
            self._set_procedure.restype = ctypes.c_ssize_t
            self._call_procedure = user32.CallWindowProcW
            self._call_procedure.argtypes = [
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.c_size_t,
                ctypes.c_ssize_t,
            ]
            self._call_procedure.restype = ctypes.c_ssize_t
        except Exception:
            raise WindowsOperatorWindowProcedureError() from None

    @property
    def failed(self) -> bool:
        return self._failed

    @property
    def destroyed(self) -> bool:
        return self._destroyed

    @property
    def installed(self) -> bool:
        return self._installed

    def _require_thread(self) -> None:
        if threading.current_thread() is not self._thread:
            raise WindowsOperatorWindowProcedureError()

    def _require_owned(self) -> None:
        self._require_thread()
        if self._destroyed:
            raise WindowsOperatorWindowProcedureError()
        self._route.require(self._shell, registration=self._registration)

    def _current_procedure(self) -> int:
        self._require_owned()
        result = _address(int(self._get_procedure(ctypes.c_void_p(self._shell), _GWLP_WNDPROC)))
        self._require_owned()
        if not result:
            raise WindowsOperatorWindowProcedureError()
        return result

    def install(self) -> None:
        """Install once, retaining a strong callback pin before the native call."""
        try:
            self._require_owned()
            if self._attempted:
                raise WindowsOperatorWindowProcedureError()
            self._attempted = True
            self._previous = self._current_procedure()
            self._callback, self._callback_address = _make_callback(self._dispatch)
            _PINNED_PROCEDURES.add(self)
            # SetWindowLongPtr may enter sent callbacks. The previous procedure
            # and lifetime pin must already exist before it becomes callable.
            self._installed = True
            previous = _address(
                int(
                    self._set_procedure(
                        ctypes.c_void_p(self._shell),
                        _GWLP_WNDPROC,
                        ctypes.c_ssize_t(self._callback_address).value,
                    )
                )
            )
            self._require_owned()
            # A real WNDPROC cannot be null. Unlike general window attributes,
            # a zero previous WNDPROC is therefore always an installation failure.
            if not previous:
                raise WindowsOperatorWindowProcedureError()
            changed = previous != self._previous
            self._previous = previous
            if changed or self._failed:
                raise WindowsOperatorWindowProcedureError()
        except Exception:
            self._failed = True
            raise WindowsOperatorWindowProcedureError() from None

    def detach(self) -> None:
        """Restore before DestroyWindow; on failure keep the callback pinned.

        Do not call this inside the handler. It must finish outside all callback
        frames, including when acknowledging an unexpected terminal destruction.
        Registration ownership remains with the shell on a successful restore.
        """
        try:
            self._require_thread()
            if self._callback_depth:
                raise WindowsOperatorWindowProcedureError()
            if not self._installed or self._destroyed:
                self._installed = False
                _PINNED_PROCEDURES.discard(self)
                return
            current = self._current_procedure()
            if current != self._previous:
                # Another subclass may be chained above us. Never overwrite it
                # or release an address it may still use to call its predecessor.
                if current != self._callback_address:
                    raise WindowsOperatorWindowProcedureError()
                replaced = _address(
                    int(
                        self._set_procedure(
                            ctypes.c_void_p(self._shell),
                            _GWLP_WNDPROC,
                            ctypes.c_ssize_t(self._previous).value,
                        )
                    )
                )
                self._require_owned()
                if replaced != self._callback_address:
                    raise WindowsOperatorWindowProcedureError()
            self._installed = False
            _PINNED_PROCEDURES.discard(self)
        except Exception:
            self._failed = True
            raise WindowsOperatorWindowProcedureError() from None

    def _forward(self, hwnd: int, message: int, wparam: int, lparam: int) -> int:
        self._require_owned()
        return int(
            self._call_procedure(
                ctypes.c_void_p(self._previous),
                ctypes.c_void_p(hwnd),
                message,
                wparam,
                lparam,
            )
        )

    def _retire(self) -> None:
        self._destroyed = True
        self._installed = False
        try:
            self._route.unregister(self._shell, registration=self._registration)
        except BaseException:
            # Never retire a new registration that reused the same HWND.
            self._failed = True

    def _dispatch(self, hwnd: int, message: int, wparam: int, lparam: int) -> int:
        self._callback_depth += 1
        try:
            self._require_owned()
            if hwnd != self._shell or not self._installed or self._destroying:
                raise WindowsOperatorWindowProcedureError()
            if message == _WM_NCDESTROY:
                self._destroying = True
                try:
                    return self._forward(hwnd, message, wparam, lparam)
                finally:
                    # Keep the global callback pin through the return to native
                    # code. The caller releases it with detach after this frame.
                    self._retire()
            if self._failed:
                return 0
            result = self._handler(hwnd, message, wparam, lparam)
            # A handler or nested sent callback may retire/reuse the HWND.
            self._require_owned()
            if self._failed:
                return 0
            if result is None:
                return self._forward(hwnd, message, wparam, lparam)
            if type(result) is not int or not _RESULT_MIN <= result <= _RESULT_MAX:
                raise WindowsOperatorWindowProcedureError()
            return result
        except BaseException:
            self._failed = True
            return 0
        finally:
            self._callback_depth -= 1
