"""Bounded Win32 DIB presentation surface for transient BGRx frames.

The surface is intentionally host-neutral: it owns only an in-memory Win32 DIB section
and never creates application chrome or persists frame bytes. Native handles and
pointers remain private implementation details and never enter snapshots or errors.
"""

from __future__ import annotations

import asyncio
import ctypes
import enum
import sys
import typing

from pydantic import BaseModel, ConfigDict, Field

from k5vision.media.presentation_frame import PixelFormat, PresentationVideoFrame

_MAX_DIMENSION = 16_384
_MAX_FRAME_BYTES = 128 * 1024 * 1024
_MAX_TOTAL_FRAME_BYTES = 64 * 1024 * 1024 * 1024
_MAX_FRAMES = 1_000_000
_MAX_SURFACE_REPLACEMENTS = 1024
_MAX_SOURCE_SPAN_MS = 2_147_483_647
_MAX_BLITS = 1_000_000
_BI_RGB = 0
_DIB_RGB_COLORS = 0
_SRCCOPY = 0x00CC0020
_HGDI_ERROR = ctypes.c_void_p(-1).value
_MAX_POINTER = (1 << (ctypes.sizeof(ctypes.c_void_p) * 8)) - 1


class WindowsPresentationSurfaceState(enum.StrEnum):
    READY = "ready"
    OPEN = "open"
    FAILED = "failed"
    CLOSED = "closed"


class WindowsPresentationSurfaceErrorCode(enum.StrEnum):
    INVALID_CONFIGURATION = "invalid_configuration"
    INVALID_STATE = "invalid_state"
    INVALID_FRAME = "invalid_frame"
    UNSUPPORTED_PLATFORM = "unsupported_platform"
    NATIVE_LOAD_FAILURE = "native_load_failure"
    SURFACE_CREATE_FAILURE = "surface_create_failure"
    SURFACE_COPY_FAILURE = "surface_copy_failure"
    INVALID_TARGET = "invalid_target"
    BLIT_FAILURE = "blit_failure"
    BLIT_LIMIT = "blit_limit"
    SURFACE_REPLACEMENT_LIMIT = "surface_replacement_limit"
    FRAME_LIMIT = "frame_limit"
    FRAME_BYTES_LIMIT = "frame_bytes_limit"
    TOTAL_BYTES_LIMIT = "total_bytes_limit"
    CLEANUP_FAILURE = "cleanup_failure"


class WindowsPresentationSurfaceError(RuntimeError):
    """Sanitized presentation-surface failure."""

    def __init__(self, code: WindowsPresentationSurfaceErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class WindowsPresentationSurfaceSnapshot(BaseModel):
    """Payload/source/path/identifier/native-handle-free surface observability."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: typing.Literal["1"] = "1"
    state: WindowsPresentationSurfaceState
    surface_open: bool
    width: int = Field(ge=0, le=_MAX_DIMENSION)
    height: int = Field(ge=0, le=_MAX_DIMENSION)
    native_stride_bytes: int = Field(ge=0, le=_MAX_FRAME_BYTES)
    presented_frames: int = Field(ge=0, le=_MAX_FRAMES)
    presented_frame_bytes: int = Field(ge=0, le=_MAX_TOTAL_FRAME_BYTES)
    surface_replacements: int = Field(ge=0, le=_MAX_SURFACE_REPLACEMENTS)
    max_source_span_ms: int = Field(ge=0, le=_MAX_SOURCE_SPAN_MS)
    blits: int = Field(ge=0, le=_MAX_BLITS)


class _NativeSurfaceFailure(enum.StrEnum):
    UNSUPPORTED_PLATFORM = "unsupported_platform"
    LOAD = "load"
    CREATE = "create"
    COPY = "copy"
    BLIT = "blit"
    DESTROY = "destroy"


class _NativeSurfaceError(RuntimeError):
    def __init__(self, failure: _NativeSurfaceFailure) -> None:
        super().__init__(failure.value)
        self.failure = failure


@typing.runtime_checkable
class _NativeSurfaceBoundary(typing.Protocol):
    def create_surface(self, width: int, height: int) -> tuple[int, int, int]: ...

    def copy_frame(
        self,
        bits_pointer: int,
        native_stride_bytes: int,
        frame: PresentationVideoFrame,
    ) -> None: ...

    def blit_surface(
        self,
        handle: int,
        width: int,
        height: int,
        target_dc: int,
    ) -> None: ...

    def destroy_surface(self, handle: int) -> None: ...


class _BitmapInfoHeader(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.c_uint32),
        ("biWidth", ctypes.c_int32),
        ("biHeight", ctypes.c_int32),
        ("biPlanes", ctypes.c_uint16),
        ("biBitCount", ctypes.c_uint16),
        ("biCompression", ctypes.c_uint32),
        ("biSizeImage", ctypes.c_uint32),
        ("biXPelsPerMeter", ctypes.c_int32),
        ("biYPelsPerMeter", ctypes.c_int32),
        ("biClrUsed", ctypes.c_uint32),
        ("biClrImportant", ctypes.c_uint32),
    ]


class _BitmapInfo(ctypes.Structure):
    _fields_ = [
        ("bmiHeader", _BitmapInfoHeader),
        ("bmiColors", ctypes.c_uint32 * 1),
    ]


class _Win32DibSurfaceApi:
    """Narrow reviewed GDI boundary: CreateDIBSection, DeleteObject, and memory copy."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise _NativeSurfaceError(_NativeSurfaceFailure.UNSUPPORTED_PLATFORM)
        try:
            loader = ctypes.WinDLL
            self._gdi32 = loader("gdi32", use_last_error=True)
            self._create_dib_section = self._gdi32.CreateDIBSection
            self._delete_object = self._gdi32.DeleteObject
            self._create_dib_section.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(_BitmapInfo),
                ctypes.c_uint32,
                ctypes.POINTER(ctypes.c_void_p),
                ctypes.c_void_p,
                ctypes.c_uint32,
            ]
            self._create_dib_section.restype = ctypes.c_void_p
            self._delete_object.argtypes = [ctypes.c_void_p]
            self._delete_object.restype = ctypes.c_int
            self._create_compatible_dc = self._gdi32.CreateCompatibleDC
            self._delete_dc = self._gdi32.DeleteDC
            self._select_object = self._gdi32.SelectObject
            self._bit_blt = self._gdi32.BitBlt
            self._create_compatible_dc.argtypes = [ctypes.c_void_p]
            self._create_compatible_dc.restype = ctypes.c_void_p
            self._delete_dc.argtypes = [ctypes.c_void_p]
            self._delete_dc.restype = ctypes.c_int
            self._select_object.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            self._select_object.restype = ctypes.c_void_p
            self._bit_blt.argtypes = [
                ctypes.c_void_p,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_void_p,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_uint32,
            ]
            self._bit_blt.restype = ctypes.c_int
        except Exception:
            raise _NativeSurfaceError(_NativeSurfaceFailure.LOAD) from None

    def create_surface(self, width: int, height: int) -> tuple[int, int, int]:
        info = _BitmapInfo()
        info.bmiHeader.biSize = ctypes.sizeof(_BitmapInfoHeader)
        info.bmiHeader.biWidth = width
        info.bmiHeader.biHeight = -height
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = _BI_RGB
        info.bmiHeader.biSizeImage = width * height * 4
        bits = ctypes.c_void_p()
        try:
            handle = self._create_dib_section(
                None,
                ctypes.byref(info),
                _DIB_RGB_COLORS,
                ctypes.byref(bits),
                None,
                0,
            )
        except Exception:
            raise _NativeSurfaceError(_NativeSurfaceFailure.CREATE) from None
        handle_value = int(handle or 0)
        bits_value = int(bits.value or 0)
        if handle_value == 0 or bits_value == 0:
            if handle_value:
                try:
                    self._delete_object(ctypes.c_void_p(handle_value))
                except Exception:
                    pass
            raise _NativeSurfaceError(_NativeSurfaceFailure.CREATE)
        return handle_value, bits_value, width * 4

    @staticmethod
    def _copy_view(destination: int, view: memoryview) -> None:
        try:
            byte_view = view.cast("B")
        except (TypeError, ValueError):
            byte_view = memoryview(view.tobytes())
        if not byte_view.c_contiguous:
            byte_view = memoryview(byte_view.tobytes())
        if not byte_view.readonly:
            try:
                source = (ctypes.c_ubyte * len(byte_view)).from_buffer(byte_view)
                ctypes.memmove(destination, ctypes.addressof(source), len(byte_view))
                return
            except (BufferError, TypeError, ValueError):
                pass
        transient = byte_view.tobytes()
        ctypes.memmove(destination, transient, len(transient))

    def copy_frame(
        self,
        bits_pointer: int,
        native_stride_bytes: int,
        frame: PresentationVideoFrame,
    ) -> None:
        row_bytes = frame.width * 4
        if native_stride_bytes != row_bytes:
            raise _NativeSurfaceError(_NativeSurfaceFailure.COPY)
        try:
            if frame.stride_bytes == row_bytes:
                self._copy_view(bits_pointer, frame.payload)
                return
            for row in range(frame.height):
                start = row * frame.stride_bytes
                stop = start + row_bytes
                self._copy_view(
                    bits_pointer + (row * native_stride_bytes),
                    frame.payload[start:stop],
                )
        except _NativeSurfaceError:
            raise
        except Exception:
            raise _NativeSurfaceError(_NativeSurfaceFailure.COPY) from None

    def blit_surface(
        self,
        handle: int,
        width: int,
        height: int,
        target_dc: int,
    ) -> None:
        try:
            source_dc = int(
                self._create_compatible_dc(ctypes.c_void_p(target_dc)) or 0
            )
        except Exception:
            raise _NativeSurfaceError(_NativeSurfaceFailure.BLIT) from None
        if source_dc == 0:
            raise _NativeSurfaceError(_NativeSurfaceFailure.BLIT)

        try:
            previous = int(
                self._select_object(
                    ctypes.c_void_p(source_dc),
                    ctypes.c_void_p(handle),
                )
                or 0
            )
        except Exception:
            try:
                self._delete_dc(ctypes.c_void_p(source_dc))
            except Exception:
                pass
            raise _NativeSurfaceError(_NativeSurfaceFailure.BLIT) from None

        if previous in {0, _HGDI_ERROR}:
            try:
                self._delete_dc(ctypes.c_void_p(source_dc))
            except Exception:
                pass
            raise _NativeSurfaceError(_NativeSurfaceFailure.BLIT)

        try:
            copied = bool(
                self._bit_blt(
                    ctypes.c_void_p(target_dc),
                    0,
                    0,
                    width,
                    height,
                    ctypes.c_void_p(source_dc),
                    0,
                    0,
                    _SRCCOPY,
                )
            )
        except Exception:
            copied = False

        try:
            restored = int(
                self._select_object(
                    ctypes.c_void_p(source_dc),
                    ctypes.c_void_p(previous),
                )
                or 0
            )
        except Exception:
            restored = 0
        try:
            deleted_dc = bool(self._delete_dc(ctypes.c_void_p(source_dc)))
        except Exception:
            deleted_dc = False

        if not copied or restored in {0, _HGDI_ERROR} or not deleted_dc:
            raise _NativeSurfaceError(_NativeSurfaceFailure.BLIT)

    def destroy_surface(self, handle: int) -> None:
        try:
            deleted = self._delete_object(ctypes.c_void_p(handle))
        except Exception:
            raise _NativeSurfaceError(_NativeSurfaceFailure.DESTROY) from None
        if not deleted:
            raise _NativeSurfaceError(_NativeSurfaceFailure.DESTROY)


class BoundedWindowsPresentationSurface:
    """Consume transient BGRx frames into one bounded in-memory Windows DIB surface."""

    def __init__(
        self,
        *,
        max_frames: int = 100_000,
        max_frame_bytes: int = 64 * 1024 * 1024,
        max_total_frame_bytes: int = 16 * 1024 * 1024 * 1024,
        max_surface_replacements: int = 64,
        max_blits: int = 100_000,
        native_api: _NativeSurfaceBoundary | None = None,
    ) -> None:
        if not 1 <= max_frames <= _MAX_FRAMES:
            raise ValueError("max_frames must be between 1 and 1000000")
        if not 1 <= max_frame_bytes <= _MAX_FRAME_BYTES:
            raise ValueError("max_frame_bytes must be between 1 and 134217728")
        if not 1 <= max_total_frame_bytes <= _MAX_TOTAL_FRAME_BYTES:
            raise ValueError("max_total_frame_bytes must be between 1 and 68719476736")
        if not 0 <= max_surface_replacements <= _MAX_SURFACE_REPLACEMENTS:
            raise ValueError("max_surface_replacements must be between 0 and 1024")
        if not 1 <= max_blits <= _MAX_BLITS:
            raise ValueError("max_blits must be between 1 and 1000000")

        self._max_frames = max_frames
        self._max_frame_bytes = max_frame_bytes
        self._max_total_frame_bytes = max_total_frame_bytes
        self._max_surface_replacements = max_surface_replacements
        self._max_blits = max_blits
        self._native_api = native_api
        self._state = WindowsPresentationSurfaceState.READY
        self._surface_handle: int | None = None
        self._bits_pointer: int | None = None
        self._width = 0
        self._height = 0
        self._native_stride_bytes = 0
        self._presented_frames = 0
        self._presented_frame_bytes = 0
        self._surface_replacements = 0
        self._max_source_span_ms = 0
        self._blits = 0
        self._lock = asyncio.Lock()

    @property
    def snapshot(self) -> WindowsPresentationSurfaceSnapshot:
        return WindowsPresentationSurfaceSnapshot(
            state=self._state,
            surface_open=self._surface_handle is not None,
            width=self._width,
            height=self._height,
            native_stride_bytes=self._native_stride_bytes,
            presented_frames=self._presented_frames,
            presented_frame_bytes=self._presented_frame_bytes,
            surface_replacements=self._surface_replacements,
            max_source_span_ms=self._max_source_span_ms,
            blits=self._blits,
        )

    async def open(self) -> WindowsPresentationSurfaceSnapshot:
        async with self._lock:
            if self._state == WindowsPresentationSurfaceState.OPEN:
                return self.snapshot
            if self._state != WindowsPresentationSurfaceState.READY:
                raise WindowsPresentationSurfaceError(
                    WindowsPresentationSurfaceErrorCode.INVALID_STATE,
                    "presentation surface cannot open from current state",
                )
            if self._native_api is None:
                try:
                    self._native_api = _Win32DibSurfaceApi()
                except _NativeSurfaceError as exc:
                    self._state = WindowsPresentationSurfaceState.FAILED
                    if exc.failure == _NativeSurfaceFailure.UNSUPPORTED_PLATFORM:
                        code = WindowsPresentationSurfaceErrorCode.UNSUPPORTED_PLATFORM
                        message = "Windows presentation surface requires Win32"
                    else:
                        code = WindowsPresentationSurfaceErrorCode.NATIVE_LOAD_FAILURE
                        message = "Windows presentation native surface is unavailable"
                    raise WindowsPresentationSurfaceError(code, message) from None
            self._state = WindowsPresentationSurfaceState.OPEN
            return self.snapshot

    def _clear_surface_state(self) -> None:
        self._surface_handle = None
        self._bits_pointer = None
        self._width = 0
        self._height = 0
        self._native_stride_bytes = 0

    def _destroy_surface(self) -> None:
        handle = self._surface_handle
        self._clear_surface_state()
        if handle is None:
            return
        if self._native_api is None:
            raise _NativeSurfaceError(_NativeSurfaceFailure.DESTROY)
        self._native_api.destroy_surface(handle)

    def _fail_and_release(self) -> None:
        self._state = WindowsPresentationSurfaceState.FAILED
        try:
            self._destroy_surface()
        except _NativeSurfaceError:
            self._clear_surface_state()

    def _ensure_geometry(self, frame: PresentationVideoFrame) -> None:
        if self._surface_handle is not None and (self._width, self._height) == (
            frame.width,
            frame.height,
        ):
            return
        replacing = self._surface_handle is not None
        if replacing and self._surface_replacements >= self._max_surface_replacements:
            self._fail_and_release()
            raise WindowsPresentationSurfaceError(
                WindowsPresentationSurfaceErrorCode.SURFACE_REPLACEMENT_LIMIT,
                "presentation surface replacement limit exceeded",
            )
        if replacing:
            try:
                self._destroy_surface()
            except _NativeSurfaceError:
                self._state = WindowsPresentationSurfaceState.FAILED
                raise WindowsPresentationSurfaceError(
                    WindowsPresentationSurfaceErrorCode.CLEANUP_FAILURE,
                    "presentation surface replacement cleanup failed",
                ) from None
        if self._native_api is None:
            self._state = WindowsPresentationSurfaceState.FAILED
            raise WindowsPresentationSurfaceError(
                WindowsPresentationSurfaceErrorCode.NATIVE_LOAD_FAILURE,
                "Windows presentation native surface is unavailable",
            )
        try:
            handle, bits_pointer, native_stride_bytes = self._native_api.create_surface(
                frame.width,
                frame.height,
            )
        except _NativeSurfaceError:
            self._state = WindowsPresentationSurfaceState.FAILED
            raise WindowsPresentationSurfaceError(
                WindowsPresentationSurfaceErrorCode.SURFACE_CREATE_FAILURE,
                "presentation surface creation failed",
            ) from None
        if native_stride_bytes != frame.width * 4:
            try:
                self._native_api.destroy_surface(handle)
            except _NativeSurfaceError:
                pass
            self._state = WindowsPresentationSurfaceState.FAILED
            raise WindowsPresentationSurfaceError(
                WindowsPresentationSurfaceErrorCode.SURFACE_CREATE_FAILURE,
                "presentation surface geometry is invalid",
            )
        self._surface_handle = handle
        self._bits_pointer = bits_pointer
        self._width = frame.width
        self._height = frame.height
        self._native_stride_bytes = native_stride_bytes
        if replacing:
            self._surface_replacements += 1

    async def present(self, frame: PresentationVideoFrame) -> None:
        """Copy one transient validated frame into the private in-memory DIB surface."""
        async with self._lock:
            if self._state != WindowsPresentationSurfaceState.OPEN:
                raise WindowsPresentationSurfaceError(
                    WindowsPresentationSurfaceErrorCode.INVALID_STATE,
                    "presentation surface is not open",
                )
            if (
                not isinstance(frame, PresentationVideoFrame)
                or frame.pixel_format != PixelFormat.BGRX
            ):
                self._fail_and_release()
                raise WindowsPresentationSurfaceError(
                    WindowsPresentationSurfaceErrorCode.INVALID_FRAME,
                    "presentation surface frame is invalid",
                )
            frame_bytes = len(frame.payload)
            if self._presented_frames >= self._max_frames:
                self._fail_and_release()
                raise WindowsPresentationSurfaceError(
                    WindowsPresentationSurfaceErrorCode.FRAME_LIMIT,
                    "presentation surface frame limit exceeded",
                )
            if frame_bytes > self._max_frame_bytes:
                self._fail_and_release()
                raise WindowsPresentationSurfaceError(
                    WindowsPresentationSurfaceErrorCode.FRAME_BYTES_LIMIT,
                    "presentation surface frame-byte limit exceeded",
                )
            if self._presented_frame_bytes + frame_bytes > self._max_total_frame_bytes:
                self._fail_and_release()
                raise WindowsPresentationSurfaceError(
                    WindowsPresentationSurfaceErrorCode.TOTAL_BYTES_LIMIT,
                    "presentation surface aggregate frame-byte limit exceeded",
                )

            self._ensure_geometry(frame)
            if self._native_api is None or self._bits_pointer is None:
                self._fail_and_release()
                raise WindowsPresentationSurfaceError(
                    WindowsPresentationSurfaceErrorCode.NATIVE_LOAD_FAILURE,
                    "Windows presentation native surface is unavailable",
                )
            try:
                self._native_api.copy_frame(
                    self._bits_pointer,
                    self._native_stride_bytes,
                    frame,
                )
            except _NativeSurfaceError:
                self._fail_and_release()
                raise WindowsPresentationSurfaceError(
                    WindowsPresentationSurfaceErrorCode.SURFACE_COPY_FAILURE,
                    "presentation surface frame copy failed",
                ) from None

            self._presented_frames += 1
            self._presented_frame_bytes += frame_bytes
            self._max_source_span_ms = max(self._max_source_span_ms, frame.source_elapsed_ms)

    async def blit(self, target_dc: int) -> None:
        """Copy the current private surface into one caller-owned GDI device context."""
        async with self._lock:
            if (
                self._state != WindowsPresentationSurfaceState.OPEN
                or self._surface_handle is None
                or self._width < 1
                or self._height < 1
            ):
                raise WindowsPresentationSurfaceError(
                    WindowsPresentationSurfaceErrorCode.INVALID_STATE,
                    "presentation surface is not ready for target presentation",
                )
            if (
                isinstance(target_dc, bool)
                or not isinstance(target_dc, int)
                or not 1 <= target_dc <= _MAX_POINTER
            ):
                raise WindowsPresentationSurfaceError(
                    WindowsPresentationSurfaceErrorCode.INVALID_TARGET,
                    "presentation target is invalid",
                )
            if self._blits >= self._max_blits:
                self._fail_and_release()
                raise WindowsPresentationSurfaceError(
                    WindowsPresentationSurfaceErrorCode.BLIT_LIMIT,
                    "presentation target operation limit exceeded",
                )
            if self._native_api is None:
                self._fail_and_release()
                raise WindowsPresentationSurfaceError(
                    WindowsPresentationSurfaceErrorCode.NATIVE_LOAD_FAILURE,
                    "Windows presentation native surface is unavailable",
                )
            try:
                self._native_api.blit_surface(
                    self._surface_handle,
                    self._width,
                    self._height,
                    target_dc,
                )
            except _NativeSurfaceError:
                self._fail_and_release()
                raise WindowsPresentationSurfaceError(
                    WindowsPresentationSurfaceErrorCode.BLIT_FAILURE,
                    "presentation target operation failed",
                ) from None
            self._blits += 1

    async def __call__(self, frame: PresentationVideoFrame) -> None:
        await self.present(frame)

    async def close(self) -> WindowsPresentationSurfaceSnapshot:
        async with self._lock:
            if self._state == WindowsPresentationSurfaceState.CLOSED:
                return self.snapshot
            try:
                self._destroy_surface()
            except _NativeSurfaceError:
                self._state = WindowsPresentationSurfaceState.FAILED
                raise WindowsPresentationSurfaceError(
                    WindowsPresentationSurfaceErrorCode.CLEANUP_FAILURE,
                    "presentation surface cleanup failed",
                ) from None
            self._native_api = None
            self._state = WindowsPresentationSurfaceState.CLOSED
            return self.snapshot
