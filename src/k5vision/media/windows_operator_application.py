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
from k5vision.media.viewport_client_projection import (
    ViewportClientProjection,
    project_viewport_layout,
)
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
    GEOMETRY = "geometry"
    DESTROY = "destroy"


class _NativeShellError(RuntimeError):
    def __init__(self, failure: _NativeShellFailure) -> None:
        super().__init__(failure.value)
        self.failure = failure


class _ShellRect(ctypes.Structure):
    _fields_ = [(name, ctypes.c_int32) for name in ("left", "top", "right", "bottom")]


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


@typing.runtime_checkable
class _NativeShellGeometryBoundary(typing.Protocol):
    def client_size(self, shell: int) -> tuple[int, int]: ...

    def ensure_client_size(self, shell: int, width: int, height: int) -> tuple[int, int]: ...


OperatorHostFactory = Callable[[int], _OperatorHostBoundary]


class _Win32OperatorShellApi:
    """Small ctypes boundary for one visible top-level Win32 shell."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise _NativeShellError(_NativeShellFailure.UNSUPPORTED_PLATFORM)
        self._geometry_change_pending = False
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

            self._get_client_rect = self._user32.GetClientRect
            self._get_client_rect.argtypes = [ctypes.c_void_p, ctypes.POINTER(_ShellRect)]
            self._get_client_rect.restype = ctypes.c_int
            self._get_window_rect = self._user32.GetWindowRect
            self._get_window_rect.argtypes = [ctypes.c_void_p, ctypes.POINTER(_ShellRect)]
            self._get_window_rect.restype = ctypes.c_int
            self._is_iconic = self._user32.IsIconic
            self._is_iconic.argtypes = [ctypes.c_void_p]
            self._is_iconic.restype = ctypes.c_int
            self._set_window_pos = self._user32.SetWindowPos
            self._set_window_pos.argtypes = [
                ctypes.c_void_p,
                ctypes.c_void_p,
                *([ctypes.c_int] * 4),
                ctypes.c_uint32,
            ]
            self._set_window_pos.restype = ctypes.c_int

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

    def client_size(self, shell: int) -> tuple[int, int]:
        """Observe actual client pixels on the HWND-creating thread."""
        rect = _ShellRect()
        try:
            if self._is_iconic(ctypes.c_void_p(shell)):
                return 0, 0
            if not self._get_client_rect(ctypes.c_void_p(shell), ctypes.byref(rect)):
                raise _NativeShellError(_NativeShellFailure.GEOMETRY)
            result = rect.right - rect.left, rect.bottom - rect.top
            if any(not 0 <= value <= _MAX_DIMENSION for value in result):
                raise _NativeShellError(_NativeShellFailure.GEOMETRY)
            return result
        except Exception:
            raise _NativeShellError(_NativeShellFailure.GEOMETRY) from None

    def ensure_client_size(self, shell: int, width: int, height: int) -> tuple[int, int]:
        """Clamp a positive settled client once; never restore a minimized shell."""
        try:
            if any(
                type(value) is not int or not 1 <= value <= _MAX_DIMENSION
                for value in (width, height)
            ):
                raise _NativeShellError(_NativeShellFailure.GEOMETRY)
            current = self.client_size(shell)
            if 0 in current or (current[0] >= width and current[1] >= height):
                return current
            outer = _ShellRect()
            if not self._get_window_rect(ctypes.c_void_p(shell), ctypes.byref(outer)):
                raise _NativeShellError(_NativeShellFailure.GEOMETRY)
            outer_width, outer_height = outer.right - outer.left, outer.bottom - outer.top
            if outer_width < current[0] or outer_height < current[1]:
                raise _NativeShellError(_NativeShellFailure.GEOMETRY)
            requested_width = outer_width + max(0, width - current[0])
            requested_height = outer_height + max(0, height - current[1])
            if not all(
                1 <= value <= 2 * _MAX_DIMENSION for value in (requested_width, requested_height)
            ):
                raise _NativeShellError(_NativeShellFailure.GEOMETRY)
            if not self._set_window_pos(
                ctypes.c_void_p(shell),
                None,
                0,
                0,
                requested_width,
                requested_height,
                0x0002 | 0x0004 | 0x0010,  # NOMOVE | NOZORDER | NOACTIVATE
            ):
                raise _NativeShellError(_NativeShellFailure.GEOMETRY)
            result = self.client_size(shell)
            if 0 not in result and (result[0] < width or result[1] < height):
                raise _NativeShellError(_NativeShellFailure.GEOMETRY)
            return result
        except Exception:
            raise _NativeShellError(_NativeShellFailure.GEOMETRY) from None

    def _note_geometry_message(self, shell: int, message: _Win32Message) -> None:
        if int(message.hwnd or 0) != shell:
            return
        kind, command = int(message.message), int(message.wParam)
        if (
            kind in {0x0005, 0x0047}  # SIZE, WINDOWPOSCHANGED when queued.
            or (kind in {0x00A1, 0x00A3} and command in {2, *range(10, 18)})
            or (kind == 0x0112 and command & 0xFFF0 in {0xF000, 0xF010, 0xF020, 0xF030, 0xF120})
        ):
            # Record intent before DispatchMessage may enter the native modal loop.
            # A drag that returns to the same size still invalidates old input.
            self._geometry_change_pending = True

    def take_geometry_change(self) -> bool:
        pending = getattr(self, "_geometry_change_pending", False)
        self._geometry_change_pending = False
        return pending

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
                self._note_geometry_message(shell, message)
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

    _minimum_client_width = 192
    _minimum_client_height = 192
    _content_top = 0

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
        self._reference_size = (1, 1)
        self._logical_layout: ViewportLayout | None = None
        self._projection: ViewportClientProjection | None = None
        self._observed_client: tuple[int, int] | None = None
        self._last_positive_client: tuple[int, int] | None = None
        self._pointer_projection_suspended = False
        self._projection_change_in_progress = False
        self._lock = asyncio.Lock()

    def _observe_client(self) -> tuple[int, int] | None:
        native, shell = self._native_api, self._shell
        if shell is None or not isinstance(native, _NativeShellGeometryBoundary):
            return None
        extent = native.client_size(shell)
        if (
            not isinstance(extent, tuple)
            or len(extent) != 2
            or any(type(value) is not int or not 0 <= value <= _MAX_DIMENSION for value in extent)
        ):
            raise _NativeShellError(_NativeShellFailure.GEOMETRY)
        if 0 not in extent and (
            extent[0] < self._minimum_client_width or extent[1] < self._minimum_client_height
        ):
            extent = native.ensure_client_size(
                shell, self._minimum_client_width, self._minimum_client_height
            )
            if (
                not isinstance(extent, tuple)
                or len(extent) != 2
                or any(
                    type(value) is not int or not 0 <= value <= _MAX_DIMENSION for value in extent
                )
                or (
                    0 not in extent
                    and (
                        extent[0] < self._minimum_client_width
                        or extent[1] < self._minimum_client_height
                    )
                )
            ):
                raise _NativeShellError(_NativeShellFailure.GEOMETRY)
        return extent

    def _projection_for(
        self, layout: ViewportLayout, extent: tuple[int, int] | None
    ) -> ViewportClientProjection | None:
        if extent is None:
            return None  # Existing injected host-only boundaries remain compatible.
        selected = (
            extent
            if 0 not in extent
            else (
                self._last_positive_client
                or (self._minimum_client_width, self._minimum_client_height)
            )
        )
        return project_viewport_layout(
            layout,
            reference_width=self._reference_size[0],
            reference_height=self._reference_size[1],
            client_width=selected[0],
            client_height=selected[1],
            content_top=self._content_top,
        )

    def _invalidate_projected_pointer_input(self) -> None:
        """Interactive variants discard stale raw input and deliver CANCEL here."""

    def _begin_projection_change(
        self,
        projection: ViewportClientProjection | None,
        extent: tuple[int, int] | None,
        *,
        force: bool = False,
    ) -> None:
        if force or projection != self._projection or extent != self._observed_client:
            # Fence raw pointer input before awaiting target replacement. A separate
            # input consumer must never reinterpret an old batch during that await.
            self._projection_change_in_progress = True
            self._pointer_projection_suspended = True
            self._invalidate_projected_pointer_input()

    def _accept_projection(
        self,
        layout: ViewportLayout,
        projection: ViewportClientProjection | None,
        extent: tuple[int, int] | None,
    ) -> None:
        changed = projection != self._projection or extent != self._observed_client
        self._logical_layout, self._projection, self._observed_client = layout, projection, extent
        self._pointer_projection_suspended = extent is not None and 0 in extent
        if extent is not None and 0 not in extent:
            self._last_positive_client = extent
        if changed and not self._projection_change_in_progress:
            self._invalidate_projected_pointer_input()
        self._projection_change_in_progress = False

    def _refresh_native_chrome(self) -> None:
        refresh = getattr(self._native_api, "after_viewport_layout", None)
        if callable(refresh):
            refresh()

    async def _refresh_client_projection(self, *, force: bool = False) -> None:
        extent = self._observe_client()
        if (extent == self._observed_client and not force) or self._logical_layout is None:
            return
        if extent is not None and 0 in extent:
            self._begin_projection_change(self._projection, extent, force=force)
            self._accept_projection(self._logical_layout, self._projection, extent)
            return
        projection = self._projection_for(self._logical_layout, extent)
        self._begin_projection_change(projection, extent, force=force)
        host = self._host
        if projection != self._projection:
            if not (
                host is not None
                and self._state == WindowsOperatorApplicationState.RUNNING
                and host.snapshot.state == WindowsOperatorHostState.RUNNING
            ):
                # No live targets were relaid out. Keep their accepted projection,
                # but invalidate input; a later start/replace observes the new size.
                self._accept_projection(self._logical_layout, self._projection, extent)
                return
            if not isinstance(host, _RelayoutOperatorHostBoundary):
                raise _NativeShellError(_NativeShellFailure.GEOMETRY)
            child = await host.relayout(
                self._logical_layout if projection is None else projection.physical_layout
            )
            if child.state != WindowsOperatorHostState.RUNNING:
                raise _NativeShellError(_NativeShellFailure.GEOMETRY)
            self._host_snapshot = child
            self._refresh_native_chrome()
        self._accept_projection(self._logical_layout, projection, extent)

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
            self._reference_size = (width, height)
            try:
                self._observed_client = self._observe_client()
                if self._observed_client is not None and 0 not in self._observed_client:
                    self._last_positive_client = self._observed_client
            except Exception:
                await self._fail_closed()
                raise WindowsOperatorApplicationError(
                    WindowsOperatorApplicationErrorCode.SHELL_CREATE_FAILURE,
                    "operator application client geometry is unavailable",
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
                extent = self._observe_client()
                projection = self._projection_for(layout, extent)
                self._begin_projection_change(projection, extent)
                self._host_snapshot = await host.start(
                    layout if projection is None else projection.physical_layout, streams
                )
                self._accept_projection(layout, projection, extent)
                self._refresh_native_chrome()
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
                extent = self._observe_client()
                projection = self._projection_for(layout, extent)
                self._begin_projection_change(projection, extent)
                self._host_snapshot = await self._host.replace(
                    layout if projection is None else projection.physical_layout, streams
                )
                self._accept_projection(layout, projection, extent)
                self._refresh_native_chrome()
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
                extent = self._observe_client()
                projection = self._projection_for(layout, extent)
                self._begin_projection_change(projection, extent)
                child = await host.relayout(
                    layout if projection is None else projection.physical_layout
                )
                self._accept_projection(layout, projection, extent)
                self._refresh_native_chrome()
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
            if not close_requested:
                try:
                    # Sample actual client dimensions once after this bounded batch;
                    # WM_SIZE can also arrive synchronously inside native dispatch.
                    changed = getattr(native, "take_geometry_change", None)
                    force = changed() if callable(changed) else False
                    if type(force) is not bool:
                        raise _NativeShellError(_NativeShellFailure.GEOMETRY)
                    await self._refresh_client_projection(force=force)
                except asyncio.CancelledError:
                    await self._fail_closed()
                    raise
                except Exception:
                    await self._fail_closed()
                    raise WindowsOperatorApplicationError(
                        WindowsOperatorApplicationErrorCode.PUMP_FAILURE,
                        "operator application client relayout failed",
                    ) from None
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
