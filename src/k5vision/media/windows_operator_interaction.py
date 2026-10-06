"""Bounded source-free pointer input for the visible Win32 operator shell."""

from __future__ import annotations

import ctypes
import enum
import typing
from collections import deque

from pydantic import BaseModel, ConfigDict, Field

from k5vision.media.viewport_editor import _MAX_EDIT_DELTA
from k5vision.media.windows_operator_application import (
    BoundedWindowsOperatorApplication,
    WindowsOperatorApplicationError,
    WindowsOperatorApplicationErrorCode,
    WindowsOperatorApplicationState,
    _NativeShellBoundary,
    _NativeShellError,
    _NativeShellFailure,
    _Win32Message,
    _Win32OperatorShellApi,
    _Win32Point,
)

_MAX_POINTER_EVENTS = 256
_PM_REMOVE = 0x0001
_WM_CLOSE = 0x0010
_WM_QUIT = 0x0012
_WM_CANCELMODE = 0x001F
_WM_MOUSEMOVE = 0x0200
_WM_LBUTTONDOWN = 0x0201
_WM_LBUTTONUP = 0x0202
_WM_CAPTURECHANGED = 0x0215


class WindowsPointerEventKind(enum.StrEnum):
    DOWN = "down"
    MOVE = "move"
    UP = "up"
    CANCEL = "cancel"


class WindowsPointerEvent(BaseModel):
    """Ephemeral client-relative pointer input with no native/source identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: WindowsPointerEventKind
    x: int = Field(ge=-32_768, le=32_767)
    y: int = Field(ge=-32_768, le=32_767)


class _ProjectedWindowsPointerEvent(WindowsPointerEvent):
    """Trusted inverse projection can span the canonical layout's wider bounds."""

    x: int = Field(ge=0, le=2_000_000)
    y: int = Field(ge=0, le=2_000_000)


def _signed_word(value: int) -> int:
    return ctypes.c_short(value & 0xFFFF).value


class _InteractiveWin32OperatorShellApi(_Win32OperatorShellApi):
    """Capture a bounded source-free pointer stream while pumping Win32 messages."""

    def __init__(self) -> None:
        super().__init__()
        self._pointer_events: deque[WindowsPointerEvent] = deque()
        self._capture_active = False
        self._pointer_time_fence: int | None = None
        try:
            self._get_tick_count = self._kernel32.GetTickCount
            self._get_tick_count.argtypes = []
            self._get_tick_count.restype = ctypes.c_uint32
            self._is_child = self._user32.IsChild
            self._is_child.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            self._is_child.restype = ctypes.c_int
            self._map_window_points = self._user32.MapWindowPoints
            self._map_window_points.argtypes = [
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.POINTER(_Win32Point),
                ctypes.c_uint32,
            ]
            self._map_window_points.restype = ctypes.c_int

            self._set_capture = self._user32.SetCapture
            self._set_capture.argtypes = [ctypes.c_void_p]
            self._set_capture.restype = ctypes.c_void_p

            self._get_capture = self._user32.GetCapture
            self._get_capture.argtypes = []
            self._get_capture.restype = ctypes.c_void_p

            self._release_capture = self._user32.ReleaseCapture
            self._release_capture.argtypes = []
            self._release_capture.restype = ctypes.c_int
        except Exception:
            raise _NativeShellError(_NativeShellFailure.LOAD) from None

    def _append_pointer_event(
        self,
        *,
        kind: WindowsPointerEventKind,
        shell: int,
        hwnd: int,
        lparam: int,
    ) -> None:
        if len(self._pointer_events) >= _MAX_POINTER_EVENTS:
            raise _NativeShellError(_NativeShellFailure.PUMP)
        point = _Win32Point(_signed_word(lparam), _signed_word(lparam >> 16))
        if hwnd:
            try:
                self._map_window_points(
                    ctypes.c_void_p(hwnd),
                    ctypes.c_void_p(shell),
                    ctypes.byref(point),
                    1,
                )
            except Exception:
                raise _NativeShellError(_NativeShellFailure.PUMP) from None
        self._pointer_events.append(WindowsPointerEvent(kind=kind, x=int(point.x), y=int(point.y)))

    def _acquire_pointer_capture(self, shell: int) -> None:
        try:
            # Sent callbacks can move capture without a queued CAPTURECHANGED.
            # Never steal capture from another shell or an unrelated control.
            if int(self._get_capture() or 0) not in {0, shell}:
                raise _NativeShellError(_NativeShellFailure.PUMP)
            self._set_capture(ctypes.c_void_p(shell))
            captured = int(self._get_capture() or 0)
        except Exception:
            raise _NativeShellError(_NativeShellFailure.PUMP) from None
        if captured != shell:
            raise _NativeShellError(_NativeShellFailure.PUMP)
        self._capture_active = True

    def _release_pointer_capture(self, shell: int) -> None:
        if not self._capture_active:
            return
        self._capture_active = False
        try:
            if int(self._get_capture() or 0) != shell:
                return  # A sent callback transferred ownership; leave it alone.
            released = bool(self._release_capture())
        except Exception:
            raise _NativeShellError(_NativeShellFailure.PUMP) from None
        if not released:
            raise _NativeShellError(_NativeShellFailure.PUMP)

    def _cancel_pointer_capture(self, shell: int) -> None:
        if not self._capture_active:
            return
        self._release_pointer_capture(shell)
        self._append_pointer_event(
            kind=WindowsPointerEventKind.CANCEL,
            shell=shell,
            hwnd=0,
            lparam=0,
        )

    def _capture_was_lost(self, shell: int) -> None:
        if not self._capture_active:
            return
        self._capture_active = False
        self._append_pointer_event(
            kind=WindowsPointerEventKind.CANCEL,
            shell=shell,
            hwnd=0,
            lparam=0,
        )

    def pump_messages(self, shell: int, max_messages: int) -> tuple[int, bool]:
        count = 0
        close_requested = False
        message = _Win32Message()
        try:
            registration = self._message_route.registration(shell)
            if self._thread_quit_requested(shell):
                self._cancel_pointer_capture(shell)
                return 0, True
            while count < max_messages and self._peek_shell_message(shell, message, registration):
                count += 1
                hwnd = int(message.hwnd or 0)
                self._note_geometry_message(shell, message)
                if message.message == _WM_QUIT or (message.message == _WM_CLOSE and hwnd == shell):
                    self._cancel_pointer_capture(shell)
                    close_requested = True
                    continue
                if message.message == _WM_CANCELMODE:
                    self._cancel_pointer_capture(shell)
                    continue
                if message.message == _WM_CAPTURECHANGED:
                    self._capture_was_lost(shell)
                    continue
                if message.message in {
                    _WM_LBUTTONDOWN,
                    _WM_MOUSEMOVE,
                    _WM_LBUTTONUP,
                } and not self._pointer_message_for_shell(shell, message):
                    self._translate_message(ctypes.byref(message))
                    self._dispatch_message(ctypes.byref(message))
                    continue
                if self._stale_pointer_message(message):
                    continue
                if message.message == _WM_LBUTTONDOWN:
                    self._cancel_pointer_capture(shell)
                    self._append_pointer_event(
                        kind=WindowsPointerEventKind.DOWN,
                        shell=shell,
                        hwnd=hwnd,
                        lparam=int(message.lParam),
                    )
                    self._acquire_pointer_capture(shell)
                elif message.message == _WM_MOUSEMOVE:
                    self._append_pointer_event(
                        kind=WindowsPointerEventKind.MOVE,
                        shell=shell,
                        hwnd=hwnd,
                        lparam=int(message.lParam),
                    )
                elif message.message == _WM_LBUTTONUP:
                    self._append_pointer_event(
                        kind=WindowsPointerEventKind.UP,
                        shell=shell,
                        hwnd=hwnd,
                        lparam=int(message.lParam),
                    )
                    self._release_pointer_capture(shell)
                self._translate_message(ctypes.byref(message))
                self._dispatch_message(ctypes.byref(message))
            self._message_route.require(shell, registration=registration)
            if self._thread_quit_requested(shell):
                self._cancel_pointer_capture(shell)
                close_requested = True
        except _NativeShellError:
            raise
        except Exception:
            raise _NativeShellError(_NativeShellFailure.PUMP) from None
        return count, close_requested

    def _pointer_message_for_shell(self, shell: int, message: _Win32Message) -> bool:
        hwnd = int(message.hwnd or 0)
        if hwnd == shell:
            return True
        if not hwnd:
            return False
        try:
            return bool(self._is_child(ctypes.c_void_p(shell), ctypes.c_void_p(hwnd)))
        except Exception:
            raise _NativeShellError(_NativeShellFailure.PUMP) from None

    def _stale_pointer_message(self, message: _Win32Message) -> bool:
        fence = getattr(self, "_pointer_time_fence", None)
        if fence is None or message.message not in {_WM_LBUTTONDOWN, _WM_MOUSEMOVE, _WM_LBUTTONUP}:
            return False
        # MSG.time and GetTickCount use the same DWORD uptime clock. Equality is
        # conservatively stale; subtraction is valid across the 49.7-day wrap.
        elapsed = (int(message.time) - fence) & 0xFFFFFFFF
        return not 0 < elapsed < 0x80000000

    def discard_pointer_events(self, shell: int) -> None:
        """Drop old-geometry input even if an UP already released capture."""
        self._pointer_events.clear()
        if self._capture_active:
            try:
                captured = int(self._get_capture() or 0)
            except Exception:
                raise _NativeShellError(_NativeShellFailure.PUMP) from None
            if captured == shell:
                self._release_pointer_capture(shell)
            else:
                # Capture may already have moved to a command control or another
                # window. Never release someone else's capture during resize.
                self._capture_active = False
        self._pointer_events.clear()
        try:
            self._pointer_time_fence = int(self._get_tick_count()) & 0xFFFFFFFF
        except Exception:
            raise _NativeShellError(_NativeShellFailure.PUMP) from None

    def drain_pointer_events(self, max_events: int) -> tuple[WindowsPointerEvent, ...]:
        if not 1 <= max_events <= _MAX_POINTER_EVENTS:
            raise _NativeShellError(_NativeShellFailure.PUMP)
        drained: list[WindowsPointerEvent] = []
        while self._pointer_events and len(drained) < max_events:
            drained.append(self._pointer_events.popleft())
        return tuple(drained)


class BoundedInteractiveWindowsOperatorApplication(BoundedWindowsOperatorApplication):
    """Visible operator application with bounded ephemeral pointer capture."""

    def __init__(self, **kwargs: typing.Any) -> None:
        super().__init__(**kwargs)
        self._pointer_projection_cancel_pending = False
        self._mapped_pointer_start: tuple[int, int] | None = None

    def _discard_native_pointer_input(self) -> None:
        native = self._native_api
        try:
            discard = getattr(native, "discard_pointer_events", None)
            if callable(discard) and self._shell is not None:
                discard(self._shell)
                return
            drain = getattr(native, "drain_pointer_events", None)
            if callable(drain):
                drain(_MAX_POINTER_EVENTS)
        except Exception:
            raise WindowsOperatorApplicationError(
                WindowsOperatorApplicationErrorCode.PUMP_FAILURE,
                "operator application pointer cancellation failed",
            ) from None

    def _invalidate_projected_pointer_input(self) -> None:
        self._discard_native_pointer_input()
        self._mapped_pointer_start = None
        self._pointer_projection_cancel_pending = True

    def _ensure_native_api(self) -> _NativeShellBoundary:
        if self._native_api is not None:
            return self._native_api
        try:
            self._native_api = _InteractiveWin32OperatorShellApi()
        except _NativeShellError as exc:
            self._state = WindowsOperatorApplicationState.FAILED
            if exc.failure == _NativeShellFailure.UNSUPPORTED_PLATFORM:
                code = WindowsOperatorApplicationErrorCode.UNSUPPORTED_PLATFORM
                message = "operator application requires Win32"
            else:
                code = WindowsOperatorApplicationErrorCode.NATIVE_LOAD_FAILURE
                message = "operator application native API is unavailable"
            raise WindowsOperatorApplicationError(code, message) from None
        return self._native_api

    def drain_pointer_events(
        self,
        *,
        max_events: int = 64,
    ) -> tuple[WindowsPointerEvent, ...]:
        """Drain one bounded ephemeral pointer batch without retaining coordinates."""
        if not 1 <= max_events <= _MAX_POINTER_EVENTS:
            raise WindowsOperatorApplicationError(
                WindowsOperatorApplicationErrorCode.INVALID_CONFIGURATION,
                "operator application pointer batch is invalid",
            )
        native = self._native_api
        drain = None if native is None else getattr(native, "drain_pointer_events", None)
        if not callable(drain):
            return ()
        try:
            if self._pointer_projection_cancel_pending:
                self._discard_native_pointer_input()
                self._pointer_projection_cancel_pending = False
                return (WindowsPointerEvent(kind=WindowsPointerEventKind.CANCEL, x=0, y=0),)
            events = drain(max_events)
        except _NativeShellError:
            raise WindowsOperatorApplicationError(
                WindowsOperatorApplicationErrorCode.PUMP_FAILURE,
                "operator application pointer input failed",
            ) from None
        if not isinstance(events, tuple) or not all(
            isinstance(event, WindowsPointerEvent) for event in events
        ):
            raise WindowsOperatorApplicationError(
                WindowsOperatorApplicationErrorCode.PUMP_FAILURE,
                "operator application pointer input failed",
            )
        if self._projection is None:
            return typing.cast(tuple[WindowsPointerEvent, ...], events)
        mapped: list[WindowsPointerEvent] = []
        for event in events:
            if event.kind == WindowsPointerEventKind.CANCEL:
                self._mapped_pointer_start = None
                mapped.append(event)
                continue
            point = (
                None
                if self._pointer_projection_suspended
                else self._projection.logical_point(event.x, event.y)
            )
            start = self._mapped_pointer_start
            invalid_delta = (
                point is not None
                and start is not None
                and event.kind == WindowsPointerEventKind.UP
                and any(
                    abs(end - origin) > _MAX_EDIT_DELTA
                    for end, origin in zip(point, start, strict=True)
                )
            )
            if point is None or invalid_delta:
                self._discard_native_pointer_input()
                self._mapped_pointer_start = None
                mapped.append(WindowsPointerEvent(kind=WindowsPointerEventKind.CANCEL, x=0, y=0))
                break
            if event.kind == WindowsPointerEventKind.DOWN:
                self._mapped_pointer_start = point
            elif event.kind == WindowsPointerEventKind.UP:
                self._mapped_pointer_start = None
            mapped.append(_ProjectedWindowsPointerEvent(kind=event.kind, x=point[0], y=point[1]))
        return tuple(mapped)
