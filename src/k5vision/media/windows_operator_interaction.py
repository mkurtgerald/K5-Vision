"""Bounded source-free pointer input for the visible Win32 operator shell."""

from __future__ import annotations

import ctypes
import enum
import typing
from collections import deque

from pydantic import BaseModel, ConfigDict, Field

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
_WM_MOUSEMOVE = 0x0200
_WM_LBUTTONDOWN = 0x0201
_WM_LBUTTONUP = 0x0202


class WindowsPointerEventKind(enum.StrEnum):
    DOWN = "down"
    MOVE = "move"
    UP = "up"


class WindowsPointerEvent(BaseModel):
    """Ephemeral client-relative pointer input with no native/source identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: WindowsPointerEventKind
    x: int = Field(ge=-32_768, le=32_767)
    y: int = Field(ge=-32_768, le=32_767)


def _signed_word(value: int) -> int:
    return ctypes.c_short(value & 0xFFFF).value


class _InteractiveWin32OperatorShellApi(_Win32OperatorShellApi):
    """Capture a bounded source-free pointer stream while pumping Win32 messages."""

    def __init__(self) -> None:
        super().__init__()
        self._pointer_events: deque[WindowsPointerEvent] = deque()
        try:
            self._map_window_points = self._user32.MapWindowPoints
            self._map_window_points.argtypes = [
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.POINTER(_Win32Point),
                ctypes.c_uint32,
            ]
            self._map_window_points.restype = ctypes.c_int
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

    def pump_messages(self, shell: int, max_messages: int) -> tuple[int, bool]:
        count = 0
        close_requested = False
        message = _Win32Message()
        try:
            while count < max_messages and self._peek_message(
                ctypes.byref(message),
                None,
                0,
                0,
                _PM_REMOVE,
            ):
                count += 1
                hwnd = int(message.hwnd or 0)
                if message.message == _WM_QUIT or (message.message == _WM_CLOSE and hwnd == shell):
                    close_requested = True
                    continue
                if message.message == _WM_LBUTTONDOWN:
                    self._append_pointer_event(
                        kind=WindowsPointerEventKind.DOWN,
                        shell=shell,
                        hwnd=hwnd,
                        lparam=int(message.lParam),
                    )
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
                self._translate_message(ctypes.byref(message))
                self._dispatch_message(ctypes.byref(message))
        except _NativeShellError:
            raise
        except Exception:
            raise _NativeShellError(_NativeShellFailure.PUMP) from None
        return count, close_requested

    def drain_pointer_events(self, max_events: int) -> tuple[WindowsPointerEvent, ...]:
        if not 1 <= max_events <= _MAX_POINTER_EVENTS:
            raise _NativeShellError(_NativeShellFailure.PUMP)
        drained: list[WindowsPointerEvent] = []
        while self._pointer_events and len(drained) < max_events:
            drained.append(self._pointer_events.popleft())
        return tuple(drained)


class BoundedInteractiveWindowsOperatorApplication(BoundedWindowsOperatorApplication):
    """Visible operator application with bounded ephemeral pointer capture."""

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
        return typing.cast(tuple[WindowsPointerEvent, ...], events)
