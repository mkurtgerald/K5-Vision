"""Bounded Win32 window target above the accepted presentation surface."""

from __future__ import annotations

import asyncio
import ctypes
import enum
import sys
import typing

from pydantic import BaseModel, ConfigDict, Field

_MAX_DIMENSION = 16_384
_MAX_PRESENTATIONS = 1_000_000
_WS_POPUP = 0x80000000


class WindowsPresentationTargetState(enum.StrEnum):
    READY = "ready"
    OPEN = "open"
    FAILED = "failed"
    CLOSED = "closed"


class WindowsPresentationTargetErrorCode(enum.StrEnum):
    INVALID_CONFIGURATION = "invalid_configuration"
    INVALID_STATE = "invalid_state"
    UNSUPPORTED_PLATFORM = "unsupported_platform"
    NATIVE_LOAD_FAILURE = "native_load_failure"
    TARGET_CREATE_FAILURE = "target_create_failure"
    TARGET_DC_FAILURE = "target_dc_failure"
    PRESENTATION_FAILURE = "presentation_failure"
    PRESENTATION_LIMIT = "presentation_limit"
    CLEANUP_FAILURE = "cleanup_failure"


class WindowsPresentationTargetError(RuntimeError):
    """Sanitized target failure that never exposes native handles."""

    def __init__(self, code: WindowsPresentationTargetErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class WindowsPresentationTargetSnapshot(BaseModel):
    """Handle/source/path/payload-free target observability."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: typing.Literal["1"] = "1"
    state: WindowsPresentationTargetState
    target_open: bool
    width: int = Field(ge=0, le=_MAX_DIMENSION)
    height: int = Field(ge=0, le=_MAX_DIMENSION)
    presentations: int = Field(ge=0, le=_MAX_PRESENTATIONS)


class _NativeTargetFailure(enum.StrEnum):
    UNSUPPORTED_PLATFORM = "unsupported_platform"
    LOAD = "load"
    CREATE = "create"
    ACQUIRE_DC = "acquire_dc"
    RELEASE_DC = "release_dc"
    DESTROY = "destroy"


class _NativeTargetError(RuntimeError):
    def __init__(self, failure: _NativeTargetFailure) -> None:
        super().__init__(failure.value)
        self.failure = failure


@typing.runtime_checkable
class _SurfaceBoundary(typing.Protocol):
    async def blit(self, target_dc: int) -> None: ...


@typing.runtime_checkable
class _NativeTargetBoundary(typing.Protocol):
    def create_target(self, width: int, height: int) -> int: ...

    def acquire_dc(self, target: int) -> int: ...

    def release_dc(self, target: int, target_dc: int) -> None: ...

    def destroy_target(self, target: int) -> None: ...


class _Win32WindowTargetApi:
    """Narrow Win32 boundary for one toolkit-free target window."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise _NativeTargetError(_NativeTargetFailure.UNSUPPORTED_PLATFORM)
        try:
            loader = ctypes.WinDLL
            self._user32 = loader("user32", use_last_error=True)
            self._kernel32 = loader("kernel32", use_last_error=True)

            self._get_module_handle = self._kernel32.GetModuleHandleW
            self._get_module_handle.argtypes = [ctypes.c_wchar_p]
            self._get_module_handle.restype = ctypes.c_void_p

            self._create_window = self._user32.CreateWindowExW
            self._create_window.argtypes = [
                ctypes.c_uint32,
                ctypes.c_wchar_p,
                ctypes.c_wchar_p,
                ctypes.c_uint32,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.c_void_p,
            ]
            self._create_window.restype = ctypes.c_void_p

            self._get_dc = self._user32.GetDC
            self._get_dc.argtypes = [ctypes.c_void_p]
            self._get_dc.restype = ctypes.c_void_p

            self._release_dc = self._user32.ReleaseDC
            self._release_dc.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            self._release_dc.restype = ctypes.c_int

            self._destroy_window = self._user32.DestroyWindow
            self._destroy_window.argtypes = [ctypes.c_void_p]
            self._destroy_window.restype = ctypes.c_int
        except Exception:
            raise _NativeTargetError(_NativeTargetFailure.LOAD) from None

    def create_target(self, width: int, height: int) -> int:
        try:
            instance = int(self._get_module_handle(None) or 0)
            target = int(
                self._create_window(
                    0,
                    "STATIC",
                    "",
                    _WS_POPUP,
                    0,
                    0,
                    width,
                    height,
                    None,
                    None,
                    ctypes.c_void_p(instance),
                    None,
                )
                or 0
            )
        except Exception:
            raise _NativeTargetError(_NativeTargetFailure.CREATE) from None
        if target == 0:
            raise _NativeTargetError(_NativeTargetFailure.CREATE)
        return target

    def acquire_dc(self, target: int) -> int:
        try:
            target_dc = int(self._get_dc(ctypes.c_void_p(target)) or 0)
        except Exception:
            raise _NativeTargetError(_NativeTargetFailure.ACQUIRE_DC) from None
        if target_dc == 0:
            raise _NativeTargetError(_NativeTargetFailure.ACQUIRE_DC)
        return target_dc

    def release_dc(self, target: int, target_dc: int) -> None:
        try:
            released = int(
                self._release_dc(
                    ctypes.c_void_p(target),
                    ctypes.c_void_p(target_dc),
                )
            )
        except Exception:
            raise _NativeTargetError(_NativeTargetFailure.RELEASE_DC) from None
        if released != 1:
            raise _NativeTargetError(_NativeTargetFailure.RELEASE_DC)

    def destroy_target(self, target: int) -> None:
        try:
            destroyed = bool(self._destroy_window(ctypes.c_void_p(target)))
        except Exception:
            raise _NativeTargetError(_NativeTargetFailure.DESTROY) from None
        if not destroyed:
            raise _NativeTargetError(_NativeTargetFailure.DESTROY)


class BoundedWindowsPresentationTarget:
    """Present an accepted surface into one bounded project-owned Win32 target."""

    def __init__(
        self,
        *,
        max_presentations: int = 100_000,
        native_api: _NativeTargetBoundary | None = None,
    ) -> None:
        if not 1 <= max_presentations <= _MAX_PRESENTATIONS:
            raise ValueError("max_presentations must be between 1 and 1000000")
        self._max_presentations = max_presentations
        self._native_api = native_api
        self._state = WindowsPresentationTargetState.READY
        self._target: int | None = None
        self._width = 0
        self._height = 0
        self._presentations = 0
        self._lock = asyncio.Lock()

    @property
    def snapshot(self) -> WindowsPresentationTargetSnapshot:
        return WindowsPresentationTargetSnapshot(
            state=self._state,
            target_open=self._target is not None,
            width=self._width,
            height=self._height,
            presentations=self._presentations,
        )

    def _destroy_target(self) -> None:
        target = self._target
        self._target = None
        self._width = 0
        self._height = 0
        if target is None:
            return
        if self._native_api is None:
            raise _NativeTargetError(_NativeTargetFailure.DESTROY)
        self._native_api.destroy_target(target)

    def _fail_and_destroy(self) -> None:
        self._state = WindowsPresentationTargetState.FAILED
        try:
            self._destroy_target()
        except _NativeTargetError:
            self._target = None
            self._width = 0
            self._height = 0

    async def open(self, width: int, height: int) -> WindowsPresentationTargetSnapshot:
        """Create one bounded native target without exposing its handle."""
        async with self._lock:
            if self._state == WindowsPresentationTargetState.OPEN:
                if (width, height) == (self._width, self._height):
                    return self.snapshot
                raise WindowsPresentationTargetError(
                    WindowsPresentationTargetErrorCode.INVALID_STATE,
                    "presentation target is already open",
                )
            if self._state != WindowsPresentationTargetState.READY:
                raise WindowsPresentationTargetError(
                    WindowsPresentationTargetErrorCode.INVALID_STATE,
                    "presentation target cannot open from current state",
                )
            if (
                isinstance(width, bool)
                or isinstance(height, bool)
                or not isinstance(width, int)
                or not isinstance(height, int)
                or not 1 <= width <= _MAX_DIMENSION
                or not 1 <= height <= _MAX_DIMENSION
            ):
                raise WindowsPresentationTargetError(
                    WindowsPresentationTargetErrorCode.INVALID_CONFIGURATION,
                    "presentation target geometry is invalid",
                )

            if self._native_api is None:
                try:
                    self._native_api = _Win32WindowTargetApi()
                except _NativeTargetError as exc:
                    self._state = WindowsPresentationTargetState.FAILED
                    if exc.failure == _NativeTargetFailure.UNSUPPORTED_PLATFORM:
                        code = WindowsPresentationTargetErrorCode.UNSUPPORTED_PLATFORM
                        message = "Windows presentation target requires Win32"
                    else:
                        code = WindowsPresentationTargetErrorCode.NATIVE_LOAD_FAILURE
                        message = "Windows presentation target native API is unavailable"
                    raise WindowsPresentationTargetError(code, message) from None

            try:
                target = self._native_api.create_target(width, height)
            except _NativeTargetError:
                self._state = WindowsPresentationTargetState.FAILED
                raise WindowsPresentationTargetError(
                    WindowsPresentationTargetErrorCode.TARGET_CREATE_FAILURE,
                    "presentation target creation failed",
                ) from None

            self._target = target
            self._width = width
            self._height = height
            self._state = WindowsPresentationTargetState.OPEN
            return self.snapshot

    async def present(self, surface: _SurfaceBoundary) -> WindowsPresentationTargetSnapshot:
        """Blit one accepted surface into the native target and release the target DC."""
        async with self._lock:
            if self._state != WindowsPresentationTargetState.OPEN or self._target is None:
                raise WindowsPresentationTargetError(
                    WindowsPresentationTargetErrorCode.INVALID_STATE,
                    "presentation target is not open",
                )
            if not isinstance(surface, _SurfaceBoundary):
                raise WindowsPresentationTargetError(
                    WindowsPresentationTargetErrorCode.INVALID_CONFIGURATION,
                    "presentation surface boundary is invalid",
                )
            if self._presentations >= self._max_presentations:
                self._fail_and_destroy()
                raise WindowsPresentationTargetError(
                    WindowsPresentationTargetErrorCode.PRESENTATION_LIMIT,
                    "presentation target operation limit exceeded",
                )
            if self._native_api is None:
                self._fail_and_destroy()
                raise WindowsPresentationTargetError(
                    WindowsPresentationTargetErrorCode.NATIVE_LOAD_FAILURE,
                    "Windows presentation target native API is unavailable",
                )

            try:
                target_dc = self._native_api.acquire_dc(self._target)
            except _NativeTargetError:
                self._fail_and_destroy()
                raise WindowsPresentationTargetError(
                    WindowsPresentationTargetErrorCode.TARGET_DC_FAILURE,
                    "presentation target device context is unavailable",
                ) from None

            presentation_error: BaseException | None = None
            try:
                await surface.blit(target_dc)
            except BaseException as exc:
                presentation_error = exc

            release_error = False
            try:
                self._native_api.release_dc(self._target, target_dc)
            except _NativeTargetError:
                release_error = True

            if release_error:
                self._fail_and_destroy()
                raise WindowsPresentationTargetError(
                    WindowsPresentationTargetErrorCode.CLEANUP_FAILURE,
                    "presentation target device context release failed",
                ) from None

            if presentation_error is not None:
                self._fail_and_destroy()
                if isinstance(presentation_error, asyncio.CancelledError):
                    raise presentation_error
                raise WindowsPresentationTargetError(
                    WindowsPresentationTargetErrorCode.PRESENTATION_FAILURE,
                    "presentation target operation failed",
                ) from None

            self._presentations += 1
            return self.snapshot

    async def close(self) -> WindowsPresentationTargetSnapshot:
        """Release the target deterministically."""
        async with self._lock:
            if self._state == WindowsPresentationTargetState.CLOSED:
                return self.snapshot
            try:
                self._destroy_target()
            except _NativeTargetError:
                self._state = WindowsPresentationTargetState.FAILED
                raise WindowsPresentationTargetError(
                    WindowsPresentationTargetErrorCode.CLEANUP_FAILURE,
                    "presentation target cleanup failed",
                ) from None
            self._native_api = None
            self._state = WindowsPresentationTargetState.CLOSED
            return self.snapshot
