"""Bounded visible Win32 application shell above the accepted operator host."""

from __future__ import annotations

import asyncio
import ctypes
import enum
import sys
import typing
from collections.abc import Callable, Sequence

from pydantic import BaseModel, ConfigDict, Field

from k5vision.media.mixed_presentation import MixedPresentationStream
from k5vision.media.viewport_geometry import ViewportLayout
from k5vision.media.windows_operator_host import (
    BoundedWindowsOperatorHost,
    WindowsOperatorHostSnapshot,
    WindowsOperatorHostState,
)
from k5vision.media.windows_operator_runtime import BoundedWindowsOperatorRuntime
from k5vision.media.windows_presentation_target import BoundedWindowsPresentationTarget
from k5vision.media.windows_viewport_layout import BoundedWindowsViewportLayout
from k5vision.media.windows_viewport_runtime import BoundedWindowsViewportRuntime

_MAX_DIMENSION = 16_384
_MAX_PUMP_MESSAGES = 256
_MAX_PUMP_CYCLES = 1_000_000
_WS_OVERLAPPEDWINDOW = 0x00CF0000
_WS_VISIBLE = 0x10000000
_PM_REMOVE = 0x0001
_WM_CLOSE = 0x0010
_WM_QUIT = 0x0012


class WindowsOperatorApplicationState(enum.StrEnum):
    READY = "ready"
    OPEN = "open"
    RUNNING = "running"
    COMPLETE = "complete"
    STOPPED = "stopped"
    FAILED = "failed"
    CLOSED = "closed"


class WindowsOperatorApplicationErrorCode(enum.StrEnum):
    INVALID_CONFIGURATION = "invalid_configuration"
    INVALID_STATE = "invalid_state"
    UNSUPPORTED_PLATFORM = "unsupported_platform"
    NATIVE_LOAD_FAILURE = "native_load_failure"
    SHELL_CREATE_FAILURE = "shell_create_failure"
    HOST_ASSEMBLY_FAILURE = "host_assembly_failure"
    START_FAILURE = "start_failure"
    EXECUTION_FAILURE = "execution_failure"
    CONTROL_FAILURE = "control_failure"
    PUMP_FAILURE = "pump_failure"
    PUMP_LIMIT = "pump_limit"
    CLEANUP_FAILURE = "cleanup_failure"


class WindowsOperatorApplicationError(RuntimeError):
    """Sanitized application-shell failure with no retained native identity."""

    def __init__(
        self,
        code: WindowsOperatorApplicationErrorCode,
        message: str,
    ) -> None:
        super().__init__(message)
        self.code = code


class WindowsOperatorApplicationSnapshot(BaseModel):
    """Source/path/media/native-handle-free application observability."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: typing.Literal["1"] = "1"
    state: WindowsOperatorApplicationState
    shell_open: bool
    pump_cycles: int = Field(ge=0, le=_MAX_PUMP_CYCLES)
    pumped_messages: int = Field(ge=0)
    generation: int = Field(ge=0)
    viewport_count: int = Field(ge=0)
    open_surface_count: int = Field(ge=0)
    delivered_frames: int = Field(ge=0)
    presentations: int = Field(ge=0)


class _NativeShellFailure(enum.StrEnum):
    UNSUPPORTED_PLATFORM = "unsupported_platform"
    LOAD = "load"
    CREATE = "create"
    PUMP = "pump"
    DESTROY = "destroy"


class _NativeShellError(RuntimeError):
    def __init__(self, failure: _NativeShellFailure) -> None:
        super().__init__(failure.value)
        self.failure = failure


class _Win32Point(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _Win32Message(ctypes.Structure):
    _fields_ = [
        ("hwnd", ctypes.c_void_p),
        ("message", ctypes.c_uint32),
        ("wParam", ctypes.c_size_t),
        ("lParam", ctypes.c_ssize_t),
        ("time", ctypes.c_uint32),
        ("pt", _Win32Point),
        ("lPrivate", ctypes.c_uint32),
    ]


@typing.runtime_checkable
class _OperatorHostBoundary(typing.Protocol):
    @property
    def snapshot(self) -> WindowsOperatorHostSnapshot: ...

    async def start(
        self,
        layout: ViewportLayout,
        streams: Sequence[MixedPresentationStream],
    ) -> WindowsOperatorHostSnapshot: ...

    async def replace(
        self,
        layout: ViewportLayout,
        streams: Sequence[MixedPresentationStream],
    ) -> WindowsOperatorHostSnapshot: ...

    async def wait(self) -> WindowsOperatorHostSnapshot: ...

    async def stop(self) -> WindowsOperatorHostSnapshot: ...

    async def close(self) -> WindowsOperatorHostSnapshot: ...


@typing.runtime_checkable
class _RelayoutOperatorHostBoundary(typing.Protocol):
    async def relayout(self, layout: ViewportLayout) -> WindowsOperatorHostSnapshot: ...


@typing.runtime_checkable
class _NativeShellBoundary(typing.Protocol):
    def create_shell(self, width: int, height: int) -> int: ...

    def pump_messages(self, shell: int, max_messages: int) -> tuple[int, bool]: ...

    def destroy_shell(self, shell: int) -> None: ...


OperatorHostFactory = Callable[[int], _OperatorHostBoundary]


class _Win32OperatorShellApi:
    """Small ctypes boundary for one visible top-level Win32 shell."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise _NativeShellError(_NativeShellFailure.UNSUPPORTED_PLATFORM)
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

            self._peek_message = self._user32.PeekMessageW
            self._peek_message.argtypes = [
                ctypes.POINTER(_Win32Message),
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.c_uint32,
                ctypes.c_uint32,
            ]
            self._peek_message.restype = ctypes.c_int

            self._translate_message = self._user32.TranslateMessage
            self._translate_message.argtypes = [ctypes.POINTER(_Win32Message)]
            self._translate_message.restype = ctypes.c_int

            self._dispatch_message = self._user32.DispatchMessageW
            self._dispatch_message.argtypes = [ctypes.POINTER(_Win32Message)]
            self._dispatch_message.restype = ctypes.c_ssize_t

            self._destroy_window = self._user32.DestroyWindow
            self._destroy_window.argtypes = [ctypes.c_void_p]
            self._destroy_window.restype = ctypes.c_int
        except Exception:
            raise _NativeShellError(_NativeShellFailure.LOAD) from None

    def create_shell(self, width: int, height: int) -> int:
        try:
            instance = int(self._get_module_handle(None) or 0)
            shell = int(
                self._create_window(
                    0,
                    "STATIC",
                    "K5 Vision",
                    _WS_OVERLAPPEDWINDOW | _WS_VISIBLE,
                    100,
                    100,
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
            raise _NativeShellError(_NativeShellFailure.CREATE) from None
        if shell == 0:
            raise _NativeShellError(_NativeShellFailure.CREATE)
        return shell

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
                self._translate_message(ctypes.byref(message))
                self._dispatch_message(ctypes.byref(message))
        except Exception:
            raise _NativeShellError(_NativeShellFailure.PUMP) from None
        return count, close_requested

    def destroy_shell(self, shell: int) -> None:
        try:
            destroyed = bool(self._destroy_window(ctypes.c_void_p(shell)))
        except Exception:
            raise _NativeShellError(_NativeShellFailure.DESTROY) from None
        if not destroyed:
            raise _NativeShellError(_NativeShellFailure.DESTROY)


def _default_host_factory(parent_handle: int) -> BoundedWindowsOperatorHost:
    def target_factory() -> BoundedWindowsPresentationTarget:
        return BoundedWindowsPresentationTarget(parent_handle=parent_handle)

    def layout_factory(layout: ViewportLayout) -> BoundedWindowsViewportLayout:
        return BoundedWindowsViewportLayout(layout, target_factory=target_factory)

    def windows_runtime_factory(layout: ViewportLayout) -> BoundedWindowsViewportRuntime:
        return BoundedWindowsViewportRuntime(layout, layout_factory=layout_factory)

    def operator_runtime_factory(layout: ViewportLayout) -> BoundedWindowsOperatorRuntime:
        return BoundedWindowsOperatorRuntime(
            layout,
            windows_runtime_factory=windows_runtime_factory,
        )

    return BoundedWindowsOperatorHost(runtime_factory=operator_runtime_factory)


class BoundedWindowsOperatorApplication:
    """Own one visible shell and an accepted re-entrant operator host beneath it."""

    def __init__(
        self,
        *,
        host_factory: OperatorHostFactory | None = None,
        native_api: _NativeShellBoundary | None = None,
        max_pump_cycles: int = 100_000,
        cleanup_timeout_seconds: float = 5.0,
    ) -> None:
        selected_factory = host_factory or _default_host_factory
        if not callable(selected_factory):
            raise WindowsOperatorApplicationError(
                WindowsOperatorApplicationErrorCode.INVALID_CONFIGURATION,
                "operator application host factory is invalid",
            )
        if not 1 <= max_pump_cycles <= _MAX_PUMP_CYCLES:
            raise WindowsOperatorApplicationError(
                WindowsOperatorApplicationErrorCode.INVALID_CONFIGURATION,
                "operator application pump bound is invalid",
            )
        if not 0.1 <= cleanup_timeout_seconds <= 30.0:
            raise WindowsOperatorApplicationError(
                WindowsOperatorApplicationErrorCode.INVALID_CONFIGURATION,
                "operator application cleanup timeout is invalid",
            )
        self._host_factory = selected_factory
        self._native_api = native_api
        self._max_pump_cycles = max_pump_cycles
        self._cleanup_timeout_seconds = cleanup_timeout_seconds
        self._state = WindowsOperatorApplicationState.READY
        self._shell: int | None = None
        self._host: _OperatorHostBoundary | None = None
        self._host_snapshot: WindowsOperatorHostSnapshot | None = None
        self._pump_cycles = 0
        self._pumped_messages = 0
        self._lock = asyncio.Lock()

    @property
    def snapshot(self) -> WindowsOperatorApplicationSnapshot:
        host = self._host.snapshot if self._host is not None else self._host_snapshot
        return WindowsOperatorApplicationSnapshot(
            state=self._state,
            shell_open=self._shell is not None,
            pump_cycles=self._pump_cycles,
            pumped_messages=self._pumped_messages,
            generation=0 if host is None else host.generation,
            viewport_count=0 if host is None else host.viewport_count,
            open_surface_count=0 if host is None else host.open_surface_count,
            delivered_frames=0 if host is None else host.delivered_frames,
            presentations=0 if host is None else host.presentations,
        )

    def _ensure_native_api(self) -> _NativeShellBoundary:
        if self._native_api is not None:
            return self._native_api
        try:
            self._native_api = _Win32OperatorShellApi()
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

    def _destroy_shell(self) -> bool:
        shell = self._shell
        self._shell = None
        if shell is None:
            return False
        native = self._native_api
        if native is None:
            return True
        try:
            native.destroy_shell(shell)
        except _NativeShellError:
            return True
        return False

    async def _close_host(self) -> bool:
        host = self._host
        self._host = None
        if host is None:
            return False
        try:
            self._host_snapshot = await asyncio.wait_for(
                host.close(),
                timeout=self._cleanup_timeout_seconds,
            )
        except BaseException:
            return True
        return False

    async def _fail_closed(self) -> None:
        await self._close_host()
        self._destroy_shell()
        self._state = WindowsOperatorApplicationState.FAILED

    def _assemble_host(self) -> _OperatorHostBoundary:
        shell = self._shell
        if shell is None:
            raise WindowsOperatorApplicationError(
                WindowsOperatorApplicationErrorCode.INVALID_STATE,
                "operator application shell is not open",
            )
        try:
            host = self._host_factory(shell)
        except Exception:
            raise WindowsOperatorApplicationError(
                WindowsOperatorApplicationErrorCode.HOST_ASSEMBLY_FAILURE,
                "operator application host assembly failed",
            ) from None
        if not isinstance(host, _OperatorHostBoundary):
            raise WindowsOperatorApplicationError(
                WindowsOperatorApplicationErrorCode.HOST_ASSEMBLY_FAILURE,
                "operator application host assembly failed",
            )
        return host

    async def open(self, width: int, height: int) -> WindowsOperatorApplicationSnapshot:
        """Create one visible top-level shell without exposing its native handle."""
        async with self._lock:
            if self._state == WindowsOperatorApplicationState.OPEN:
                return self.snapshot
            if self._state != WindowsOperatorApplicationState.READY:
                raise WindowsOperatorApplicationError(
                    WindowsOperatorApplicationErrorCode.INVALID_STATE,
                    "operator application cannot open from current state",
                )
            if (
                isinstance(width, bool)
                or isinstance(height, bool)
                or not isinstance(width, int)
                or not isinstance(height, int)
                or not 1 <= width <= _MAX_DIMENSION
                or not 1 <= height <= _MAX_DIMENSION
            ):
                raise WindowsOperatorApplicationError(
                    WindowsOperatorApplicationErrorCode.INVALID_CONFIGURATION,
                    "operator application shell geometry is invalid",
                )
            native = self._ensure_native_api()
            try:
                self._shell = native.create_shell(width, height)
            except _NativeShellError:
                self._state = WindowsOperatorApplicationState.FAILED
                raise WindowsOperatorApplicationError(
                    WindowsOperatorApplicationErrorCode.SHELL_CREATE_FAILURE,
                    "operator application shell creation failed",
                ) from None
            self._state = WindowsOperatorApplicationState.OPEN
            return self.snapshot

    async def start(
        self,
        layout: ViewportLayout,
        streams: Sequence[MixedPresentationStream],
    ) -> WindowsOperatorApplicationSnapshot:
        """Start the accepted operator host inside the already-open shell."""
        async with self._lock:
            if self._state != WindowsOperatorApplicationState.OPEN:
                raise WindowsOperatorApplicationError(
                    WindowsOperatorApplicationErrorCode.INVALID_STATE,
                    "operator application cannot start from current state",
                )
            try:
                host = self._assemble_host()
            except WindowsOperatorApplicationError:
                await self._fail_closed()
                raise
            self._host = host
            try:
                self._host_snapshot = await host.start(layout, streams)
            except asyncio.CancelledError:
                await self._fail_closed()
                raise
            except Exception:
                await self._fail_closed()
                raise WindowsOperatorApplicationError(
                    WindowsOperatorApplicationErrorCode.START_FAILURE,
                    "operator application start failed",
                ) from None
            self._state = WindowsOperatorApplicationState.RUNNING
            return self.snapshot

    async def replace(
        self,
        layout: ViewportLayout,
        streams: Sequence[MixedPresentationStream],
    ) -> WindowsOperatorApplicationSnapshot:
        """Replace the current arbitrary layout without replacing the top-level shell."""
        async with self._lock:
            if (
                self._state
                not in {
                    WindowsOperatorApplicationState.RUNNING,
                    WindowsOperatorApplicationState.COMPLETE,
                    WindowsOperatorApplicationState.STOPPED,
                }
                or self._host is None
            ):
                raise WindowsOperatorApplicationError(
                    WindowsOperatorApplicationErrorCode.INVALID_STATE,
                    "operator application cannot replace from current state",
                )
            try:
                self._host_snapshot = await self._host.replace(layout, streams)
            except asyncio.CancelledError:
                await self._fail_closed()
                raise
            except Exception:
                await self._fail_closed()
                raise WindowsOperatorApplicationError(
                    WindowsOperatorApplicationErrorCode.CONTROL_FAILURE,
                    "operator application replacement failed",
                ) from None
            self._state = WindowsOperatorApplicationState.RUNNING
            return self.snapshot

    async def relayout(self, layout: ViewportLayout) -> WindowsOperatorApplicationSnapshot:
        """Apply source-free geometry while preserving shell, generation, and media plan."""
        async with self._lock:
            if self._state != WindowsOperatorApplicationState.RUNNING or self._host is None:
                raise WindowsOperatorApplicationError(
                    WindowsOperatorApplicationErrorCode.INVALID_STATE,
                    "operator application cannot relayout from current state",
                )
            host = self._host
            if not isinstance(host, _RelayoutOperatorHostBoundary):
                await self._fail_closed()
                raise WindowsOperatorApplicationError(
                    WindowsOperatorApplicationErrorCode.CONTROL_FAILURE,
                    "operator application relayout boundary is unavailable",
                )
            try:
                child = await host.relayout(layout)
            except asyncio.CancelledError:
                await self._fail_closed()
                raise
            except Exception:
                await self._fail_closed()
                raise WindowsOperatorApplicationError(
                    WindowsOperatorApplicationErrorCode.CONTROL_FAILURE,
                    "operator application relayout failed",
                ) from None
            self._host_snapshot = child
            if child.state != WindowsOperatorHostState.RUNNING:
                await self._fail_closed()
                raise WindowsOperatorApplicationError(
                    WindowsOperatorApplicationErrorCode.CONTROL_FAILURE,
                    "operator application relayout did not preserve running state",
                )
            return self.snapshot

    async def wait(self) -> WindowsOperatorApplicationSnapshot:
        """Wait for the active host generation while retaining the shell."""
        async with self._lock:
            if self._state != WindowsOperatorApplicationState.RUNNING or self._host is None:
                raise WindowsOperatorApplicationError(
                    WindowsOperatorApplicationErrorCode.INVALID_STATE,
                    "operator application has no running generation",
                )
            host = self._host
        try:
            child = await host.wait()
        except asyncio.CancelledError:
            raise
        except Exception:
            async with self._lock:
                await self._fail_closed()
            raise WindowsOperatorApplicationError(
                WindowsOperatorApplicationErrorCode.EXECUTION_FAILURE,
                "operator application execution failed",
            ) from None
        async with self._lock:
            if host is not self._host:
                raise WindowsOperatorApplicationError(
                    WindowsOperatorApplicationErrorCode.INVALID_STATE,
                    "operator application generation changed during wait",
                )
            self._host_snapshot = child
            if child.state == WindowsOperatorHostState.COMPLETE:
                self._state = WindowsOperatorApplicationState.COMPLETE
            elif child.state == WindowsOperatorHostState.STOPPED:
                self._state = WindowsOperatorApplicationState.STOPPED
            else:
                await self._fail_closed()
                raise WindowsOperatorApplicationError(
                    WindowsOperatorApplicationErrorCode.EXECUTION_FAILURE,
                    "operator application execution failed",
                )
            return self.snapshot

    async def stop(self) -> WindowsOperatorApplicationSnapshot:
        """Stop the active host generation while leaving the shell available."""
        async with self._lock:
            if self._state == WindowsOperatorApplicationState.CLOSED:
                return self.snapshot
            if self._state != WindowsOperatorApplicationState.RUNNING or self._host is None:
                raise WindowsOperatorApplicationError(
                    WindowsOperatorApplicationErrorCode.INVALID_STATE,
                    "operator application has no running generation",
                )
            try:
                child = await self._host.stop()
            except asyncio.CancelledError:
                await self._fail_closed()
                raise
            except Exception:
                await self._fail_closed()
                raise WindowsOperatorApplicationError(
                    WindowsOperatorApplicationErrorCode.CONTROL_FAILURE,
                    "operator application stop failed",
                ) from None
            self._host_snapshot = child
            if child.state == WindowsOperatorHostState.COMPLETE:
                self._state = WindowsOperatorApplicationState.COMPLETE
            elif child.state == WindowsOperatorHostState.STOPPED:
                self._state = WindowsOperatorApplicationState.STOPPED
            else:
                await self._fail_closed()
                raise WindowsOperatorApplicationError(
                    WindowsOperatorApplicationErrorCode.CONTROL_FAILURE,
                    "operator application stop failed",
                )
            return self.snapshot

    async def pump(
        self,
        *,
        max_messages: int = 64,
    ) -> WindowsOperatorApplicationSnapshot:
        """Process a bounded message batch and honor close requests fail-closed."""
        async with self._lock:
            if (
                self._state
                not in {
                    WindowsOperatorApplicationState.OPEN,
                    WindowsOperatorApplicationState.RUNNING,
                    WindowsOperatorApplicationState.COMPLETE,
                    WindowsOperatorApplicationState.STOPPED,
                }
                or self._shell is None
            ):
                raise WindowsOperatorApplicationError(
                    WindowsOperatorApplicationErrorCode.INVALID_STATE,
                    "operator application shell cannot pump from current state",
                )
            if not 1 <= max_messages <= _MAX_PUMP_MESSAGES:
                raise WindowsOperatorApplicationError(
                    WindowsOperatorApplicationErrorCode.INVALID_CONFIGURATION,
                    "operator application message batch is invalid",
                )
            if self._pump_cycles >= self._max_pump_cycles:
                await self._fail_closed()
                raise WindowsOperatorApplicationError(
                    WindowsOperatorApplicationErrorCode.PUMP_LIMIT,
                    "operator application pump limit reached",
                )
            native = self._native_api
            if native is None:
                await self._fail_closed()
                raise WindowsOperatorApplicationError(
                    WindowsOperatorApplicationErrorCode.NATIVE_LOAD_FAILURE,
                    "operator application native API is unavailable",
                )
            try:
                pumped, close_requested = native.pump_messages(
                    self._shell,
                    max_messages,
                )
            except _NativeShellError:
                await self._fail_closed()
                raise WindowsOperatorApplicationError(
                    WindowsOperatorApplicationErrorCode.PUMP_FAILURE,
                    "operator application message pump failed",
                ) from None
            self._pump_cycles += 1
            self._pumped_messages += pumped
            if close_requested:
                cleanup_failed = await self._close_host()
                cleanup_failed = self._destroy_shell() or cleanup_failed
                if cleanup_failed:
                    self._state = WindowsOperatorApplicationState.FAILED
                    raise WindowsOperatorApplicationError(
                        WindowsOperatorApplicationErrorCode.CLEANUP_FAILURE,
                        "operator application close request cleanup failed",
                    )
                self._state = WindowsOperatorApplicationState.CLOSED
            return self.snapshot

    async def close(self) -> WindowsOperatorApplicationSnapshot:
        """Release child presentation resources before destroying the shell."""
        async with self._lock:
            if self._state == WindowsOperatorApplicationState.CLOSED:
                return self.snapshot
            host_failed = await self._close_host()
            shell_failed = self._destroy_shell()
            if host_failed or shell_failed:
                self._state = WindowsOperatorApplicationState.FAILED
                raise WindowsOperatorApplicationError(
                    WindowsOperatorApplicationErrorCode.CLEANUP_FAILURE,
                    "operator application cleanup failed",
                )
            self._state = WindowsOperatorApplicationState.CLOSED
            self._native_api = None
            return self.snapshot
