from __future__ import annotations

import asyncio

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
        self.blit_calls: list[tuple[int, int, int, int]] = []
        self.destroy_calls: list[int] = []
        self.fail_blit = False

    def create_surface(self, width: int, height: int) -> tuple[int, int, int]:
        return 101, 8192, width * 4

    def copy_frame(
        self,
        bits_pointer: int,
        native_stride_bytes: int,
        frame: PresentationVideoFrame,
    ) -> None:
        return None

    def blit_surface(
        self,
        handle: int,
        width: int,
        height: int,
        target_dc: int,
    ) -> None:
        if self.fail_blit:
            raise surface_module._NativeSurfaceError(surface_module._NativeSurfaceFailure.BLIT)
        self.blit_calls.append((handle, width, height, target_dc))

    def destroy_surface(self, handle: int) -> None:
        self.destroy_calls.append(handle)


def _frame() -> PresentationVideoFrame:
    return PresentationVideoFrame(
        payload=memoryview(bytearray(16)),
        width=2,
        height=2,
        stride_bytes=8,
        pixel_format=PixelFormat.BGRX,
        source_elapsed_ms=25,
    )


def test_target_operation_is_bounded_and_does_not_retain_target() -> None:
    async def scenario() -> None:
        native = FakeNativeApi()
        surface = BoundedWindowsPresentationSurface(native_api=native)
        await surface.open()
        await surface.present(_frame())

        target_dc = 987654321
        await surface.blit(target_dc)

        snapshot = surface.snapshot
        assert snapshot.state == WindowsPresentationSurfaceState.OPEN
        assert snapshot.blits == 1
        assert native.blit_calls == [(101, 2, 2, target_dc)]
        serialized = snapshot.model_dump_json()
        assert str(target_dc) not in serialized
        await surface.close()

    asyncio.run(scenario())


def test_target_requires_an_existing_surface() -> None:
    async def scenario() -> None:
        surface = BoundedWindowsPresentationSurface(native_api=FakeNativeApi())
        await surface.open()
        with pytest.raises(WindowsPresentationSurfaceError) as exc_info:
            await surface.blit(1)
        assert exc_info.value.code == WindowsPresentationSurfaceErrorCode.INVALID_STATE
        assert surface.snapshot.state == WindowsPresentationSurfaceState.OPEN

    asyncio.run(scenario())


@pytest.mark.parametrize("target_dc", [0, -1, True, object()])
def test_invalid_target_is_rejected_without_leaking_value(target_dc: object) -> None:
    async def scenario() -> None:
        surface = BoundedWindowsPresentationSurface(native_api=FakeNativeApi())
        await surface.open()
        await surface.present(_frame())
        with pytest.raises(WindowsPresentationSurfaceError) as exc_info:
            await surface.blit(target_dc)  # type: ignore[arg-type]
        assert exc_info.value.code == WindowsPresentationSurfaceErrorCode.INVALID_TARGET
        assert repr(target_dc) not in str(exc_info.value)
        assert surface.snapshot.state == WindowsPresentationSurfaceState.OPEN
        await surface.close()

    asyncio.run(scenario())


def test_native_target_failure_is_sanitized_and_fails_closed() -> None:
    async def scenario() -> None:
        native = FakeNativeApi()
        native.fail_blit = True
        surface = BoundedWindowsPresentationSurface(native_api=native)
        await surface.open()
        await surface.present(_frame())

        with pytest.raises(WindowsPresentationSurfaceError) as exc_info:
            await surface.blit(123456789)

        assert exc_info.value.code == WindowsPresentationSurfaceErrorCode.BLIT_FAILURE
        assert "123456789" not in str(exc_info.value)
        assert surface.snapshot.state == WindowsPresentationSurfaceState.FAILED
        assert not surface.snapshot.surface_open
        assert native.destroy_calls == [101]

    asyncio.run(scenario())


def test_target_operation_limit_fails_closed() -> None:
    async def scenario() -> None:
        native = FakeNativeApi()
        surface = BoundedWindowsPresentationSurface(native_api=native, max_blits=1)
        await surface.open()
        await surface.present(_frame())
        await surface.blit(7)

        with pytest.raises(WindowsPresentationSurfaceError) as exc_info:
            await surface.blit(8)

        assert exc_info.value.code == WindowsPresentationSurfaceErrorCode.BLIT_LIMIT
        assert surface.snapshot.state == WindowsPresentationSurfaceState.FAILED
        assert surface.snapshot.blits == 1
        assert native.destroy_calls == [101]

    asyncio.run(scenario())


@pytest.mark.parametrize("max_blits", [0, 1_000_001])
def test_target_operation_bound_is_validated(max_blits: int) -> None:
    with pytest.raises(ValueError):
        BoundedWindowsPresentationSurface(max_blits=max_blits)
