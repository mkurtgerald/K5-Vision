from __future__ import annotations

import asyncio
from collections.abc import Sequence

import pytest

from k5vision.media.mixed_presentation import MixedPresentationStream
from k5vision.media.presentation_runtime import (
    PresentationRuntimeSnapshot,
    PresentationRuntimeState,
)
from k5vision.media.viewport_dispatch import ViewportBinding
from k5vision.media.viewport_geometry import ViewportGeometry, ViewportLayout, ViewportPlacement
from k5vision.media.windows_operator_runtime import (
    BoundedWindowsOperatorRuntime,
    WindowsOperatorRuntimeError,
    WindowsOperatorRuntimeErrorCode,
    WindowsOperatorRuntimeState,
)
from k5vision.media.windows_viewport_runtime import (
    WindowsViewportRuntimeSnapshot,
    WindowsViewportRuntimeState,
)


def _layout() -> ViewportLayout:
    return ViewportLayout(
        placements=(
            ViewportPlacement(
                logical_slot=7,
                geometry=ViewportGeometry(x=17, y=29, width=613, height=347),
            ),
            ViewportPlacement(
                logical_slot=4095,
                geometry=ViewportGeometry(x=701, y=41, width=211, height=719),
            ),
        )
    )


def _windows_snapshot(
    state: WindowsViewportRuntimeState,
    *,
    open_surfaces: int,
    presentations: int = 0,
) -> WindowsViewportRuntimeSnapshot:
    return WindowsViewportRuntimeSnapshot(
        state=state,
        viewport_count=2,
        open_surface_count=open_surfaces,
        presentations=presentations,
    )


def _presentation_snapshot(
    state: PresentationRuntimeState,
    *,
    delivered_frames: int = 0,
) -> PresentationRuntimeSnapshot:
    return PresentationRuntimeSnapshot(
        state=state,
        stream_count=2,
        live_streams=1,
        playback_streams=1,
        viewport_count=2,
        completed_streams=2 if state == PresentationRuntimeState.COMPLETE else 0,
        delivered_frames=delivered_frames,
        delivered_frame_bytes=delivered_frames * 16,
        max_source_span_ms=9 if delivered_frames else 0,
    )


async def _consumer(_frame: object) -> None:
    return None


class FakeWindowsRuntime:
    def __init__(self, layout: ViewportLayout) -> None:
        self.layout = layout
        self.opened = 0
        self.closed = 0
        self.fail_open = False
        self.fail_close = False
        self._snapshot = _windows_snapshot(
            WindowsViewportRuntimeState.READY,
            open_surfaces=0,
        )
        self._bindings = (
            ViewportBinding(slot=7, consumer=_consumer),
            ViewportBinding(slot=4095, consumer=_consumer),
        )

    @property
    def snapshot(self) -> WindowsViewportRuntimeSnapshot:
        return self._snapshot

    @property
    def bindings(self) -> tuple[ViewportBinding, ...]:
        return self._bindings

    async def open(self) -> WindowsViewportRuntimeSnapshot:
        self.opened += 1
        if self.fail_open:
            raise RuntimeError("SECRET Windows open detail")
        self._snapshot = _windows_snapshot(
            WindowsViewportRuntimeState.OPEN,
            open_surfaces=2,
        )
        return self._snapshot

    async def close(self) -> WindowsViewportRuntimeSnapshot:
        self.closed += 1
        if self.fail_close:
            raise RuntimeError("SECRET Windows cleanup detail")
        self._snapshot = _windows_snapshot(
            WindowsViewportRuntimeState.CLOSED,
            open_surfaces=0,
        )
        return self._snapshot


class FakePresentationRuntime:
    def __init__(self, bindings: Sequence[ViewportBinding]) -> None:
        self.bindings = tuple(bindings)
        self.started: list[Sequence[MixedPresentationStream]] = []
        self.waited = 0
        self.stopped = 0
        self.closed = 0
        self.fail_start = False
        self.fail_wait = False
        self.fail_stop = False
        self.fail_close = False
        self._snapshot = _presentation_snapshot(PresentationRuntimeState.CREATED)

    @property
    def snapshot(self) -> PresentationRuntimeSnapshot:
        return self._snapshot

    async def start(
        self,
        streams: Sequence[MixedPresentationStream],
    ) -> PresentationRuntimeSnapshot:
        self.started.append(streams)
        if self.fail_start:
            raise RuntimeError("SECRET start detail")
        self._snapshot = _presentation_snapshot(PresentationRuntimeState.RUNNING)
        return self._snapshot

    async def wait(self) -> PresentationRuntimeSnapshot:
        self.waited += 1
        if self.fail_wait:
            raise RuntimeError("SECRET wait detail")
        self._snapshot = _presentation_snapshot(
            PresentationRuntimeState.COMPLETE,
            delivered_frames=2,
        )
        return self._snapshot

    async def stop(self) -> PresentationRuntimeSnapshot:
        self.stopped += 1
        if self.fail_stop:
            raise RuntimeError("SECRET stop detail")
        self._snapshot = _presentation_snapshot(
            PresentationRuntimeState.STOPPED,
            delivered_frames=1,
        )
        return self._snapshot

    async def close(self) -> PresentationRuntimeSnapshot:
        self.closed += 1
        if self.fail_close:
            raise RuntimeError("SECRET media cleanup detail")
        self._snapshot = _presentation_snapshot(PresentationRuntimeState.CLOSED)
        return self._snapshot


def test_start_wait_close_preserves_sparse_bindings_and_safe_snapshot() -> None:
    windows: list[FakeWindowsRuntime] = []
    presentations: list[FakePresentationRuntime] = []

    def windows_factory(layout: ViewportLayout) -> FakeWindowsRuntime:
        runtime = FakeWindowsRuntime(layout)
        windows.append(runtime)
        return runtime

    def presentation_factory(
        bindings: Sequence[ViewportBinding],
    ) -> FakePresentationRuntime:
        runtime = FakePresentationRuntime(bindings)
        presentations.append(runtime)
        return runtime

    runtime = BoundedWindowsOperatorRuntime(
        _layout(),
        windows_runtime_factory=windows_factory,
        presentation_runtime_factory=presentation_factory,
    )
    streams: tuple[MixedPresentationStream, ...] = ()

    async def scenario() -> object:
        started = await runtime.start(streams)
        assert started.state == WindowsOperatorRuntimeState.RUNNING
        completed = await runtime.wait()
        assert completed.state == WindowsOperatorRuntimeState.COMPLETE
        assert completed.delivered_frames == 2
        return await runtime.close()

    closed = asyncio.run(scenario())
    assert windows[0].opened == 1
    assert windows[0].closed == 1
    assert [binding.slot for binding in presentations[0].bindings] == [7, 4095]
    assert presentations[0].started == [streams]
    assert presentations[0].waited == 1
    assert presentations[0].closed == 1
    assert closed.state == WindowsOperatorRuntimeState.CLOSED
    assert closed.open_surface_count == 0

    serialized = closed.model_dump_json().casefold()
    for forbidden in (
        "4095",
        "logical_slot",
        "rtsp://",
        "source_id",
        "recording_id",
        "path",
        "handle",
        "pointer",
        "payload",
    ):
        assert forbidden not in serialized


def test_windows_and_start_failures_are_sanitized_and_fail_closed() -> None:
    windows: list[FakeWindowsRuntime] = []

    def bad_windows(layout: ViewportLayout) -> FakeWindowsRuntime:
        runtime = FakeWindowsRuntime(layout)
        runtime.fail_open = True
        windows.append(runtime)
        return runtime

    failed_windows = BoundedWindowsOperatorRuntime(
        _layout(),
        windows_runtime_factory=bad_windows,
    )
    with pytest.raises(WindowsOperatorRuntimeError) as windows_exc:
        asyncio.run(failed_windows.start(()))
    assert windows_exc.value.code == WindowsOperatorRuntimeErrorCode.WINDOWS_OPEN_FAILURE
    assert "secret" not in str(windows_exc.value).casefold()
    assert failed_windows.snapshot.state == WindowsOperatorRuntimeState.FAILED
    assert windows[0].closed == 1

    presentations: list[FakePresentationRuntime] = []

    def presentation_factory(
        bindings: Sequence[ViewportBinding],
    ) -> FakePresentationRuntime:
        runtime = FakePresentationRuntime(bindings)
        runtime.fail_start = True
        presentations.append(runtime)
        return runtime

    failed_start = BoundedWindowsOperatorRuntime(
        _layout(),
        windows_runtime_factory=FakeWindowsRuntime,
        presentation_runtime_factory=presentation_factory,
    )
    with pytest.raises(WindowsOperatorRuntimeError) as start_exc:
        asyncio.run(failed_start.start(()))
    assert start_exc.value.code == WindowsOperatorRuntimeErrorCode.START_FAILURE
    assert "secret" not in str(start_exc.value).casefold()
    assert failed_start.snapshot.state == WindowsOperatorRuntimeState.FAILED
    assert presentations[0].closed == 1


def test_wait_failure_and_stop_release_both_children() -> None:
    windows: list[FakeWindowsRuntime] = []
    presentations: list[FakePresentationRuntime] = []

    def windows_factory(layout: ViewportLayout) -> FakeWindowsRuntime:
        runtime = FakeWindowsRuntime(layout)
        windows.append(runtime)
        return runtime

    def bad_wait_factory(
        bindings: Sequence[ViewportBinding],
    ) -> FakePresentationRuntime:
        runtime = FakePresentationRuntime(bindings)
        runtime.fail_wait = True
        presentations.append(runtime)
        return runtime

    failed_wait = BoundedWindowsOperatorRuntime(
        _layout(),
        windows_runtime_factory=windows_factory,
        presentation_runtime_factory=bad_wait_factory,
    )

    async def fail_wait() -> None:
        await failed_wait.start(())
        with pytest.raises(WindowsOperatorRuntimeError) as exc_info:
            await failed_wait.wait()
        assert exc_info.value.code == WindowsOperatorRuntimeErrorCode.EXECUTION_FAILURE
        assert "secret" not in str(exc_info.value).casefold()

    asyncio.run(fail_wait())
    assert failed_wait.snapshot.state == WindowsOperatorRuntimeState.FAILED
    assert presentations[0].closed == 1
    assert windows[0].closed == 1

    windows.clear()
    presentations.clear()

    def good_factory(bindings: Sequence[ViewportBinding]) -> FakePresentationRuntime:
        runtime = FakePresentationRuntime(bindings)
        presentations.append(runtime)
        return runtime

    stopped_runtime = BoundedWindowsOperatorRuntime(
        _layout(),
        windows_runtime_factory=windows_factory,
        presentation_runtime_factory=good_factory,
    )

    async def stop() -> object:
        await stopped_runtime.start(())
        return await stopped_runtime.stop()

    stopped = asyncio.run(stop())
    assert stopped.state == WindowsOperatorRuntimeState.STOPPED
    assert stopped.open_surface_count == 0
    assert presentations[0].stopped == 1
    assert presentations[0].closed == 1
    assert windows[0].closed == 1


def test_cleanup_failure_attempts_both_children() -> None:
    windows: list[FakeWindowsRuntime] = []
    presentations: list[FakePresentationRuntime] = []

    def windows_factory(layout: ViewportLayout) -> FakeWindowsRuntime:
        runtime = FakeWindowsRuntime(layout)
        runtime.fail_close = True
        windows.append(runtime)
        return runtime

    def presentation_factory(
        bindings: Sequence[ViewportBinding],
    ) -> FakePresentationRuntime:
        runtime = FakePresentationRuntime(bindings)
        runtime.fail_close = True
        presentations.append(runtime)
        return runtime

    runtime = BoundedWindowsOperatorRuntime(
        _layout(),
        windows_runtime_factory=windows_factory,
        presentation_runtime_factory=presentation_factory,
    )

    async def scenario() -> None:
        await runtime.start(())
        with pytest.raises(WindowsOperatorRuntimeError) as exc_info:
            await runtime.close()
        assert exc_info.value.code == WindowsOperatorRuntimeErrorCode.CLEANUP_FAILURE
        assert "secret" not in str(exc_info.value).casefold()

    asyncio.run(scenario())
    assert runtime.snapshot.state == WindowsOperatorRuntimeState.FAILED
    assert presentations[0].closed == 1
    assert windows[0].closed == 1


def test_invalid_configuration_and_state_are_bounded() -> None:
    one = ViewportLayout(
        placements=(
            ViewportPlacement(
                logical_slot=7,
                geometry=ViewportGeometry(x=1, y=2, width=3, height=4),
            ),
        )
    )
    with pytest.raises(WindowsOperatorRuntimeError) as invalid_exc:
        BoundedWindowsOperatorRuntime(one)
    assert invalid_exc.value.code == WindowsOperatorRuntimeErrorCode.INVALID_CONFIGURATION

    runtime = BoundedWindowsOperatorRuntime(
        _layout(),
        windows_runtime_factory=FakeWindowsRuntime,
        presentation_runtime_factory=FakePresentationRuntime,
    )
    with pytest.raises(WindowsOperatorRuntimeError) as state_exc:
        asyncio.run(runtime.wait())
    assert state_exc.value.code == WindowsOperatorRuntimeErrorCode.INVALID_STATE
