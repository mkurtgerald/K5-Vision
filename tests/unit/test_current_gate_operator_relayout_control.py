from __future__ import annotations

import asyncio

import pytest

from k5vision.media.mixed_presentation import MixedLiveStream
from k5vision.media.presentation_runtime import PresentationRuntimeSnapshot, PresentationRuntimeState
from k5vision.media.viewport_dispatch import ViewportBinding
from k5vision.media.viewport_geometry import ViewportGeometry, ViewportLayout, ViewportPlacement
from k5vision.media.windows_operator_application import (
    BoundedWindowsOperatorApplication,
    WindowsOperatorApplicationSnapshot,
    WindowsOperatorApplicationState,
)
from k5vision.media.windows_operator_control import BoundedWindowsOperatorControl
from k5vision.media.windows_operator_host import (
    BoundedWindowsOperatorHost,
    WindowsOperatorHostSnapshot,
    WindowsOperatorHostState,
)
from k5vision.media.windows_operator_runtime import (
    BoundedWindowsOperatorRuntime,
    WindowsOperatorRuntimeError,
    WindowsOperatorRuntimeErrorCode,
    WindowsOperatorRuntimeSnapshot,
    WindowsOperatorRuntimeState,
)
from k5vision.media.windows_viewport_runtime import (
    WindowsViewportRuntimeSnapshot,
    WindowsViewportRuntimeState,
)


def _layout(offset: int = 0, *, second_slot: int = 4095) -> ViewportLayout:
    return ViewportLayout(
        placements=(
            ViewportPlacement(
                logical_slot=7,
                geometry=ViewportGeometry(
                    x=17 + offset,
                    y=29 + offset,
                    width=613 - offset,
                    height=347 + offset,
                    z_index=2,
                ),
            ),
            ViewportPlacement(
                logical_slot=second_slot,
                geometry=ViewportGeometry(
                    x=701 - offset,
                    y=41 + offset,
                    width=211 + offset,
                    height=719 - offset,
                    z_index=1,
                ),
            ),
        )
    )


def _streams() -> tuple[MixedLiveStream, MixedLiveStream]:
    return (
        MixedLiveStream(slot=7, source_uri="execution-only-a", frame_limit=3),
        MixedLiveStream(slot=4095, source_uri="execution-only-b", frame_limit=3),
    )


async def _consume(_frame: object) -> None:
    return None


class FakeWindowsRuntime:
    def __init__(self, layout: ViewportLayout) -> None:
        self.layout = layout
        self.opened = 0
        self.closed = 0
        self.relayouts: list[ViewportLayout] = []

    @property
    def snapshot(self) -> WindowsViewportRuntimeSnapshot:
        state = WindowsViewportRuntimeState.OPEN if self.opened and not self.closed else WindowsViewportRuntimeState.READY
        if self.closed:
            state = WindowsViewportRuntimeState.CLOSED
        return WindowsViewportRuntimeSnapshot(
            state=state,
            viewport_count=2,
            open_surface_count=0 if self.closed else 2,
            presentations=0,
        )

    @property
    def bindings(self) -> tuple[ViewportBinding, ...]:
        return (
            ViewportBinding(slot=7, consumer=_consume),
            ViewportBinding(slot=4095, consumer=_consume),
        )

    async def open(self) -> WindowsViewportRuntimeSnapshot:
        self.opened += 1
        return self.snapshot

    async def relayout(self, layout: ViewportLayout) -> WindowsViewportRuntimeSnapshot:
        self.relayouts.append(layout)
        self.layout = layout
        return self.snapshot

    async def close(self) -> WindowsViewportRuntimeSnapshot:
        self.closed += 1
        return self.snapshot


class FakePresentationRuntime:
    def __init__(self) -> None:
        self.state = PresentationRuntimeState.CREATED
        self.start_calls = 0
        self.close_calls = 0
        self.stop_calls = 0
        self.wait_entered = asyncio.Event()
        self.finish = asyncio.Event()

    @property
    def snapshot(self) -> PresentationRuntimeSnapshot:
        return PresentationRuntimeSnapshot(
            state=self.state,
            stream_count=2 if self.state != PresentationRuntimeState.CREATED else 0,
            live_streams=2 if self.state != PresentationRuntimeState.CREATED else 0,
            playback_streams=0,
            viewport_count=2,
            completed_streams=0,
            delivered_frames=0,
            delivered_frame_bytes=0,
            max_source_span_ms=0,
        )

    async def start(self, _streams: object) -> PresentationRuntimeSnapshot:
        self.start_calls += 1
        self.state = PresentationRuntimeState.RUNNING
        return self.snapshot

    async def wait(self) -> PresentationRuntimeSnapshot:
        self.wait_entered.set()
        await self.finish.wait()
        self.state = PresentationRuntimeState.COMPLETE
        return self.snapshot

    async def stop(self) -> PresentationRuntimeSnapshot:
        self.stop_calls += 1
        self.state = PresentationRuntimeState.STOPPED
        self.finish.set()
        return self.snapshot

    async def close(self) -> PresentationRuntimeSnapshot:
        self.close_calls += 1
        self.state = PresentationRuntimeState.CLOSED
        self.finish.set()
        return self.snapshot


def test_operator_runtime_relayout_does_not_restart_media_or_block_waiter() -> None:
    windows: list[FakeWindowsRuntime] = []
    presentations: list[FakePresentationRuntime] = []

    def windows_factory(layout: ViewportLayout) -> FakeWindowsRuntime:
        runtime = FakeWindowsRuntime(layout)
        windows.append(runtime)
        return runtime

    def presentation_factory(_bindings: object) -> FakePresentationRuntime:
        runtime = FakePresentationRuntime()
        presentations.append(runtime)
        return runtime

    runtime = BoundedWindowsOperatorRuntime(
        _layout(),
        windows_runtime_factory=windows_factory,
        presentation_runtime_factory=presentation_factory,
    )
    replacement = _layout(37)

    async def scenario() -> None:
        started = await runtime.start(_streams())
        assert started.state == WindowsOperatorRuntimeState.RUNNING
        waiter = asyncio.create_task(runtime.wait())
        await presentations[0].wait_entered.wait()

        relayout = await asyncio.wait_for(runtime.relayout(replacement), timeout=0.25)
        assert relayout.state == WindowsOperatorRuntimeState.RUNNING
        assert presentations[0].start_calls == 1
        assert presentations[0].close_calls == 0
        assert windows[0].relayouts == [replacement]

        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert runtime.snapshot.state == WindowsOperatorRuntimeState.RUNNING
        assert presentations[0].close_calls == 0
        await runtime.close()

    asyncio.run(scenario())
    assert len(windows) == 1
    assert len(presentations) == 1


def test_operator_runtime_relayout_rejects_slot_set_change_without_failure() -> None:
    windows: list[FakeWindowsRuntime] = []
    presentations: list[FakePresentationRuntime] = []

    def windows_factory(layout: ViewportLayout) -> FakeWindowsRuntime:
        runtime = FakeWindowsRuntime(layout)
        windows.append(runtime)
        return runtime

    def presentation_factory(_bindings: object) -> FakePresentationRuntime:
        runtime = FakePresentationRuntime()
        presentations.append(runtime)
        return runtime

    runtime = BoundedWindowsOperatorRuntime(
        _layout(),
        windows_runtime_factory=windows_factory,
        presentation_runtime_factory=presentation_factory,
    )

    async def scenario() -> None:
        await runtime.start(_streams())
        with pytest.raises(WindowsOperatorRuntimeError) as exc_info:
            await runtime.relayout(_layout(5, second_slot=4000))
        assert exc_info.value.code == WindowsOperatorRuntimeErrorCode.INVALID_CONFIGURATION
        assert runtime.snapshot.state == WindowsOperatorRuntimeState.RUNNING
        assert windows[0].relayouts == []
        await runtime.close()

    asyncio.run(scenario())


class FakeHostRuntime:
    def __init__(self, _layout: ViewportLayout) -> None:
        self.state = WindowsOperatorRuntimeState.READY
        self.start_calls = 0
        self.stop_calls = 0
        self.close_calls = 0
        self.relayouts: list[ViewportLayout] = []

    @property
    def snapshot(self) -> WindowsOperatorRuntimeSnapshot:
        return WindowsOperatorRuntimeSnapshot(
            state=self.state,
            viewport_count=2,
            open_surface_count=2 if self.state == WindowsOperatorRuntimeState.RUNNING else 0,
            stream_count=2 if self.state == WindowsOperatorRuntimeState.RUNNING else 0,
            delivered_frames=0,
            presentations=0,
        )

    async def start(self, _streams: object) -> WindowsOperatorRuntimeSnapshot:
        self.start_calls += 1
        self.state = WindowsOperatorRuntimeState.RUNNING
        return self.snapshot

    async def relayout(self, layout: ViewportLayout) -> WindowsOperatorRuntimeSnapshot:
        self.relayouts.append(layout)
        return self.snapshot

    async def wait(self) -> WindowsOperatorRuntimeSnapshot:
        return self.snapshot

    async def stop(self) -> WindowsOperatorRuntimeSnapshot:
        self.stop_calls += 1
        self.state = WindowsOperatorRuntimeState.STOPPED
        return self.snapshot

    async def close(self) -> WindowsOperatorRuntimeSnapshot:
        self.close_calls += 1
        self.state = WindowsOperatorRuntimeState.CLOSED
        return self.snapshot


def test_host_relayout_preserves_generation_and_runtime_instance() -> None:
    runtimes: list[FakeHostRuntime] = []

    def runtime_factory(layout: ViewportLayout) -> FakeHostRuntime:
        runtime = FakeHostRuntime(layout)
        runtimes.append(runtime)
        return runtime

    host = BoundedWindowsOperatorHost(runtime_factory=runtime_factory)
    replacement = _layout(21)

    async def scenario() -> None:
        started = await host.start(_layout(), _streams())
        assert started.generation == 1
        relayout = await host.relayout(replacement)
        assert relayout.state == WindowsOperatorHostState.RUNNING
        assert relayout.generation == 1
        assert runtimes[0].start_calls == 1
        assert runtimes[0].stop_calls == 0
        assert runtimes[0].close_calls == 0
        assert runtimes[0].relayouts == [replacement]
        await host.close()

    asyncio.run(scenario())
    assert len(runtimes) == 1


class FakeNativeShell:
    def __init__(self) -> None:
        self.destroyed = 0

    def create_shell(self, _width: int, _height: int) -> int:
        return 77

    def pump_messages(self, _shell: int, _max_messages: int) -> tuple[int, bool]:
        return 0, False

    def destroy_shell(self, _shell: int) -> None:
        self.destroyed += 1


class FakeApplicationHost:
    def __init__(self) -> None:
        self.state = WindowsOperatorHostState.READY
        self.generation = 0
        self.start_calls = 0
        self.relayouts: list[ViewportLayout] = []
        self.close_calls = 0

    @property
    def snapshot(self) -> WindowsOperatorHostSnapshot:
        return WindowsOperatorHostSnapshot(
            state=self.state,
            generation=self.generation,
            generation_limit=8,
            completed_generations=0,
            stopped_generations=0,
            failures=0,
            viewport_count=2 if self.generation else 0,
            open_surface_count=2 if self.state == WindowsOperatorHostState.RUNNING else 0,
            stream_count=2 if self.state == WindowsOperatorHostState.RUNNING else 0,
            delivered_frames=0,
            presentations=0,
        )

    async def start(self, _layout: ViewportLayout, _streams: object) -> WindowsOperatorHostSnapshot:
        self.start_calls += 1
        self.generation = 1
        self.state = WindowsOperatorHostState.RUNNING
        return self.snapshot

    async def replace(self, _layout: ViewportLayout, _streams: object) -> WindowsOperatorHostSnapshot:
        self.generation += 1
        return self.snapshot

    async def relayout(self, layout: ViewportLayout) -> WindowsOperatorHostSnapshot:
        self.relayouts.append(layout)
        return self.snapshot

    async def wait(self) -> WindowsOperatorHostSnapshot:
        return self.snapshot

    async def stop(self) -> WindowsOperatorHostSnapshot:
        self.state = WindowsOperatorHostState.STOPPED
        return self.snapshot

    async def close(self) -> WindowsOperatorHostSnapshot:
        self.close_calls += 1
        self.state = WindowsOperatorHostState.CLOSED
        return self.snapshot


def test_application_relayout_preserves_shell_and_generation() -> None:
    native = FakeNativeShell()
    host = FakeApplicationHost()
    app = BoundedWindowsOperatorApplication(
        native_api=native,
        host_factory=lambda _parent: host,
    )
    replacement = _layout(17)

    async def scenario() -> None:
        await app.open(1280, 720)
        started = await app.start(_layout(), _streams())
        assert started.generation == 1
        relayout = await app.relayout(replacement)
        assert relayout.state == WindowsOperatorApplicationState.RUNNING
        assert relayout.shell_open is True
        assert relayout.generation == 1
        assert host.start_calls == 1
        assert host.relayouts == [replacement]
        assert native.destroyed == 0
        await app.close()

    asyncio.run(scenario())


class FakeControlApplication:
    def __init__(self) -> None:
        self.state = WindowsOperatorApplicationState.READY
        self.generation = 0
        self.relayouts: list[ViewportLayout] = []
        self.replace_calls = 0
        self.wait_calls = 0
        self.wait_entered = asyncio.Event()
        self.wait_release = asyncio.Event()

    @property
    def snapshot(self) -> WindowsOperatorApplicationSnapshot:
        return WindowsOperatorApplicationSnapshot(
            state=self.state,
            shell_open=self.state != WindowsOperatorApplicationState.CLOSED,
            pump_cycles=0,
            pumped_messages=0,
            generation=self.generation,
            viewport_count=2 if self.generation else 0,
            open_surface_count=2 if self.state == WindowsOperatorApplicationState.RUNNING else 0,
            delivered_frames=0,
            presentations=0,
        )

    async def open(self, _width: int, _height: int) -> WindowsOperatorApplicationSnapshot:
        self.state = WindowsOperatorApplicationState.OPEN
        return self.snapshot

    async def start(self, _layout: ViewportLayout, _streams: object) -> WindowsOperatorApplicationSnapshot:
        self.generation = 1
        self.state = WindowsOperatorApplicationState.RUNNING
        return self.snapshot

    async def replace(self, _layout: ViewportLayout, _streams: object) -> WindowsOperatorApplicationSnapshot:
        self.replace_calls += 1
        self.generation += 1
        self.state = WindowsOperatorApplicationState.RUNNING
        return self.snapshot

    async def relayout(self, layout: ViewportLayout) -> WindowsOperatorApplicationSnapshot:
        self.relayouts.append(layout)
        return self.snapshot

    async def wait(self) -> WindowsOperatorApplicationSnapshot:
        self.wait_calls += 1
        self.wait_entered.set()
        await self.wait_release.wait()
        self.state = WindowsOperatorApplicationState.COMPLETE
        return self.snapshot

    async def stop(self) -> WindowsOperatorApplicationSnapshot:
        self.state = WindowsOperatorApplicationState.STOPPED
        self.wait_release.set()
        return self.snapshot

    async def pump(self, *, max_messages: int = 64) -> WindowsOperatorApplicationSnapshot:
        await asyncio.sleep(0)
        return self.snapshot

    async def close(self) -> WindowsOperatorApplicationSnapshot:
        self.state = WindowsOperatorApplicationState.CLOSED
        self.wait_release.set()
        return self.snapshot


def test_control_relayout_keeps_existing_wait_generation_and_updates_geometry_after_acceptance() -> None:
    app = FakeControlApplication()
    control = BoundedWindowsOperatorControl(
        application_factory=lambda: app,
        poll_interval_seconds=0,
        max_cycles=1000,
    )
    replacement = _layout(31)

    async def scenario() -> object:
        task = asyncio.create_task(
            control.run(
                width=1280,
                height=720,
                layout=_layout(),
                streams=_streams(),
            )
        )
        await app.wait_entered.wait()
        queued = control.request_relayout(replacement)
        assert queued.active_layout == _layout()
        for _ in range(20):
            await asyncio.sleep(0)
            if app.relayouts:
                break
        assert app.relayouts == [replacement]
        assert app.wait_calls == 1
        assert app.replace_calls == 0
        assert app.generation == 1
        assert control.control_snapshot.active_layout == replacement
        assert control.control_snapshot.relayouts == 1
        assert control.control_snapshot.replacements == 0
        control.request_stop()
        return await task

    result = asyncio.run(scenario())
    assert result.relayouts == 1
    assert result.replacements == 0
    serialized = result.model_dump_json().casefold()
    for forbidden in (
        "execution-only-a",
        "execution-only-b",
        "rtsp://",
        "credential",
        "password",
        "source_uri",
        "payload",
        "handle",
        "pointer",
    ):
        assert forbidden not in serialized
