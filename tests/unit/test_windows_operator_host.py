from __future__ import annotations

import asyncio
from collections.abc import Sequence

import pytest

from k5vision.media.mixed_presentation import MixedPresentationStream
from k5vision.media.viewport_geometry import ViewportGeometry, ViewportLayout, ViewportPlacement
from k5vision.media.windows_operator_host import (
    BoundedWindowsOperatorHost,
    WindowsOperatorHostError,
    WindowsOperatorHostErrorCode,
    WindowsOperatorHostState,
)
from k5vision.media.windows_operator_runtime import (
    WindowsOperatorRuntimeSnapshot,
    WindowsOperatorRuntimeState,
)


def _layout(offset: int = 0) -> ViewportLayout:
    return ViewportLayout(
        placements=(
            ViewportPlacement(
                logical_slot=7,
                geometry=ViewportGeometry(
                    x=17 + offset,
                    y=29 + offset,
                    width=613,
                    height=347,
                ),
            ),
            ViewportPlacement(
                logical_slot=4095,
                geometry=ViewportGeometry(
                    x=701 + offset,
                    y=41 + offset,
                    width=211,
                    height=719,
                ),
            ),
        )
    )


def _snapshot(
    state: WindowsOperatorRuntimeState,
    *,
    open_surfaces: int,
    delivered_frames: int = 1,
    presentations: int = 1,
) -> WindowsOperatorRuntimeSnapshot:
    return WindowsOperatorRuntimeSnapshot(
        state=state,
        viewport_count=2,
        open_surface_count=open_surfaces,
        stream_count=2,
        delivered_frames=delivered_frames,
        presentations=presentations,
    )


class FakeOperatorRuntime:
    def __init__(
        self,
        layout: ViewportLayout,
        events: list[str],
        name: str,
        *,
        fail_start: bool = False,
        fail_close: bool = False,
    ) -> None:
        self.layout = layout
        self.events = events
        self.name = name
        self.fail_start = fail_start
        self.fail_close = fail_close
        self._snapshot = _snapshot(
            WindowsOperatorRuntimeState.READY,
            open_surfaces=0,
            delivered_frames=0,
            presentations=0,
        )

    @property
    def snapshot(self) -> WindowsOperatorRuntimeSnapshot:
        return self._snapshot

    async def start(
        self,
        _streams: Sequence[MixedPresentationStream],
    ) -> WindowsOperatorRuntimeSnapshot:
        self.events.append(f"start:{self.name}")
        if self.fail_start:
            raise RuntimeError("SECRET start detail")
        self._snapshot = _snapshot(WindowsOperatorRuntimeState.RUNNING, open_surfaces=2)
        return self._snapshot

    async def wait(self) -> WindowsOperatorRuntimeSnapshot:
        self.events.append(f"wait:{self.name}")
        self._snapshot = _snapshot(WindowsOperatorRuntimeState.COMPLETE, open_surfaces=2)
        return self._snapshot

    async def stop(self) -> WindowsOperatorRuntimeSnapshot:
        self.events.append(f"stop:{self.name}")
        self._snapshot = _snapshot(WindowsOperatorRuntimeState.STOPPED, open_surfaces=0)
        return self._snapshot

    async def close(self) -> WindowsOperatorRuntimeSnapshot:
        self.events.append(f"close:{self.name}")
        if self.fail_close:
            raise RuntimeError("SECRET close detail")
        self._snapshot = _snapshot(WindowsOperatorRuntimeState.CLOSED, open_surfaces=0)
        return self._snapshot


class RuntimeFactory:
    def __init__(self, configurations: list[dict[str, bool]] | None = None) -> None:
        self.events: list[str] = []
        self.runtimes: list[FakeOperatorRuntime] = []
        self.configurations = configurations or []

    def __call__(self, layout: ViewportLayout) -> FakeOperatorRuntime:
        index = len(self.runtimes)
        configuration = self.configurations[index] if index < len(self.configurations) else {}
        runtime = FakeOperatorRuntime(
            layout,
            self.events,
            str(index + 1),
            **configuration,
        )
        self.runtimes.append(runtime)
        return runtime


def test_replace_releases_prior_generation_before_starting_successor() -> None:
    factory = RuntimeFactory()
    host = BoundedWindowsOperatorHost(runtime_factory=factory)

    async def scenario() -> object:
        first = await host.start(_layout(), ())
        assert first.state == WindowsOperatorHostState.RUNNING
        assert first.generation == 1
        assert first.open_surface_count == 2

        second = await host.replace(_layout(50), ())
        assert second.state == WindowsOperatorHostState.RUNNING
        assert second.generation == 2
        assert second.stopped_generations == 1
        assert second.delivered_frames == 2
        assert second.presentations == 2

        stopped = await host.stop()
        assert stopped.state == WindowsOperatorHostState.STOPPED
        assert stopped.open_surface_count == 0
        assert stopped.stopped_generations == 2
        return await host.close()

    closed = asyncio.run(scenario())
    assert factory.events == [
        "start:1",
        "stop:1",
        "close:1",
        "start:2",
        "stop:2",
        "close:2",
    ]
    assert factory.runtimes[0].layout == _layout()
    assert factory.runtimes[1].layout == _layout(50)
    assert closed.state == WindowsOperatorHostState.CLOSED
    assert closed.generation == 2

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


def test_wait_completed_generation_can_be_replaced() -> None:
    factory = RuntimeFactory()
    host = BoundedWindowsOperatorHost(runtime_factory=factory)

    async def scenario() -> object:
        await host.start(_layout(), ())
        completed = await host.wait()
        assert completed.state == WindowsOperatorHostState.COMPLETE
        assert completed.completed_generations == 1
        replacement = await host.replace(_layout(10), ())
        assert replacement.state == WindowsOperatorHostState.RUNNING
        assert replacement.generation == 2
        return await host.close()

    closed = asyncio.run(scenario())
    assert factory.events == ["start:1", "wait:1", "close:1", "start:2", "stop:2", "close:2"]
    assert closed.state == WindowsOperatorHostState.CLOSED


def test_failed_replacement_is_sanitized_and_fail_closed() -> None:
    factory = RuntimeFactory([{}, {"fail_start": True}])
    host = BoundedWindowsOperatorHost(runtime_factory=factory)

    async def scenario() -> None:
        await host.start(_layout(), ())
        with pytest.raises(WindowsOperatorHostError) as exc_info:
            await host.replace(_layout(25), ())
        assert exc_info.value.code == WindowsOperatorHostErrorCode.START_FAILURE
        assert "secret" not in str(exc_info.value).casefold()

    asyncio.run(scenario())
    assert host.snapshot.state == WindowsOperatorHostState.FAILED
    assert host.snapshot.failures == 1
    assert host.snapshot.open_surface_count == 0
    assert factory.events == [
        "start:1",
        "stop:1",
        "close:1",
        "start:2",
        "close:2",
    ]


def test_prior_cleanup_failure_blocks_replacement_and_is_sanitized() -> None:
    factory = RuntimeFactory([{"fail_close": True}])
    host = BoundedWindowsOperatorHost(runtime_factory=factory)

    async def scenario() -> None:
        await host.start(_layout(), ())
        with pytest.raises(WindowsOperatorHostError) as exc_info:
            await host.replace(_layout(25), ())
        assert exc_info.value.code == WindowsOperatorHostErrorCode.CLEANUP_FAILURE
        assert "secret" not in str(exc_info.value).casefold()

    asyncio.run(scenario())
    assert host.snapshot.state == WindowsOperatorHostState.FAILED
    assert host.snapshot.failures == 1
    assert len(factory.runtimes) == 1
    assert factory.events == ["start:1", "stop:1", "close:1"]


def test_generation_limit_preserves_running_generation() -> None:
    factory = RuntimeFactory()
    host = BoundedWindowsOperatorHost(runtime_factory=factory, max_generations=1)

    async def scenario() -> None:
        await host.start(_layout(), ())
        with pytest.raises(WindowsOperatorHostError) as exc_info:
            await host.replace(_layout(25), ())
        assert exc_info.value.code == WindowsOperatorHostErrorCode.GENERATION_LIMIT
        assert host.snapshot.state == WindowsOperatorHostState.RUNNING
        await host.close()

    asyncio.run(scenario())
    assert factory.events == ["start:1", "stop:1", "close:1"]


def test_invalid_layout_is_rejected_before_runtime_assembly() -> None:
    one = ViewportLayout(
        placements=(
            ViewportPlacement(
                logical_slot=7,
                geometry=ViewportGeometry(x=1, y=2, width=3, height=4),
            ),
        )
    )
    factory = RuntimeFactory()
    host = BoundedWindowsOperatorHost(runtime_factory=factory)

    with pytest.raises(WindowsOperatorHostError) as exc_info:
        asyncio.run(host.start(one, ()))
    assert exc_info.value.code == WindowsOperatorHostErrorCode.INVALID_CONFIGURATION
    assert factory.runtimes == []
