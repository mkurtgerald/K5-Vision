from __future__ import annotations

import asyncio

import pytest

import k5vision.media.windows_presentation_target as target_module
from k5vision.media.windows_presentation_target import (
    BoundedWindowsPresentationTarget,
    WindowsPresentationTargetError,
    WindowsPresentationTargetErrorCode,
    WindowsPresentationTargetState,
)


class FakeNativeApi:
    def __init__(self) -> None:
        self.created: list[tuple[int, int, int, int]] = []
        self.acquired: list[int] = []
        self.released: list[tuple[int, int]] = []
        self.destroyed: list[int] = []
        self.fail_create = False
        self.fail_acquire = False
        self.fail_release = False
        self.fail_destroy = False

    def create_target(self, x: int, y: int, width: int, height: int) -> int:
        if self.fail_create:
            raise target_module._NativeTargetError(target_module._NativeTargetFailure.CREATE)
        self.created.append((x, y, width, height))
        return 101

    def acquire_dc(self, target: int) -> int:
        if self.fail_acquire:
            raise target_module._NativeTargetError(target_module._NativeTargetFailure.ACQUIRE_DC)
        self.acquired.append(target)
        return 202

    def release_dc(self, target: int, target_dc: int) -> None:
        if self.fail_release:
            raise target_module._NativeTargetError(target_module._NativeTargetFailure.RELEASE_DC)
        self.released.append((target, target_dc))

    def destroy_target(self, target: int) -> None:
        self.destroyed.append(target)
        if self.fail_destroy:
            raise target_module._NativeTargetError(target_module._NativeTargetFailure.DESTROY)


class FakeSurface:
    def __init__(self) -> None:
        self.calls: list[int] = []
        self.fail = False

    async def blit(self, target_dc: int) -> None:
        self.calls.append(target_dc)
        if self.fail:
            raise RuntimeError("C:\\private\\target-secret")


def test_open_present_close_is_bounded_positioned_and_handle_free() -> None:
    async def scenario() -> None:
        native = FakeNativeApi()
        surface = FakeSurface()
        target = BoundedWindowsPresentationTarget(native_api=native)

        opened = await target.open(640, 480, x=17, y=29)
        assert opened.state == WindowsPresentationTargetState.OPEN
        assert opened.target_open
        assert opened.x == 17
        assert opened.y == 29
        assert opened.width == 640
        assert opened.height == 480
        assert native.created == [(17, 29, 640, 480)]

        current = await target.present(surface)
        assert current.presentations == 1
        assert surface.calls == [202]
        assert native.released == [(101, 202)]

        serialized = current.model_dump_json()
        assert "101" not in serialized
        assert "202" not in serialized

        closed = await target.close()
        assert closed.state == WindowsPresentationTargetState.CLOSED
        assert not closed.target_open
        assert closed.x == 0
        assert closed.y == 0
        assert native.destroyed == [101]
        assert (await target.close()).state == WindowsPresentationTargetState.CLOSED

    asyncio.run(scenario())


def test_matching_open_is_idempotent_but_geometry_change_is_rejected() -> None:
    async def scenario() -> None:
        native = FakeNativeApi()
        target = BoundedWindowsPresentationTarget(native_api=native)
        await target.open(320, 240, x=5, y=7)
        again = await target.open(320, 240, x=5, y=7)
        assert again.state == WindowsPresentationTargetState.OPEN
        assert native.created == [(5, 7, 320, 240)]

        with pytest.raises(WindowsPresentationTargetError) as exc_info:
            await target.open(320, 240, x=6, y=7)
        assert exc_info.value.code == WindowsPresentationTargetErrorCode.INVALID_STATE
        await target.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("x", "y", "width", "height"),
    [
        (0, 0, 0, 1),
        (0, 0, 1, 0),
        (0, 0, -1, 1),
        (0, 0, 1, -1),
        (0, 0, 16_385, 1),
        (0, 0, 1, 16_385),
        (-1, 0, 1, 1),
        (0, -1, 1, 1),
        (1_000_001, 0, 1, 1),
        (0, 1_000_001, 1, 1),
        (True, 0, 1, 1),
        (0, True, 1, 1),
        (0, 0, True, 1),
    ],
)
def test_geometry_validation_is_explicit(
    x: object,
    y: object,
    width: object,
    height: object,
) -> None:
    async def scenario() -> None:
        target = BoundedWindowsPresentationTarget(native_api=FakeNativeApi())
        with pytest.raises(WindowsPresentationTargetError) as exc_info:
            await target.open(width, height, x=x, y=y)  # type: ignore[arg-type]
        assert exc_info.value.code == WindowsPresentationTargetErrorCode.INVALID_CONFIGURATION
        assert target.snapshot.state == WindowsPresentationTargetState.READY

    asyncio.run(scenario())


def test_presentation_failure_releases_dc_and_fails_closed() -> None:
    async def scenario() -> None:
        native = FakeNativeApi()
        surface = FakeSurface()
        surface.fail = True
        target = BoundedWindowsPresentationTarget(native_api=native)
        await target.open(64, 64)

        with pytest.raises(WindowsPresentationTargetError) as exc_info:
            await target.present(surface)

        assert exc_info.value.code == WindowsPresentationTargetErrorCode.PRESENTATION_FAILURE
        assert "private" not in str(exc_info.value).casefold()
        assert native.released == [(101, 202)]
        assert native.destroyed == [101]
        assert target.snapshot.state == WindowsPresentationTargetState.FAILED
        assert not target.snapshot.target_open

    asyncio.run(scenario())


def test_dc_acquire_and_release_failures_are_sanitized() -> None:
    async def acquire_failure() -> None:
        native = FakeNativeApi()
        native.fail_acquire = True
        target = BoundedWindowsPresentationTarget(native_api=native)
        await target.open(64, 64)
        with pytest.raises(WindowsPresentationTargetError) as exc_info:
            await target.present(FakeSurface())
        assert exc_info.value.code == WindowsPresentationTargetErrorCode.TARGET_DC_FAILURE
        assert target.snapshot.state == WindowsPresentationTargetState.FAILED
        assert native.destroyed == [101]

    asyncio.run(acquire_failure())

    async def release_failure() -> None:
        native = FakeNativeApi()
        native.fail_release = True
        target = BoundedWindowsPresentationTarget(native_api=native)
        await target.open(64, 64)
        with pytest.raises(WindowsPresentationTargetError) as exc_info:
            await target.present(FakeSurface())
        assert exc_info.value.code == WindowsPresentationTargetErrorCode.CLEANUP_FAILURE
        assert target.snapshot.state == WindowsPresentationTargetState.FAILED
        assert native.destroyed == [101]

    asyncio.run(release_failure())


def test_presentation_limit_fails_closed() -> None:
    async def scenario() -> None:
        native = FakeNativeApi()
        target = BoundedWindowsPresentationTarget(max_presentations=1, native_api=native)
        await target.open(64, 64)
        await target.present(FakeSurface())

        with pytest.raises(WindowsPresentationTargetError) as exc_info:
            await target.present(FakeSurface())

        assert exc_info.value.code == WindowsPresentationTargetErrorCode.PRESENTATION_LIMIT
        assert target.snapshot.state == WindowsPresentationTargetState.FAILED
        assert native.destroyed == [101]

    asyncio.run(scenario())


def test_invalid_surface_does_not_destroy_open_target() -> None:
    async def scenario() -> None:
        native = FakeNativeApi()
        target = BoundedWindowsPresentationTarget(native_api=native)
        await target.open(64, 64)

        with pytest.raises(WindowsPresentationTargetError) as exc_info:
            await target.present(object())  # type: ignore[arg-type]

        assert exc_info.value.code == WindowsPresentationTargetErrorCode.INVALID_CONFIGURATION
        assert target.snapshot.state == WindowsPresentationTargetState.OPEN
        assert native.destroyed == []
        await target.close()

    asyncio.run(scenario())


def test_close_cleanup_failure_is_sanitized() -> None:
    async def scenario() -> None:
        native = FakeNativeApi()
        native.fail_destroy = True
        target = BoundedWindowsPresentationTarget(native_api=native)
        await target.open(64, 64)

        with pytest.raises(WindowsPresentationTargetError) as exc_info:
            await target.close()

        assert exc_info.value.code == WindowsPresentationTargetErrorCode.CLEANUP_FAILURE
        assert target.snapshot.state == WindowsPresentationTargetState.FAILED
        assert not target.snapshot.target_open

    asyncio.run(scenario())


def test_default_target_fails_closed_off_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(target_module.sys, "platform", "not-win32")
        target = BoundedWindowsPresentationTarget()
        with pytest.raises(WindowsPresentationTargetError) as exc_info:
            await target.open(64, 64)
        assert exc_info.value.code == WindowsPresentationTargetErrorCode.UNSUPPORTED_PLATFORM
        assert target.snapshot.state == WindowsPresentationTargetState.FAILED

    asyncio.run(scenario())


@pytest.mark.parametrize("limit", [0, 1_000_001])
def test_presentation_bound_is_validated(limit: int) -> None:
    with pytest.raises(ValueError):
        BoundedWindowsPresentationTarget(max_presentations=limit)
