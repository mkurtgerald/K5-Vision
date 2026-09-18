from __future__ import annotations

import asyncio
import ctypes
import typing

import pytest

import k5vision.media.windows_presentation_surface as surface_module
from k5vision.media.presentation_frame import PixelFormat, PresentationVideoFrame
from k5vision.media.windows_presentation_surface import (
    BoundedWindowsPresentationSurface,
    WindowsPresentationSurfaceError,
    WindowsPresentationSurfaceErrorCode,
    WindowsPresentationSurfaceState,
)


class FakeNativeApi:
    def __init__(self) -> None:
        self.create_calls: list[tuple[int, int]] = []
        self.copy_calls: list[tuple[int, int, int, int]] = []
        self.destroy_calls: list[int] = []
        self.fail_create = False
        self.fail_copy = False
        self.fail_destroy = False
        self.next_handle = 100

    def create_surface(self, width: int, height: int) -> tuple[int, int, int]:
        if self.fail_create:
            raise surface_module._NativeSurfaceError(surface_module._NativeSurfaceFailure.CREATE)
        self.create_calls.append((width, height))
        handle = self.next_handle
        self.next_handle += 1
        return handle, 4096 + handle, width * 4

    def copy_frame(
        self,
        bits_pointer: int,
        native_stride_bytes: int,
        frame: PresentationVideoFrame,
    ) -> None:
        if self.fail_copy:
            raise surface_module._NativeSurfaceError(surface_module._NativeSurfaceFailure.COPY)
        self.copy_calls.append((bits_pointer, native_stride_bytes, frame.width, len(frame.payload)))

    def destroy_surface(self, handle: int) -> None:
        self.destroy_calls.append(handle)
        if self.fail_destroy:
            raise surface_module._NativeSurfaceError(surface_module._NativeSurfaceFailure.DESTROY)


def _frame(
    width: int = 2,
    height: int = 2,
    *,
    stride_bytes: int | None = None,
    source_elapsed_ms: int = 25,
) -> PresentationVideoFrame:
    stride = stride_bytes if stride_bytes is not None else width * 4
    return PresentationVideoFrame(
        payload=memoryview(bytearray(stride * height)),
        width=width,
        height=height,
        stride_bytes=stride,
        pixel_format=PixelFormat.BGRX,
        source_elapsed_ms=source_elapsed_ms,
    )


def test_surface_open_present_close_is_bounded_and_source_free() -> None:
    async def scenario() -> None:
        native = FakeNativeApi()
        surface = BoundedWindowsPresentationSurface(native_api=native)

        opened = await surface.open()
        assert opened.state == WindowsPresentationSurfaceState.OPEN
        assert not opened.surface_open

        await surface.present(_frame(source_elapsed_ms=33))
        await surface(_frame(source_elapsed_ms=66))
        current = surface.snapshot
        assert current.state == WindowsPresentationSurfaceState.OPEN
        assert current.surface_open
        assert current.width == 2
        assert current.height == 2
        assert current.native_stride_bytes == 8
        assert current.presented_frames == 2
        assert current.presented_frame_bytes == 32
        assert current.max_source_span_ms == 66
        assert current.surface_replacements == 0
        assert native.create_calls == [(2, 2)]
        assert len(native.copy_calls) == 2

        payload = current.model_dump_json().casefold()
        for forbidden in ("rtsp://", "credential", "password", "recording_id", "payload"):
            assert forbidden not in payload

        closed = await surface.close()
        assert closed.state == WindowsPresentationSurfaceState.CLOSED
        assert not closed.surface_open
        assert native.destroy_calls == [100]
        assert (await surface.close()).state == WindowsPresentationSurfaceState.CLOSED

    asyncio.run(scenario())


def test_geometry_change_replaces_native_surface_once() -> None:
    async def scenario() -> None:
        native = FakeNativeApi()
        surface = BoundedWindowsPresentationSurface(native_api=native)
        await surface.open()
        await surface.present(_frame(2, 2))
        await surface.present(_frame(3, 2))
        snapshot = surface.snapshot
        assert snapshot.width == 3
        assert snapshot.height == 2
        assert snapshot.surface_replacements == 1
        assert native.create_calls == [(2, 2), (3, 2)]
        assert native.destroy_calls == [100]
        await surface.close()
        assert native.destroy_calls == [100, 101]

    asyncio.run(scenario())


def test_surface_replacement_limit_fails_closed() -> None:
    async def scenario() -> None:
        native = FakeNativeApi()
        surface = BoundedWindowsPresentationSurface(
            native_api=native,
            max_surface_replacements=0,
        )
        await surface.open()
        await surface.present(_frame(2, 2))
        with pytest.raises(WindowsPresentationSurfaceError) as exc_info:
            await surface.present(_frame(3, 2))
        assert exc_info.value.code == WindowsPresentationSurfaceErrorCode.SURFACE_REPLACEMENT_LIMIT
        assert surface.snapshot.state == WindowsPresentationSurfaceState.FAILED
        assert not surface.snapshot.surface_open
        assert native.destroy_calls == [100]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("kwargs", "second_frame", "expected"),
    [
        ({"max_frames": 1}, _frame(), WindowsPresentationSurfaceErrorCode.FRAME_LIMIT),
        (
            {"max_frame_bytes": 15},
            _frame(),
            WindowsPresentationSurfaceErrorCode.FRAME_BYTES_LIMIT,
        ),
        (
            {"max_total_frame_bytes": 20},
            _frame(),
            WindowsPresentationSurfaceErrorCode.TOTAL_BYTES_LIMIT,
        ),
    ],
)
def test_surface_limits_fail_closed(
    kwargs: dict[str, int],
    second_frame: PresentationVideoFrame,
    expected: WindowsPresentationSurfaceErrorCode,
) -> None:
    async def scenario() -> None:
        native = FakeNativeApi()
        surface = BoundedWindowsPresentationSurface(native_api=native, **kwargs)
        await surface.open()
        if expected != WindowsPresentationSurfaceErrorCode.FRAME_BYTES_LIMIT:
            await surface.present(_frame())
        with pytest.raises(WindowsPresentationSurfaceError) as exc_info:
            await surface.present(second_frame)
        assert exc_info.value.code == expected
        assert surface.snapshot.state == WindowsPresentationSurfaceState.FAILED
        assert not surface.snapshot.surface_open

    asyncio.run(scenario())


def test_native_create_and_copy_failures_are_sanitized() -> None:
    async def scenario() -> None:
        create_native = FakeNativeApi()
        create_native.fail_create = True
        create_surface = BoundedWindowsPresentationSurface(native_api=create_native)
        await create_surface.open()
        with pytest.raises(WindowsPresentationSurfaceError) as create_exc:
            await create_surface.present(_frame())
        assert create_exc.value.code == WindowsPresentationSurfaceErrorCode.SURFACE_CREATE_FAILURE
        assert "4096" not in str(create_exc.value)

        copy_native = FakeNativeApi()
        copy_native.fail_copy = True
        copy_surface = BoundedWindowsPresentationSurface(native_api=copy_native)
        await copy_surface.open()
        with pytest.raises(WindowsPresentationSurfaceError) as copy_exc:
            await copy_surface.present(_frame())
        assert copy_exc.value.code == WindowsPresentationSurfaceErrorCode.SURFACE_COPY_FAILURE
        assert copy_surface.snapshot.state == WindowsPresentationSurfaceState.FAILED
        assert copy_native.destroy_calls == [100]

    asyncio.run(scenario())


def test_cleanup_failure_is_sanitized() -> None:
    async def scenario() -> None:
        native = FakeNativeApi()
        surface = BoundedWindowsPresentationSurface(native_api=native)
        await surface.open()
        await surface.present(_frame())
        native.fail_destroy = True
        with pytest.raises(WindowsPresentationSurfaceError) as exc_info:
            await surface.close()
        assert exc_info.value.code == WindowsPresentationSurfaceErrorCode.CLEANUP_FAILURE
        assert surface.snapshot.state == WindowsPresentationSurfaceState.FAILED
        assert not surface.snapshot.surface_open

    asyncio.run(scenario())


def test_invalid_state_and_invalid_frame_are_explicit() -> None:
    async def scenario() -> None:
        native = FakeNativeApi()
        surface = BoundedWindowsPresentationSurface(native_api=native)
        with pytest.raises(WindowsPresentationSurfaceError) as before_open:
            await surface.present(_frame())
        assert before_open.value.code == WindowsPresentationSurfaceErrorCode.INVALID_STATE

        await surface.open()
        with pytest.raises(WindowsPresentationSurfaceError) as invalid_frame:
            await surface.present(typing.cast(PresentationVideoFrame, object()))
        assert invalid_frame.value.code == WindowsPresentationSurfaceErrorCode.INVALID_FRAME
        assert surface.snapshot.state == WindowsPresentationSurfaceState.FAILED

        with pytest.raises(WindowsPresentationSurfaceError) as reopen:
            await surface.open()
        assert reopen.value.code == WindowsPresentationSurfaceErrorCode.INVALID_STATE

    asyncio.run(scenario())


def test_default_surface_fails_closed_off_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(surface_module.sys, "platform", "not-win32")
        surface = BoundedWindowsPresentationSurface()
        with pytest.raises(WindowsPresentationSurfaceError) as exc_info:
            await surface.open()
        assert exc_info.value.code == WindowsPresentationSurfaceErrorCode.UNSUPPORTED_PLATFORM
        assert surface.snapshot.state == WindowsPresentationSurfaceState.FAILED

    asyncio.run(scenario())


def test_native_loader_failure_does_not_leak_details(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(surface_module.sys, "platform", "win32")

        def fail_loader(*args: object, **kwargs: object) -> object:
            raise OSError("C:\\private\\native-secret")

        monkeypatch.setattr(surface_module.ctypes, "WinDLL", fail_loader, raising=False)
        surface = BoundedWindowsPresentationSurface()
        with pytest.raises(WindowsPresentationSurfaceError) as exc_info:
            await surface.open()
        assert exc_info.value.code == WindowsPresentationSurfaceErrorCode.NATIVE_LOAD_FAILURE
        assert "private" not in str(exc_info.value).casefold()
        assert "native-secret" not in str(exc_info.value).casefold()

    asyncio.run(scenario())


def test_native_copy_handles_padded_stride_without_retaining_source() -> None:
    native = object.__new__(surface_module._Win32DibSurfaceApi)
    frame = _frame(2, 2, stride_bytes=12)
    first = bytes(range(8))
    second = bytes(range(8, 16))
    raw = bytearray(first + b"XXXX" + second + b"YYYY")
    frame = PresentationVideoFrame(
        payload=memoryview(raw),
        width=2,
        height=2,
        stride_bytes=12,
        pixel_format=PixelFormat.BGRX,
        source_elapsed_ms=1,
    )
    destination = ctypes.create_string_buffer(16)
    native.copy_frame(ctypes.addressof(destination), 8, frame)
    assert bytes(destination.raw) == first + second


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_frames": 0},
        {"max_frame_bytes": 0},
        {"max_total_frame_bytes": 0},
        {"max_surface_replacements": -1},
        {"max_surface_replacements": 1025},
    ],
)
def test_constructor_bounds(kwargs: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        BoundedWindowsPresentationSurface(**kwargs)
