from __future__ import annotations

import asyncio

import pytest

from k5vision.media.viewport_geometry import (
    ViewportGeometry,
    ViewportLayout,
    ViewportPlacement,
)
from k5vision.media.windows_operator_application import (
    BoundedWindowsOperatorApplication,
    WindowsOperatorApplicationError,
    WindowsOperatorApplicationErrorCode,
    WindowsOperatorApplicationState,
)
from k5vision.media.windows_operator_host import (
    WindowsOperatorHostSnapshot,
    WindowsOperatorHostState,
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
                    z_index=2,
                ),
            ),
            ViewportPlacement(
                logical_slot=4095,
                geometry=ViewportGeometry(
                    x=701 + offset,
                    y=41 + offset,
                    width=211,
                    height=719,
                    z_index=1,
                ),
            ),
        )
    )


def _host_snapshot(
    state: WindowsOperatorHostState,
    *,
    generation: int = 1,
    open_surface_count: int = 2,
    delivered_frames: int = 1,
    presentations: int = 1,
) -> WindowsOperatorHostSnapshot:
    return WindowsOperatorHostSnapshot(
        state=state,
        generation=generation,
        generation_limit=16,
        completed_generations=1 if state == WindowsOperatorHostState.COMPLETE else 0,
        stopped_generations=1 if state == WindowsOperatorHostState.STOPPED else 0,
        failures=0,
        viewport_count=2,
        open_surface_count=open_surface_count,
        stream_count=2,
        delivered_frames=delivered_frames,
        presentations=presentations,
    )


class FakeNativeShell:
    def __init__(self, events: list[str], *, close_requested: bool = False) -> None:
        self.events = events
        self.close_requested = close_requested
        self.created = 0

    def create_shell(self, width: int, height: int) -> int:
        self.events.append(f"shell-open:{width}x{height}")
        self.created += 1
        return 71

    def pump_messages(self, shell: int, max_messages: int) -> tuple[int, bool]:
        assert shell == 71
        assert 1 <= max_messages <= 256
        self.events.append("pump")
        return 3, self.close_requested

    def destroy_shell(self, shell: int) -> None:
        assert shell == 71
        self.events.append("shell-close")


class FakeHost:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self._snapshot = _host_snapshot(WindowsOperatorHostState.READY, generation=0)

    @property
    def snapshot(self) -> WindowsOperatorHostSnapshot:
        return self._snapshot

    async def start(self, layout: ViewportLayout, streams: object) -> WindowsOperatorHostSnapshot:
        assert len(layout.placements) == 2
        del streams
        self.events.append("host-start")
        self._snapshot = _host_snapshot(WindowsOperatorHostState.RUNNING)
        return self._snapshot

    async def replace(
        self,
        layout: ViewportLayout,
        streams: object,
    ) -> WindowsOperatorHostSnapshot:
        assert len(layout.placements) == 2
        del streams
        self.events.append("host-replace")
        self._snapshot = _host_snapshot(
            WindowsOperatorHostState.RUNNING,
            generation=2,
            delivered_frames=2,
            presentations=2,
        )
        return self._snapshot

    async def wait(self) -> WindowsOperatorHostSnapshot:
        self.events.append("host-wait")
        self._snapshot = _host_snapshot(
            WindowsOperatorHostState.COMPLETE,
            open_surface_count=0,
        )
        return self._snapshot

    async def stop(self) -> WindowsOperatorHostSnapshot:
        self.events.append("host-stop")
        self._snapshot = _host_snapshot(
            WindowsOperatorHostState.STOPPED,
            open_surface_count=0,
        )
        return self._snapshot

    async def close(self) -> WindowsOperatorHostSnapshot:
        self.events.append("host-close")
        self._snapshot = _host_snapshot(
            WindowsOperatorHostState.CLOSED,
            open_surface_count=0,
        )
        return self._snapshot


def test_application_preserves_shell_across_arbitrary_layout_replacement() -> None:
    async def scenario() -> None:
        events: list[str] = []
        native = FakeNativeShell(events)
        host = FakeHost(events)
        parent_handles: list[int] = []

        def host_factory(parent_handle: int) -> FakeHost:
            parent_handles.append(parent_handle)
            return host

        app = BoundedWindowsOperatorApplication(
            native_api=native,
            host_factory=host_factory,
        )
        opened = await app.open(1280, 900)
        assert opened.state == WindowsOperatorApplicationState.OPEN
        assert opened.shell_open is True

        started = await app.start(_layout(), ())
        assert started.state == WindowsOperatorApplicationState.RUNNING
        assert started.viewport_count == 2
        assert started.generation == 1
        assert parent_handles == [71]

        replaced = await app.replace(_layout(37), ())
        assert replaced.state == WindowsOperatorApplicationState.RUNNING
        assert replaced.generation == 2
        assert replaced.presentations == 2
        assert native.created == 1

        pumped = await app.pump(max_messages=8)
        assert pumped.pump_cycles == 1
        assert pumped.pumped_messages == 3

        closed = await app.close()
        assert closed.state == WindowsOperatorApplicationState.CLOSED
        assert closed.shell_open is False
        assert events.index("host-close") < events.index("shell-close")

    asyncio.run(scenario())


def test_close_request_releases_host_before_shell() -> None:
    async def scenario() -> None:
        events: list[str] = []
        native = FakeNativeShell(events, close_requested=True)
        host = FakeHost(events)
        app = BoundedWindowsOperatorApplication(
            native_api=native,
            host_factory=lambda _parent: host,
        )

        await app.open(900, 700)
        await app.start(_layout(), ())
        closed = await app.pump(max_messages=4)

        assert closed.state == WindowsOperatorApplicationState.CLOSED
        assert closed.shell_open is False
        assert events.index("host-close") < events.index("shell-close")

    asyncio.run(scenario())


def test_pump_limit_fails_closed_and_releases_children_first() -> None:
    async def scenario() -> None:
        events: list[str] = []
        native = FakeNativeShell(events)
        host = FakeHost(events)
        app = BoundedWindowsOperatorApplication(
            native_api=native,
            host_factory=lambda _parent: host,
            max_pump_cycles=1,
        )

        await app.open(900, 700)
        await app.start(_layout(), ())
        await app.pump()

        with pytest.raises(WindowsOperatorApplicationError) as exc_info:
            await app.pump()

        assert exc_info.value.code == WindowsOperatorApplicationErrorCode.PUMP_LIMIT
        assert app.snapshot.state == WindowsOperatorApplicationState.FAILED
        assert app.snapshot.shell_open is False
        assert events.index("host-close") < events.index("shell-close")

    asyncio.run(scenario())


def test_application_snapshot_retains_no_sensitive_or_native_identity() -> None:
    snapshot = BoundedWindowsOperatorApplication(
        native_api=FakeNativeShell([]),
        host_factory=lambda _parent: FakeHost([]),
    ).snapshot
    payload = snapshot.model_dump_json().casefold()
    for forbidden in (
        "rtsp://",
        "credential",
        "password",
        "source_id",
        "recording_id",
        "path",
        "payload",
        "handle",
        "pointer",
    ):
        assert forbidden not in payload
