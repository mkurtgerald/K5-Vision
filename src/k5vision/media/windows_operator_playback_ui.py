"""Bounded source-free native playback commands over the accepted operator shell."""

from __future__ import annotations

import ctypes
import enum
import typing
from collections import deque

from k5vision.media.playback_control import PlaybackControlState, PlaybackPauseControl
from k5vision.media.windows_operator_application import (
    WindowsOperatorApplicationError,
    WindowsOperatorApplicationErrorCode,
    WindowsOperatorApplicationState,
    _NativeShellBoundary,
    _NativeShellError,
    _NativeShellFailure,
    _Win32Message,
)
from k5vision.media.windows_operator_control import BoundedWindowsOperatorControl
from k5vision.media.windows_operator_interaction import (
    BoundedInteractiveWindowsOperatorApplication,
    _InteractiveWin32OperatorShellApi,
)
from k5vision.media.windows_operator_session import WindowsOperatorSessionState
from k5vision.media.windows_operator_window_procedure import (
    OwnedWindowProcedure,
    WindowsOperatorWindowProcedureError,
)

_WM_CLOSE = 0x0010
_WM_COMMAND = 0x0111
_MAX_COMMANDS = 64
_PAUSE_BUTTON_ID = 0x5301
_RESUME_BUTTON_ID = 0x5302
_STOP_BUTTON_ID = 0x5303


class WindowsPlaybackCommand(enum.StrEnum):
    PAUSE = "pause"
    RESUME = "resume"
    STOP = "stop"


_BUTTONS = (
    (_PAUSE_BUTTON_ID, "Pause", WindowsPlaybackCommand.PAUSE),
    (_RESUME_BUTTON_ID, "Resume", WindowsPlaybackCommand.RESUME),
    (_STOP_BUTTON_ID, "Stop", WindowsPlaybackCommand.STOP),
)


class _PlaybackWin32OperatorShellApi(_InteractiveWin32OperatorShellApi):
    """Only record native input here; the existing async owner applies commands."""

    def __init__(self) -> None:
        super().__init__()
        self._playback_commands: deque[WindowsPlaybackCommand] = deque()
        self._button_handles: dict[int, int] = {}
        self._window_procedure: OwnedWindowProcedure | None = None
        self._shell_registration: object | None = None
        self._close_requested = False
        self._display_state: PlaybackControlState | None = None
        try:
            self._enable_window = self._user32.EnableWindow
            self._enable_window.argtypes = [ctypes.c_void_p, ctypes.c_int]
            self._enable_window.restype = ctypes.c_int
        except Exception:
            raise _NativeShellError(_NativeShellFailure.LOAD) from None

    def _receive_window_message(self, hwnd: int, message: int, wparam: int, lparam: int):
        # The callback boundary validates exact HWND, creator thread and route
        # generation before reaching this handler. No media or async work here.
        if message == _WM_CLOSE:
            self._close_requested = True
            return 0
        if message == _WM_COMMAND:
            control_id = wparam & 0xFFFF
            expected = self._button_handles.get(control_id)
            if (
                not self._close_requested
                and (wparam >> 16) & 0xFFFF == 0  # BN_CLICKED, never menu/accelerator
                and expected is not None
                and int(ctypes.c_void_p(lparam).value or 0) == expected
            ):
                if len(self._playback_commands) >= _MAX_COMMANDS:
                    raise _NativeShellError(_NativeShellFailure.PUMP)
                for identifier, _label, command in _BUTTONS:
                    if identifier == control_id:
                        self._playback_commands.append(command)
                        break
            # Unknown, forged, stale, or post-close command input is inert.
            return 0
        native_message = _Win32Message()
        native_message.hwnd = hwnd
        native_message.message = message
        native_message.wParam = wparam
        native_message.lParam = lparam
        self._note_geometry_message(hwnd, native_message)
        return None

    def create_shell(self, width: int, height: int) -> int:
        if self._shell_registration is not None or self._window_procedure is not None:
            raise _NativeShellError(_NativeShellFailure.CREATE)
        shell = super().create_shell(width, height)
        self._shell_registration = self._message_route.registration(shell)
        self._close_requested = False
        self._display_state = None
        try:
            procedure = OwnedWindowProcedure(
                self._user32, shell, self._message_route, self._receive_window_message
            )
            self._window_procedure = procedure
            procedure.install()
            self._require_callback(shell)
            instance = int(self._get_module_handle(None) or 0)
            for index, (identifier, label, _command) in enumerate(_BUTTONS):
                handle = int(
                    self._create_window(
                        0,
                        "BUTTON",
                        label,
                        0x40000000 | 0x10000000 | 0x00010000,
                        8 + index * 88,
                        8,
                        80,
                        24,
                        ctypes.c_void_p(shell),
                        ctypes.c_void_p(identifier),
                        ctypes.c_void_p(instance),
                        None,
                    )
                    or 0
                )
                self._require_callback(shell)
                if not handle:
                    raise _NativeShellError(_NativeShellFailure.CREATE)
                self._button_handles[identifier] = handle
            self.set_playback_state(shell, PlaybackControlState.RUNNING)
        except Exception:
            try:
                self.destroy_shell(shell)
            except _NativeShellError:
                pass
            raise _NativeShellError(_NativeShellFailure.CREATE) from None
        return shell

    def _require_callback(self, shell: int) -> OwnedWindowProcedure:
        self._message_route.require(shell, registration=self._shell_registration)
        if self._shell_registration is None:
            raise _NativeShellError(_NativeShellFailure.PUMP)
        procedure = self._window_procedure
        if procedure is None or procedure.failed or procedure.destroyed:
            raise _NativeShellError(_NativeShellFailure.PUMP)
        return procedure

    def _thread_quit_requested(self, shell: int) -> bool:
        self._require_callback(shell)
        return super()._thread_quit_requested(shell) or self._close_requested

    def _pointer_message_for_shell(self, shell: int, message: _Win32Message) -> bool:
        # Actual BUTTON mouse messages must be dispatched to their own procedure,
        # which synchronously notifies our parent callback with BN_CLICKED.
        if int(message.hwnd or 0) in self._button_handles.values():
            return False
        return super()._pointer_message_for_shell(shell, message)

    def set_playback_state(self, shell: int, state: PlaybackControlState) -> None:
        try:
            self._require_callback(shell)
            if not isinstance(state, PlaybackControlState):
                raise ValueError("invalid playback state")
            if state is self._display_state:
                return
            for identifier, _label, command in _BUTTONS:
                enabled = (
                    command is WindowsPlaybackCommand.STOP
                    or (
                        command is WindowsPlaybackCommand.PAUSE
                        and state is PlaybackControlState.RUNNING
                    )
                    or (
                        command is WindowsPlaybackCommand.RESUME
                        and state is PlaybackControlState.PAUSED
                    )
                )
                self._enable_window(ctypes.c_void_p(self._button_handles[identifier]), int(enabled))
                self._require_callback(shell)
            self._display_state = state
        except Exception:
            raise _NativeShellError(_NativeShellFailure.PUMP) from None

    def drain_playback_commands(self, max_commands: int) -> tuple[WindowsPlaybackCommand, ...]:
        if type(max_commands) is not int or not 1 <= max_commands <= _MAX_COMMANDS:
            raise _NativeShellError(_NativeShellFailure.PUMP)
        drained = []
        while self._playback_commands and len(drained) < max_commands:
            drained.append(self._playback_commands.popleft())
        return tuple(drained)

    def destroy_shell(self, shell: int) -> None:
        procedure = self._window_procedure
        if procedure is not None and procedure.destroyed:
            # WM_NCDESTROY already retired the exact route. Release the callback
            # outside its frame, and never touch a HWND that could now be reused.
            try:
                procedure.detach()
            except WindowsOperatorWindowProcedureError:
                raise _NativeShellError(_NativeShellFailure.DESTROY) from None
            self._playback_commands.clear()
            self._button_handles.clear()
            self._window_procedure = None
            self._shell_registration = None
            raise _NativeShellError(_NativeShellFailure.DESTROY)
        try:
            if self._shell_registration is None:
                raise ValueError("missing shell registration")
            self._message_route.require(shell, registration=self._shell_registration)
        except Exception:
            raise _NativeShellError(_NativeShellFailure.DESTROY) from None
        detach_failed = False
        if procedure is not None:
            try:
                procedure.detach()
            except WindowsOperatorWindowProcedureError:
                # Destroy our own HWND even if restoring its procedure failed.
                # The callback remains pinned until its native lifetime ends.
                detach_failed = True
        try:
            # Restoring a procedure is callback-capable too. A sent callback
            # must not make fallback destruction target a later registration.
            self._message_route.require(shell, registration=self._shell_registration)
        except Exception:
            if procedure is not None and procedure.destroyed:
                try:
                    procedure.detach()
                except WindowsOperatorWindowProcedureError:
                    pass
                else:
                    self._window_procedure = None
                    self._shell_registration = None
            raise _NativeShellError(_NativeShellFailure.DESTROY) from None
        self._playback_commands.clear()
        self._button_handles.clear()
        self._display_state = None
        try:
            super().destroy_shell(shell)
        finally:
            if procedure is not None and procedure.destroyed:
                # Destruction with a still-installed callback retires the route
                # before base cleanup can unregister it. Preserve that failure,
                # but release its lifetime pin after the native call has returned.
                try:
                    procedure.detach()
                except WindowsOperatorWindowProcedureError:
                    detach_failed = True
                else:
                    self._window_procedure = None
                    self._shell_registration = None
        self._window_procedure = None
        self._shell_registration = None
        if detach_failed:
            raise _NativeShellError(_NativeShellFailure.DESTROY)


class BoundedPlaybackWindowsOperatorApplication(BoundedInteractiveWindowsOperatorApplication):
    """Interactive two-tile shell with a reserved 40px playback toolbar."""

    _minimum_client_width = 272
    _minimum_client_height = 232
    _content_top = 40

    def __init__(self, *, pause_control: PlaybackPauseControl, **kwargs: typing.Any) -> None:
        if not isinstance(pause_control, PlaybackPauseControl):
            raise TypeError("pause_control must be a PlaybackPauseControl")
        super().__init__(**kwargs)
        self._pause_control = pause_control

    def _ensure_native_api(self) -> _NativeShellBoundary:
        if self._native_api is not None:
            return self._native_api
        try:
            self._native_api = _PlaybackWin32OperatorShellApi()
        except _NativeShellError as exc:
            self._state = WindowsOperatorApplicationState.FAILED
            code = (
                WindowsOperatorApplicationErrorCode.UNSUPPORTED_PLATFORM
                if exc.failure == _NativeShellFailure.UNSUPPORTED_PLATFORM
                else WindowsOperatorApplicationErrorCode.NATIVE_LOAD_FAILURE
            )
            raise WindowsOperatorApplicationError(
                code, "operator playback shell is unavailable"
            ) from None
        return self._native_api

    def refresh_playback_state(self) -> None:
        native, shell = self._native_api, self._shell
        update = getattr(native, "set_playback_state", None)
        if shell is not None and callable(update):
            try:
                update(shell, self._pause_control.snapshot.state)
            except Exception:
                raise WindowsOperatorApplicationError(
                    WindowsOperatorApplicationErrorCode.PUMP_FAILURE,
                    "operator playback controls are unavailable",
                ) from None

    def _refresh_native_chrome(self) -> None:
        super()._refresh_native_chrome()
        self.refresh_playback_state()

    def drain_playback_commands(self, *, max_commands: int) -> tuple[WindowsPlaybackCommand, ...]:
        native = self._native_api
        drain = getattr(native, "drain_playback_commands", None)
        try:
            if not callable(drain):
                raise ValueError("missing playback command boundary")
            commands = drain(max_commands)
            if not isinstance(commands, tuple) or not all(
                isinstance(command, WindowsPlaybackCommand) for command in commands
            ):
                raise ValueError("invalid playback command")
            return commands
        except Exception:
            raise WindowsOperatorApplicationError(
                WindowsOperatorApplicationErrorCode.PUMP_FAILURE,
                "operator playback controls are unavailable",
            ) from None


@typing.runtime_checkable
class _PlaybackApplicationBoundary(typing.Protocol):
    def drain_playback_commands(
        self, *, max_commands: int
    ) -> tuple[WindowsPlaybackCommand, ...]: ...
    def refresh_playback_state(self) -> None: ...


class BoundedPlaybackWindowsOperatorControl(BoundedWindowsOperatorControl):
    """Serialize native commands on the existing control loop and exact pause gate."""

    def __init__(
        self, *, pause_control: PlaybackPauseControl, application_factory=None, **kwargs: typing.Any
    ) -> None:
        if not isinstance(pause_control, PlaybackPauseControl):
            raise TypeError("pause_control must be a PlaybackPauseControl")
        super().__init__(
            application_factory=application_factory
            or (lambda: BoundedPlaybackWindowsOperatorApplication(pause_control=pause_control)),
            **kwargs,
        )
        self._pause_control = pause_control

    async def _process_controls(self, application, wait_task):
        if not isinstance(application, _PlaybackApplicationBoundary):
            raise WindowsOperatorApplicationError(
                WindowsOperatorApplicationErrorCode.CONTROL_FAILURE,
                "operator playback application cannot receive commands",
            )
        # Completion wins over stale commands remaining after the final frame.
        if wait_task is None or not wait_task.done():
            for command in application.drain_playback_commands(
                max_commands=self._max_controls_per_cycle
            ):
                if command is WindowsPlaybackCommand.STOP:
                    self.request_stop()
                    break
                before = self._pause_control.snapshot.state
                if command is WindowsPlaybackCommand.PAUSE:
                    await self._pause_control.pause()
                elif command is WindowsPlaybackCommand.RESUME:
                    await self._pause_control.resume()
                else:
                    raise ValueError("invalid playback command")
                if self._pause_control.snapshot.state is not before:
                    self._processed_controls += 1
        application.refresh_playback_state()
        wait_task, terminal = await super()._process_controls(application, wait_task)
        if terminal is WindowsOperatorSessionState.COMPLETE and self._stop_requests:
            terminal = WindowsOperatorSessionState.USER_CLOSED
        return wait_task, terminal
