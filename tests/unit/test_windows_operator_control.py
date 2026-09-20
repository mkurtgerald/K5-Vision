from __future__ import annotations

import asyncio

import pytest

from k5vision.media.viewport_geometry import (
    ViewportGeometry,
    ViewportLayout,
    ViewportPlacement,
)
from k5vision.media.windows_operator_application import (
    WindowsOperatorApplicationSnapshot,
    WindowsOperatorApplicationState,
)
from k5vision.media.windows_operator_control import (
    BoundedWindowsOperatorControl,
    WindowsOperatorControlError,
    WindowsOperatorControlErrorCode,
)
from k5vision.media.windows_operator_session import WindowsOperatorSessionState


def _layout(offset: int = 0) -> ViewportLayout:
    return ViewportLayout(
        placements=(
            ViewportPlacement(
                logical_slot=7,
                geometry=ViewportGeometry(
                    x=17 + offset,
                    y=29,
                    width=613,
                    height=347,
                    z_index=2,
                ),
            ),
            ViewportPlacement(
                logical_slot=4095,
                geometry=ViewportGeometry(
                    x=701,
                    y=41 + offset,
                    width=211,
                    height=719,
                    z_index=1,
                ),
            ),
        )
    )


def _snapshot(
    state: WindowsOperatorApplicationState,
    *,
    shell_open: bool,
    generation: int,
    pumped_messages: int = 0,
) -> WindowsOperatorApplicationSnapshot:
    return WindowsOperatorApplicationSnapshot(
        state=state,
        shell_open=shell_open,
        pump_cycles=pumped_messages,
        pumped_messages=pumped_messages,
        generation=generation,
        viewport_count=2,
        open_surface_count=2 if state == WindowsOperatorApplicationState.RUNNING else 0,
        delivered_frames=generation,
        presentations=generation,
    )


class ControllableFakeApplication:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.generation = 0
        self.pumps = 0
        self._wait_release = asyncio.Event()
        self._snapshot = _snapshot(
            WindowsOperatorApplicationState.READY,
            shell_open=False,
            generation=0,
        )

    @property
    def snapshot(self) -> WindowsOperatorApplicationSnapshot:
        return self._snapshot

    async def open(self, width: int, height: int) -> WindowsOperatorApplicationSnapshot:
        self.events.append(f"open:{width}x{height}")
        self._snapshot = _snapshot(
            WindowsOperatorApplicationState.OPEN,
            shell_open=True,
            generation=self.generation,
        )
        return self._snapshot

    async def start(
        self, layout: ViewportLayout, streams: object
    ) -> WindowsOperatorApplicationSnapshot:
        del streams
        assert {item.logical_slot for item in layout.placements} == {7, 4095}
        self.generation = 1
        self.events.append("start")
        self._snapshot = _snapshot(
            WindowsOperatorApplicationState.RUNNING,
            shell_open=True,
            generation=self.generation,
        )
        return self._snapshot

    async def replace(
        self,
        layout: ViewportLayout,
        streams: object,
    ) -> WindowsOperatorApplicationSnapshot:
        del streams
        assert layout.placements[0].geometry.x >= 17
        self.generation += 1
        self.events.append("replace")
        self._snapshot = _snapshot(
            WindowsOperatorApplicationState.RUNNING,
            shell_open=True,
            generation=self.generation,
            pumped_messages=self.pumps,
        )
        return self._snapshot

    async def wait(self) -> WindowsOperatorApplicationSnapshot:
        self.events.append(f"wait:{self.generation}")
        await self._wait_release.wait()
        return self._snapshot

    async def stop(self) -> WindowsOperatorApplicationSnapshot:
        self.events.append("stop")
        self._snapshot = _snapshot(
            WindowsOperatorApplicationState.STOPPED,
            shell_open=True,
            generation=self.generation,
            pumped_messages=self.pumps,
        )
        return self._snapshot

    async def pump(self, *, max_messages: int = 64) -> WindowsOperatorApplicationSnapshot:
        assert 1 <= max_messages <= 256
        self.pumps += 1
        self.events.append("pump")
        self._snapshot = _snapshot(
            WindowsOperatorApplicationState.RUNNING,
            shell_open=True,
            generation=self.generation,
            pumped_messages=self.pumps,
        )
        await asyncio.sleep(0)
        return self._snapshot

    async def close(self) -> WindowsOperatorApplicationSnapshot:
        self.events.append("close")
        self._snapshot = _snapshot(
            WindowsOperatorApplicationState.CLOSED,
            shell_open=False,
            generation=self.generation,
            pumped_messages=self.pumps,
        )
        return self._snapshot


def test_live_control_replaces_arbitrary_layout_and_stops_same_session() -> None:
    async def scenario() -> None:
        events: list[str] = []
        app = ControllableFakeApplication(events)
        control = BoundedWindowsOperatorControl(
            application_factory=lambda: app,
            poll_interval_seconds=0.001,
            max_cycles=500,
        )

        task = asyncio.create_task(
            control.run(
                width=1280,
                height=900,
                layout=_layout(),
                streams=(),
            )
        )
        while "pump" not in events:
            await asyncio.sleep(0)

        queued = control.request_replace(_layout(offset=37), ())
        assert queued.queued_controls == 1
        while "replace" not in events:
            await asyncio.sleep(0)

        assert control.control_snapshot.replacements == 1
        assert control.control_snapshot.session.generation == 2
        control.request_stop()
        finished = await task

        assert finished.session.state == WindowsOperatorSessionState.COMPLETE
        assert finished.session.shell_open is False
        assert finished.replacements == 1
        assert finished.stop_requests == 1
        assert finished.processed_controls == 2
        assert finished.queued_controls == 0
        assert events.count("open:1280x900") == 1
        assert events.count("replace") == 1
        assert events[-2:] == ["stop", "close"]

    asyncio.run(scenario())


def test_control_queue_is_bounded_while_pump_is_blocked() -> None:
    class BlockingPumpApplication(ControllableFakeApplication):
        def __init__(self, events: list[str], release: asyncio.Event) -> None:
            super().__init__(events)
            self.release = release

        async def pump(self, *, max_messages: int = 64) -> WindowsOperatorApplicationSnapshot:
            self.events.append("pump-blocked")
            await self.release.wait()
            return await super().pump(max_messages=max_messages)

    async def scenario() -> None:
        events: list[str] = []
        release = asyncio.Event()
        app = BlockingPumpApplication(events, release)
        control = BoundedWindowsOperatorControl(
            application_factory=lambda: app,
            max_pending_controls=1,
            poll_interval_seconds=0,
            max_cycles=50,
        )
        task = asyncio.create_task(control.run(width=900, height=700, layout=_layout(), streams=()))
        while "pump-blocked" not in events:
            await asyncio.sleep(0)

        control.request_replace(_layout(offset=11), ())
        with pytest.raises(WindowsOperatorControlError) as exc_info:
            control.request_stop()
        assert exc_info.value.code == WindowsOperatorControlErrorCode.CONTROL_LIMIT

        release.set()
        while control.control_snapshot.replacements != 1:
            await asyncio.sleep(0)
        control.request_stop()
        await task

    asyncio.run(scenario())


def test_control_snapshot_retains_no_sensitive_or_native_identity() -> None:
    snapshot = BoundedWindowsOperatorControl(
        application_factory=lambda: ControllableFakeApplication([]),
    ).control_snapshot
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


@pytest.mark.parametrize(
    "kwargs",
    (
        {"max_pending_controls": 0},
        {"max_pending_controls": 65},
        {"max_controls_per_cycle": 0},
        {"max_controls_per_cycle": 17},
    ),
)
def test_control_bounds_are_explicit(kwargs: dict[str, int]) -> None:
    with pytest.raises(WindowsOperatorControlError) as exc_info:
        BoundedWindowsOperatorControl(
            application_factory=lambda: ControllableFakeApplication([]),
            **kwargs,
        )
    assert exc_info.value.code == WindowsOperatorControlErrorCode.INVALID_CONFIGURATION
