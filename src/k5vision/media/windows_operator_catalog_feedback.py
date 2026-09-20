"""Ephemeral source-free outcome feedback for reusable-view commands."""

from __future__ import annotations

import contextlib
import ctypes
import enum
import typing

from k5vision.media.windows_operator_application import (
    WindowsOperatorApplicationError,
    WindowsOperatorApplicationErrorCode,
    WindowsOperatorApplicationState,
    _NativeShellBoundary,
    _NativeShellError,
    _NativeShellFailure,
)
from k5vision.media.windows_operator_catalog_commands import (
    WindowsOperatorCatalogCommand,
    WindowsOperatorCatalogCommandKind,
)
from k5vision.media.windows_operator_catalog_overlay import (
    BoundedOverlayCatalogUiWindowsOperatorControl,
    BoundedOverlayCatalogWindowsOperatorApplication,
    _OverlayCatalogWin32OperatorShellApi,
)
from k5vision.media.windows_operator_catalog_ui import (
    _MAX_NATIVE_CATALOG_COMMANDS,
    _WS_CHILD,
    _WS_VISIBLE,
)
from k5vision.media.windows_operator_control import (
    WindowsOperatorControlError,
    WindowsOperatorControlErrorCode,
)

_STATUS_X = 352
_STATUS_WIDTH = 92


class WindowsOperatorCatalogFeedback(enum.StrEnum):
    """Fixed source-free visible outcomes; never includes a view identifier."""

    READY = "ready"
    SAVED = "saved"
    APPLIED = "applied"
    DELETED = "deleted"
    REJECTED = "rejected"


_FEEDBACK_TEXT: dict[WindowsOperatorCatalogFeedback, str] = {
    WindowsOperatorCatalogFeedback.READY: "Ready",
    WindowsOperatorCatalogFeedback.SAVED: "Saved",
    WindowsOperatorCatalogFeedback.APPLIED: "Applied",
    WindowsOperatorCatalogFeedback.DELETED: "Deleted",
    WindowsOperatorCatalogFeedback.REJECTED: "Rejected",
}


def _feedback_for_command(
    kind: WindowsOperatorCatalogCommandKind,
) -> WindowsOperatorCatalogFeedback:
    if kind == WindowsOperatorCatalogCommandKind.SAVE:
        return WindowsOperatorCatalogFeedback.SAVED
    if kind == WindowsOperatorCatalogCommandKind.APPLY:
        return WindowsOperatorCatalogFeedback.APPLIED
    if kind == WindowsOperatorCatalogCommandKind.DELETE:
        return WindowsOperatorCatalogFeedback.DELETED
    raise WindowsOperatorControlError(
        WindowsOperatorControlErrorCode.INVALID_CONFIGURATION,
        "operator catalog command is invalid",
    )


class _FeedbackOverlayCatalogWin32OperatorShellApi(_OverlayCatalogWin32OperatorShellApi):
    """Add one fixed-text outcome child to the accepted overlap-safe controls."""

    def __init__(self) -> None:
        self._feedback_handle = 0
        super().__init__()
        try:
            self._set_window_text = self._user32.SetWindowTextW
            self._set_window_text.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
            self._set_window_text.restype = ctypes.c_int
        except Exception:
            raise _NativeShellError(_NativeShellFailure.LOAD) from None

    def create_shell(self, width: int, height: int) -> int:
        shell: int | None = None
        try:
            shell = super().create_shell(width, height)
            self._feedback_handle = self._create_child(
                shell=shell,
                class_name="STATIC",
                text=_FEEDBACK_TEXT[WindowsOperatorCatalogFeedback.READY],
                style=_WS_CHILD | _WS_VISIBLE,
                x=_STATUS_X,
                width=_STATUS_WIDTH,
                control_id=None,
            )
            self._raise_catalog_controls()
        except _NativeShellError:
            if shell is not None:
                with contextlib.suppress(_NativeShellError):
                    super().destroy_shell(shell)
            self._feedback_handle = 0
            raise
        return shell

    def set_catalog_feedback(self, feedback: WindowsOperatorCatalogFeedback) -> None:
        if not isinstance(feedback, WindowsOperatorCatalogFeedback) or self._feedback_handle == 0:
            raise _NativeShellError(_NativeShellFailure.PUMP)
        try:
            updated = bool(
                self._set_window_text(
                    ctypes.c_void_p(self._feedback_handle),
                    _FEEDBACK_TEXT[feedback],
                )
            )
        except Exception:
            raise _NativeShellError(_NativeShellFailure.PUMP) from None
        if not updated:
            raise _NativeShellError(_NativeShellFailure.PUMP)

    def destroy_shell(self, shell: int) -> None:
        self._feedback_handle = 0
        super().destroy_shell(shell)


class BoundedFeedbackCatalogWindowsOperatorApplication(
    BoundedOverlayCatalogWindowsOperatorApplication
):
    """Overlap-safe catalog application with ephemeral fixed-text outcomes."""

    def _ensure_native_api(self) -> _NativeShellBoundary:
        if self._native_api is not None:
            return self._native_api
        try:
            self._native_api = _FeedbackOverlayCatalogWin32OperatorShellApi()
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

    def set_catalog_feedback(self, feedback: WindowsOperatorCatalogFeedback) -> None:
        if not isinstance(feedback, WindowsOperatorCatalogFeedback):
            raise WindowsOperatorApplicationError(
                WindowsOperatorApplicationErrorCode.INVALID_CONFIGURATION,
                "operator catalog feedback is invalid",
            )
        native = self._native_api
        setter = None if native is None else getattr(native, "set_catalog_feedback", None)
        if not callable(setter):
            raise WindowsOperatorApplicationError(
                WindowsOperatorApplicationErrorCode.PUMP_FAILURE,
                "operator catalog feedback failed",
            )
        try:
            setter(feedback)
        except _NativeShellError:
            raise WindowsOperatorApplicationError(
                WindowsOperatorApplicationErrorCode.PUMP_FAILURE,
                "operator catalog feedback failed",
            ) from None


@typing.runtime_checkable
class _FeedbackCatalogApplicationBoundary(typing.Protocol):
    def drain_catalog_commands(
        self,
        *,
        max_commands: int = 16,
    ) -> tuple[WindowsOperatorCatalogCommand, ...]: ...

    def drain_catalog_rejections(self) -> int: ...

    def set_catalog_feedback(self, feedback: WindowsOperatorCatalogFeedback) -> None: ...


class BoundedFeedbackCatalogUiWindowsOperatorControl(
    BoundedOverlayCatalogUiWindowsOperatorControl
):
    """Show one fixed visible outcome after each accepted native catalog action."""

    def __init__(
        self,
        *,
        application_factory: typing.Callable[[], typing.Any] | None = None,
        **kwargs: typing.Any,
    ) -> None:
        super().__init__(
            application_factory=(
                application_factory or BoundedFeedbackCatalogWindowsOperatorApplication
            ),
            **kwargs,
        )

    def _drain_native_catalog_commands(self, application: typing.Any) -> None:
        if not isinstance(application, _FeedbackCatalogApplicationBoundary):
            raise WindowsOperatorControlError(
                WindowsOperatorControlErrorCode.APPLICATION_FAILURE,
                "operator catalog feedback is unavailable",
            )

        rejected = application.drain_catalog_rejections()
        if rejected:
            self._native_rejections += rejected
            application.set_catalog_feedback(WindowsOperatorCatalogFeedback.REJECTED)

        commands = application.drain_catalog_commands(max_commands=16)
        if len(commands) > _MAX_NATIVE_CATALOG_COMMANDS:
            raise WindowsOperatorControlError(
                WindowsOperatorControlErrorCode.APPLICATION_FAILURE,
                "operator catalog command input is invalid",
            )
        for command in commands:
            try:
                self.dispatch_catalog_command(command)
            except WindowsOperatorControlError:
                self._native_rejections += 1
                application.set_catalog_feedback(WindowsOperatorCatalogFeedback.REJECTED)
            else:
                self._native_commands += 1
                application.set_catalog_feedback(_feedback_for_command(command.kind))
