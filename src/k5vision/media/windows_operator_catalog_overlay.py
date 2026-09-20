"""Keep accepted reusable-view controls above arbitrary sibling presentation targets."""

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
from k5vision.media.windows_operator_catalog_ui import (
    BoundedCatalogUiWindowsOperatorControl,
    BoundedCatalogWindowsOperatorApplication,
    _CatalogWin32OperatorShellApi,
)

_HWND_TOP = 0
_SWP_NOSIZE = 0x0001
_SWP_NOMOVE = 0x0002
_SWP_NOACTIVATE = 0x0010
_MAX_CATALOG_Z_ORDER_CONTROLS = 8
_Z_ORDER_FLAGS = _SWP_NOSIZE | _SWP_NOMOVE | _SWP_NOACTIVATE


class _OverlayCatalogWin32OperatorShellApi(_CatalogWin32OperatorShellApi):
    """Keep the accepted command controls above arbitrary child presentation targets."""

    def __init__(self) -> None:
        self._catalog_z_order_handles: list[int] = []
        super().__init__()
        try:
            self._set_window_pos = self._user32.SetWindowPos
            self._set_window_pos.argtypes = [
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_uint32,
            ]
            self._set_window_pos.restype = ctypes.c_int
        except Exception:
            raise _NativeShellError(_NativeShellFailure.LOAD) from None

    def _create_child(self, **kwargs: typing.Any) -> int:
        if len(self._catalog_z_order_handles) >= _MAX_CATALOG_Z_ORDER_CONTROLS:
            raise _NativeShellError(_NativeShellFailure.CREATE)
        handle = super()._create_child(**kwargs)
        self._catalog_z_order_handles.append(handle)
        return handle

    def _raise_catalog_controls(self) -> None:
        if len(self._catalog_z_order_handles) > _MAX_CATALOG_Z_ORDER_CONTROLS:
            raise _NativeShellError(_NativeShellFailure.PUMP)
        try:
            for handle in self._catalog_z_order_handles:
                positioned = bool(
                    self._set_window_pos(
                        ctypes.c_void_p(handle),
                        ctypes.c_void_p(_HWND_TOP),
                        0,
                        0,
                        0,
                        0,
                        _Z_ORDER_FLAGS,
                    )
                )
                if not positioned:
                    raise _NativeShellError(_NativeShellFailure.PUMP)
        except _NativeShellError:
            raise
        except Exception:
            raise _NativeShellError(_NativeShellFailure.PUMP) from None

    def create_shell(self, width: int, height: int) -> int:
        shell: int | None = None
        try:
            shell = super().create_shell(width, height)
            self._raise_catalog_controls()
        except _NativeShellError:
            if shell is not None:
                with contextlib.suppress(_NativeShellError):
                    super().destroy_shell(shell)
            self._catalog_z_order_handles.clear()
            raise
        return shell

    def pump_messages(self, shell: int, max_messages: int) -> tuple[int, bool]:
        self._raise_catalog_controls()
        return super().pump_messages(shell, max_messages)

    def destroy_shell(self, shell: int) -> None:
        self._catalog_z_order_handles.clear()
        super().destroy_shell(shell)


class BoundedOverlayCatalogWindowsOperatorApplication(BoundedCatalogWindowsOperatorApplication):
    """Catalog application whose native controls remain above sibling video targets."""

    def _ensure_native_api(self) -> _NativeShellBoundary:
        if self._native_api is not None:
            return self._native_api
        try:
            self._native_api = _OverlayCatalogWin32OperatorShellApi()
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


class BoundedOverlayCatalogUiWindowsOperatorControl(BoundedCatalogUiWindowsOperatorControl):
    """Accepted catalog control using the overlap-safe native command surface."""

    def __init__(
        self,
        *,
        application_factory: typing.Callable[[], typing.Any] | None = None,
        **kwargs: typing.Any,
    ) -> None:
        super().__init__(
            application_factory=(
                application_factory or BoundedOverlayCatalogWindowsOperatorApplication
            ),
            **kwargs,
        )
