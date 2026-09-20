"""Visible bounded Win32 reusable-view controls over the accepted command contract."""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import typing
from collections import deque
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field

from k5vision.media.mixed_presentation import MixedPresentationStream
from k5vision.media.viewport_geometry import ViewportLayout
from k5vision.media.windows_operator_application import (
    WindowsOperatorApplicationError,
    WindowsOperatorApplicationErrorCode,
    WindowsOperatorApplicationState,
    _NativeShellBoundary,
    _NativeShellError,
    _NativeShellFailure,
    _Win32Message,
)
from k5vision.media.windows_operator_catalog_commands import (
    BoundedCommandWindowsOperatorControl,
    WindowsOperatorCatalogCommand,
    WindowsOperatorCatalogCommandKind,
    WindowsOperatorCatalogCommandSnapshot,
)
from k5vision.media.windows_operator_control import (
    WindowsOperatorControlError,
    WindowsOperatorControlSnapshot,
)
from k5vision.media.windows_operator_interaction import (
    _PM_REMOVE,
    _WM_CANCELMODE,
    _WM_CAPTURECHANGED,
    _WM_CLOSE,
    _WM_LBUTTONDOWN,
    _WM_LBUTTONUP,
    _WM_MOUSEMOVE,
    _WM_QUIT,
    BoundedInteractiveWindowsOperatorApplication,
    WindowsPointerEventKind,
    _InteractiveWin32OperatorShellApi,
)

_MAX_NATIVE_CATALOG_COMMANDS = 64
_WM_COMMAND = 0x0111
_BN_CLICKED = 0
_WS_CHILD = 0x40000000
_WS_VISIBLE = 0x10000000
_WS_BORDER = 0x00800000
_ES_NUMBER = 0x2000
_LABEL_X = 8
_EDITOR_X = 62
_SAVE_X = 118
_APPLY_X = 196
_DELETE_X = 274
_CONTROL_Y = 8
_LABEL_WIDTH = 48
_EDITOR_WIDTH = 48
_BUTTON_WIDTH = 70
_CONTROL_HEIGHT = 24
_VIEW_EDITOR_ID = 0x5100
_SAVE_BUTTON_ID = 0x5101
_APPLY_BUTTON_ID = 0x5102
_DELETE_BUTTON_ID = 0x5103


def _parse_view_id_text(text: str) -> int | None:
    """Parse only ASCII decimal logical view identifiers in the accepted range."""
    if not text or len(text) > 2 or any(character < "0" or character > "9" for character in text):
        return None
    value = int(text)
    return value if 0 <= value <= 63 else None


def _catalog_kind_for_control(control_id: int) -> WindowsOperatorCatalogCommandKind | None:
    if control_id == _SAVE_BUTTON_ID:
        return WindowsOperatorCatalogCommandKind.SAVE
    if control_id == _APPLY_BUTTON_ID:
        return WindowsOperatorCatalogCommandKind.APPLY
    if control_id == _DELETE_BUTTON_ID:
        return WindowsOperatorCatalogCommandKind.DELETE
    return None


class _CatalogWin32OperatorShellApi(_InteractiveWin32OperatorShellApi):
    """Add one small source-free reusable-view command surface to the operator shell."""

    def __init__(self) -> None:
        super().__init__()
        self._catalog_commands: deque[WindowsOperatorCatalogCommand] = deque()
        self._catalog_rejections = 0
        self._view_editor = 0
        self._ui_handles: set[int] = set()
        self._button_handles: dict[int, int] = {}
        try:
            self._get_window_text_length = self._user32.GetWindowTextLengthW
            self._get_window_text_length.argtypes = [ctypes.c_void_p]
            self._get_window_text_length.restype = ctypes.c_int

            self._get_window_text = self._user32.GetWindowTextW
            self._get_window_text.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_wchar),
                ctypes.c_int,
            ]
            self._get_window_text.restype = ctypes.c_int
        except Exception:
            raise _NativeShellError(_NativeShellFailure.LOAD) from None

    def _create_child(
        self,
        *,
        shell: int,
        class_name: str,
        text: str,
        style: int,
        x: int,
        width: int,
        control_id: int | None,
    ) -> int:
        try:
            instance = int(self._get_module_handle(None) or 0)
            handle = int(
                self._create_window(
                    0,
                    class_name,
                    text,
                    style,
                    x,
                    _CONTROL_Y,
                    width,
                    _CONTROL_HEIGHT,
                    ctypes.c_void_p(shell),
                    None if control_id is None else ctypes.c_void_p(control_id),
                    ctypes.c_void_p(instance),
                    None,
                )
                or 0
            )
        except Exception:
            raise _NativeShellError(_NativeShellFailure.CREATE) from None
        if handle == 0:
            raise _NativeShellError(_NativeShellFailure.CREATE)
        self._ui_handles.add(handle)
        return handle

    def create_shell(self, width: int, height: int) -> int:
        shell = super().create_shell(width, height)
        try:
            self._create_child(
                shell=shell,
                class_name="STATIC",
                text="View",
                style=_WS_CHILD | _WS_VISIBLE,
                x=_LABEL_X,
                width=_LABEL_WIDTH,
                control_id=None,
            )
            self._view_editor = self._create_child(
                shell=shell,
                class_name="EDIT",
                text="0",
                style=_WS_CHILD | _WS_VISIBLE | _WS_BORDER | _ES_NUMBER,
                x=_EDITOR_X,
                width=_EDITOR_WIDTH,
                control_id=_VIEW_EDITOR_ID,
            )
            for control_id, text, x in (
                (_SAVE_BUTTON_ID, "Save", _SAVE_X),
                (_APPLY_BUTTON_ID, "Apply", _APPLY_X),
                (_DELETE_BUTTON_ID, "Delete", _DELETE_X),
            ):
                self._button_handles[control_id] = self._create_child(
                    shell=shell,
                    class_name="BUTTON",
                    text=text,
                    style=_WS_CHILD | _WS_VISIBLE,
                    x=x,
                    width=_BUTTON_WIDTH,
                    control_id=control_id,
                )
        except _NativeShellError:
            with contextlib.suppress(_NativeShellError):
                super().destroy_shell(shell)
            self._view_editor = 0
            self._ui_handles.clear()
            self._button_handles.clear()
            raise
        return shell

    def _read_view_id(self) -> int | None:
        if self._view_editor == 0:
            raise _NativeShellError(_NativeShellFailure.PUMP)
        try:
            length = int(self._get_window_text_length(ctypes.c_void_p(self._view_editor)))
        except Exception:
            raise _NativeShellError(_NativeShellFailure.PUMP) from None
        if length <= 0 or length > 2:
            return None
        buffer = ctypes.create_unicode_buffer(length + 1)
        try:
            copied = int(
                self._get_window_text(
                    ctypes.c_void_p(self._view_editor),
                    buffer,
                    length + 1,
                )
            )
        except Exception:
            raise _NativeShellError(_NativeShellFailure.PUMP) from None
        if copied != length:
            raise _NativeShellError(_NativeShellFailure.PUMP)
        return _parse_view_id_text(buffer.value)

    def _consume_catalog_command_message(self, wparam: int, lparam: int) -> bool:
        control_id = wparam & 0xFFFF
        notification = (wparam >> 16) & 0xFFFF
        kind = _catalog_kind_for_control(control_id)
        expected_handle = self._button_handles.get(control_id)
        if (
            kind is None
            or notification != _BN_CLICKED
            or expected_handle is None
            or lparam != expected_handle
        ):
            return False

        view_id = self._read_view_id()
        if view_id is None:
            self._catalog_rejections += 1
            return True
        if len(self._catalog_commands) >= _MAX_NATIVE_CATALOG_COMMANDS:
            raise _NativeShellError(_NativeShellFailure.PUMP)
        self._catalog_commands.append(WindowsOperatorCatalogCommand(kind=kind, view_id=view_id))
        return True

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
                    self._cancel_pointer_capture(shell)
                    close_requested = True
                    continue
                if message.message == _WM_CANCELMODE:
                    self._cancel_pointer_capture(shell)
                    continue
                if message.message == _WM_CAPTURECHANGED:
                    self._capture_was_lost(shell)
                    continue
                if message.message == _WM_COMMAND and self._consume_catalog_command_message(
                    int(message.wParam),
                    int(message.lParam),
                ):
                    continue
                if hwnd in self._ui_handles:
                    self._translate_message(ctypes.byref(message))
                    self._dispatch_message(ctypes.byref(message))
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
                    self._release_pointer_capture()
                self._translate_message(ctypes.byref(message))
                self._dispatch_message(ctypes.byref(message))
        except _NativeShellError:
            raise
        except Exception:
            raise _NativeShellError(_NativeShellFailure.PUMP) from None
        return count, close_requested

    def drain_catalog_commands(
        self,
        max_commands: int,
    ) -> tuple[WindowsOperatorCatalogCommand, ...]:
        if not 1 <= max_commands <= _MAX_NATIVE_CATALOG_COMMANDS:
            raise _NativeShellError(_NativeShellFailure.PUMP)
        drained: list[WindowsOperatorCatalogCommand] = []
        while self._catalog_commands and len(drained) < max_commands:
            drained.append(self._catalog_commands.popleft())
        return tuple(drained)

    def drain_catalog_rejections(self) -> int:
        rejected = self._catalog_rejections
        self._catalog_rejections = 0
        return rejected

    def destroy_shell(self, shell: int) -> None:
        self._catalog_commands.clear()
        self._catalog_rejections = 0
        self._view_editor = 0
        self._ui_handles.clear()
        self._button_handles.clear()
        super().destroy_shell(shell)


class BoundedCatalogWindowsOperatorApplication(BoundedInteractiveWindowsOperatorApplication):
    """Interactive application exposing only bounded source-free reusable-view commands."""

    def _ensure_native_api(self) -> _NativeShellBoundary:
        if self._native_api is not None:
            return self._native_api
        try:
            self._native_api = _CatalogWin32OperatorShellApi()
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

    def drain_catalog_commands(
        self,
        *,
        max_commands: int = 16,
    ) -> tuple[WindowsOperatorCatalogCommand, ...]:
        if not 1 <= max_commands <= _MAX_NATIVE_CATALOG_COMMANDS:
            raise WindowsOperatorApplicationError(
                WindowsOperatorApplicationErrorCode.INVALID_CONFIGURATION,
                "operator catalog command batch is invalid",
            )
        native = self._native_api
        drain = None if native is None else getattr(native, "drain_catalog_commands", None)
        if not callable(drain):
            return ()
        try:
            commands = drain(max_commands)
        except _NativeShellError:
            raise WindowsOperatorApplicationError(
                WindowsOperatorApplicationErrorCode.PUMP_FAILURE,
                "operator catalog command input failed",
            ) from None
        if not isinstance(commands, tuple) or not all(
            isinstance(command, WindowsOperatorCatalogCommand) for command in commands
        ):
            raise WindowsOperatorApplicationError(
                WindowsOperatorApplicationErrorCode.PUMP_FAILURE,
                "operator catalog command input failed",
            )
        return typing.cast(tuple[WindowsOperatorCatalogCommand, ...], commands)

    def drain_catalog_rejections(self) -> int:
        native = self._native_api
        drain = None if native is None else getattr(native, "drain_catalog_rejections", None)
        if not callable(drain):
            return 0
        try:
            rejected = drain()
        except _NativeShellError:
            raise WindowsOperatorApplicationError(
                WindowsOperatorApplicationErrorCode.PUMP_FAILURE,
                "operator catalog command input failed",
            ) from None
        if isinstance(rejected, bool) or not isinstance(rejected, int) or rejected < 0:
            raise WindowsOperatorApplicationError(
                WindowsOperatorApplicationErrorCode.PUMP_FAILURE,
                "operator catalog command input failed",
            )
        return rejected


@typing.runtime_checkable
class _CatalogCommandApplicationBoundary(typing.Protocol):
    def drain_catalog_commands(
        self,
        *,
        max_commands: int = 16,
    ) -> tuple[WindowsOperatorCatalogCommand, ...]: ...

    def drain_catalog_rejections(self) -> int: ...


class WindowsOperatorCatalogUiSnapshot(BaseModel):
    """Aggregate native command-surface observability without command arguments."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: typing.Literal["1"] = "1"
    command: WindowsOperatorCatalogCommandSnapshot
    native_commands: int = Field(default=0, ge=0)
    native_rejections: int = Field(default=0, ge=0)


class BoundedCatalogUiWindowsOperatorControl(BoundedCommandWindowsOperatorControl):
    """Drain visible Win32 reusable-view commands into the accepted control lifecycle."""

    def __init__(
        self,
        *,
        application_factory: typing.Callable[[], typing.Any] | None = None,
        **kwargs: typing.Any,
    ) -> None:
        super().__init__(
            application_factory=application_factory or BoundedCatalogWindowsOperatorApplication,
            **kwargs,
        )
        self._native_commands = 0
        self._native_rejections = 0

    @property
    def catalog_ui_snapshot(self) -> WindowsOperatorCatalogUiSnapshot:
        return WindowsOperatorCatalogUiSnapshot(
            command=self.catalog_command_snapshot,
            native_commands=self._native_commands,
            native_rejections=self._native_rejections,
        )

    def _drain_native_catalog_commands(
        self, application: _CatalogCommandApplicationBoundary
    ) -> None:
        self._native_rejections += application.drain_catalog_rejections()
        for command in application.drain_catalog_commands(max_commands=16):
            try:
                self.dispatch_catalog_command(command)
            except WindowsOperatorControlError:
                self._native_rejections += 1
            else:
                self._native_commands += 1

    async def run(
        self,
        *,
        width: int,
        height: int,
        layout: ViewportLayout,
        streams: Sequence[MixedPresentationStream],
    ) -> WindowsOperatorControlSnapshot:
        """Run accepted control while draining native catalog commands between cycles."""
        control_task = asyncio.create_task(
            super().run(
                width=width,
                height=height,
                layout=layout,
                streams=streams,
            )
        )
        try:
            while not control_task.done():
                application = self._application
                if isinstance(application, _CatalogCommandApplicationBoundary):
                    self._drain_native_catalog_commands(application)
                await asyncio.sleep(
                    self._poll_interval_seconds if self._poll_interval_seconds else 0
                )
            return await control_task
        finally:
            if not control_task.done():
                control_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await control_task
