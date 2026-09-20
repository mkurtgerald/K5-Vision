"""Ephemeral source-free saved-view selector for reusable-view controls."""

from __future__ import annotations

import contextlib
import ctypes
import typing

from k5vision.media.windows_operator_application import (
    WindowsOperatorApplicationError,
    WindowsOperatorApplicationErrorCode,
    WindowsOperatorApplicationState,
    _NativeShellBoundary,
    _NativeShellError,
    _NativeShellFailure,
)
from k5vision.media.windows_operator_catalog_feedback import (
    BoundedFeedbackCatalogUiWindowsOperatorControl,
    BoundedFeedbackCatalogWindowsOperatorApplication,
    _FeedbackCatalogApplicationBoundary,
    _FeedbackOverlayCatalogWin32OperatorShellApi,
)
from k5vision.media.windows_operator_catalog_ui import (
    _BN_CLICKED,
    _MAX_NATIVE_CATALOG_COMMANDS,
    _WS_CHILD,
    _WS_VISIBLE,
)
from k5vision.media.windows_operator_control import (
    WindowsOperatorControlError,
    WindowsOperatorControlErrorCode,
)

_MAX_SELECTOR_VIEWS = 64
_PREVIOUS_BUTTON_ID = 0x5104
_NEXT_BUTTON_ID = 0x5105
_PREVIOUS_X = 452
_NEXT_X = 530
_SELECTOR_BUTTON_WIDTH = 70


def _validated_catalog_view_ids(view_ids: tuple[int, ...]) -> tuple[int, ...]:
    """Accept only canonical source-free catalog occupancy."""
    if (
        not isinstance(view_ids, tuple)
        or len(view_ids) > _MAX_SELECTOR_VIEWS
        or any(isinstance(view_id, bool) or not isinstance(view_id, int) for view_id in view_ids)
        or any(not 0 <= view_id < _MAX_SELECTOR_VIEWS for view_id in view_ids)
        or view_ids != tuple(sorted(set(view_ids)))
    ):
        raise ValueError("catalog selector view identifiers are invalid")
    return view_ids


class _SelectorFeedbackCatalogWin32OperatorShellApi(_FeedbackOverlayCatalogWin32OperatorShellApi):
    """Add bounded previous/next selection over source-free reusable view identifiers."""

    def __init__(self) -> None:
        self._selector_view_ids: tuple[int, ...] = ()
        self._selector_button_handles: dict[int, int] = {}
        super().__init__()

    def create_shell(self, width: int, height: int) -> int:
        shell: int | None = None
        try:
            shell = super().create_shell(width, height)
            for control_id, text, x in (
                (_PREVIOUS_BUTTON_ID, "Prev", _PREVIOUS_X),
                (_NEXT_BUTTON_ID, "Next", _NEXT_X),
            ):
                self._selector_button_handles[control_id] = self._create_child(
                    shell=shell,
                    class_name="BUTTON",
                    text=text,
                    style=_WS_CHILD | _WS_VISIBLE,
                    x=x,
                    width=_SELECTOR_BUTTON_WIDTH,
                    control_id=control_id,
                )
            self._raise_catalog_controls()
        except _NativeShellError:
            if shell is not None:
                with contextlib.suppress(_NativeShellError):
                    super().destroy_shell(shell)
            self._selector_view_ids = ()
            self._selector_button_handles.clear()
            raise
        return shell

    def set_catalog_view_ids(self, view_ids: tuple[int, ...]) -> None:
        try:
            self._selector_view_ids = _validated_catalog_view_ids(view_ids)
        except ValueError:
            raise _NativeShellError(_NativeShellFailure.PUMP) from None

    def _select_catalog_view(self, *, forward: bool) -> None:
        view_ids = self._selector_view_ids
        if not view_ids:
            self._catalog_rejections += 1
            return
        current = self._read_view_id()
        if current not in view_ids:
            selected = view_ids[0] if forward else view_ids[-1]
        else:
            index = view_ids.index(current)
            offset = 1 if forward else -1
            selected = view_ids[(index + offset) % len(view_ids)]
        try:
            updated = bool(
                self._set_window_text(ctypes.c_void_p(self._view_editor), str(selected))
            )
        except Exception:
            raise _NativeShellError(_NativeShellFailure.PUMP) from None
        if not updated:
            raise _NativeShellError(_NativeShellFailure.PUMP)

    def _consume_catalog_command_message(self, wparam: int, lparam: int) -> bool:
        control_id = wparam & 0xFFFF
        notification = (wparam >> 16) & 0xFFFF
        expected_handle = self._selector_button_handles.get(control_id)
        if expected_handle is not None:
            if notification != _BN_CLICKED or lparam != expected_handle:
                return False
            self._select_catalog_view(forward=control_id == _NEXT_BUTTON_ID)
            return True
        return super()._consume_catalog_command_message(wparam, lparam)

    def destroy_shell(self, shell: int) -> None:
        self._selector_view_ids = ()
        self._selector_button_handles.clear()
        super().destroy_shell(shell)


class BoundedSelectorCatalogWindowsOperatorApplication(
    BoundedFeedbackCatalogWindowsOperatorApplication
):
    """Feedback application with bounded source-free saved-view selector state."""

    def _ensure_native_api(self) -> _NativeShellBoundary:
        if self._native_api is not None:
            return self._native_api
        try:
            self._native_api = _SelectorFeedbackCatalogWin32OperatorShellApi()
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

    def set_catalog_view_ids(self, view_ids: tuple[int, ...]) -> None:
        try:
            canonical = _validated_catalog_view_ids(view_ids)
        except ValueError:
            raise WindowsOperatorApplicationError(
                WindowsOperatorApplicationErrorCode.INVALID_CONFIGURATION,
                "operator catalog selector state is invalid",
            ) from None
        native = self._native_api
        setter = None if native is None else getattr(native, "set_catalog_view_ids", None)
        if not callable(setter):
            raise WindowsOperatorApplicationError(
                WindowsOperatorApplicationErrorCode.PUMP_FAILURE,
                "operator catalog selector failed",
            )
        try:
            setter(canonical)
        except _NativeShellError:
            raise WindowsOperatorApplicationError(
                WindowsOperatorApplicationErrorCode.PUMP_FAILURE,
                "operator catalog selector failed",
            ) from None


@typing.runtime_checkable
class _SelectorCatalogApplicationBoundary(_FeedbackCatalogApplicationBoundary, typing.Protocol):
    def set_catalog_view_ids(self, view_ids: tuple[int, ...]) -> None: ...


class BoundedSelectorCatalogUiWindowsOperatorControl(
    BoundedFeedbackCatalogUiWindowsOperatorControl
):
    """Publish only catalog occupancy to the native previous/next selector."""

    def __init__(
        self,
        *,
        application_factory: typing.Callable[[], typing.Any] | None = None,
        **kwargs: typing.Any,
    ) -> None:
        super().__init__(
            application_factory=(
                application_factory or BoundedSelectorCatalogWindowsOperatorApplication
            ),
            **kwargs,
        )
        self._published_catalog_view_ids: tuple[int, ...] | None = None

    def _sync_catalog_selector(self, application: typing.Any) -> None:
        if not isinstance(application, _SelectorCatalogApplicationBoundary):
            raise WindowsOperatorControlError(
                WindowsOperatorControlErrorCode.APPLICATION_FAILURE,
                "operator catalog selector is unavailable",
            )
        view_ids = tuple(entry.view_id for entry in self._catalog.views)
        if len(view_ids) > _MAX_NATIVE_CATALOG_COMMANDS:
            raise WindowsOperatorControlError(
                WindowsOperatorControlErrorCode.CONTROL_LIMIT,
                "operator catalog selector exceeds limit",
            )
        if view_ids == self._published_catalog_view_ids:
            return
        try:
            application.set_catalog_view_ids(view_ids)
        except WindowsOperatorApplicationError:
            raise WindowsOperatorControlError(
                WindowsOperatorControlErrorCode.APPLICATION_FAILURE,
                "operator catalog selector failed",
            ) from None
        self._published_catalog_view_ids = view_ids

    def _drain_native_catalog_commands(self, application: typing.Any) -> None:
        self._sync_catalog_selector(application)
        super()._drain_native_catalog_commands(application)
        self._sync_catalog_selector(application)
