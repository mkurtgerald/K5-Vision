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
from k5vision.media.windows_operator_session import (
    BoundedWindowsOperatorSession,
    WindowsOperatorSessionError,
    WindowsOperatorSessionErrorCode,
    WindowsOperatorSessionState,
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


def _snapshot(
    state: WindowsOperatorApplicationState,
    *,
    shell_open: bool,
    cycles: int = 0,
    pumped_messages: int = 0,
) -> WindowsOperatorApplicationSnapshot:
    return WindowsOperatorApplicationSnapshot(
        state=state,
        shell_open=shell_open,
        pump_cycles=cycles,
        pumped_messages=pumped_messages,
        generation=1,
        viewport_count=2,
        open_surface_count=2 if state == WindowsOperatorApplicationState.RUNNING else 0,
        delivered_frames=1,
        presentations=1,
    )


class FakeApplication:
    def __init__(
        self,
        events: list[str],
        *,
        close_on_pump: bool = False,
        never_complete: bool = False,
    ) -> None:
        self.events = events
        self.close_on_pump = close_on_pump
        self.never_complete = never_complete
        self._snapshot = _snapshot(
            WindowsOperatorApplicationState.READY,
            shell_open=False,
        )
        self.pump_cycles = 0

    @property
    def snapshot(self) -> WindowsOperatorApplicationSnapshot:
        return self._snapshot

    async def open(self, width: int, height: int) -> WindowsOperatorApplicationSnapshot:
        self.events.append(f"open:{width}x{height}")
        self._snapshot = _snapshot(
            WindowsOperatorApplicationState.OPEN,
            shell_open=True,
        )
        return self._snapshot

    async def start(self, layout: ViewportLayout, streams: object) -> WindowsOperatorApplicationSnapshot:
        assert len(layout.placements) == 2
        del streams
        self.events.append("start")
        self._snapshot = _snapshot(
            WindowsOperatorApplicationState.RUNNING,
            shell_open=True,
        )
        return self._snapshot

    async def wait(self) -> WindowsOperatorApplicationSnapshot:
        self.events.append("wait")
        if self.never_complete:
            await asyncio.Event().wait()
        await asyncio.sleep(0)
        self._snapshot = _snapshot(
            WindowsOperatorApplicationState.COMPLETE,
            shell_open=True,
            cycles=self.pump_cycles,
            pumped_messages=self.pump_cycles,
        )
        return self._snapshot

    async def stop(self) -> WindowsOperatorApplicationSnapshot:
        self.events.append("stop")
        self._snapshot = _snapshot(
            WindowsOperatorApplicationState.STOPPED,
            shell_open=True,
            cycles=self.pump_cycles,
            pumped_messages=self.pump_cycles,
        )
        return self._snapshot

    async def pump(self, *, max_messages: int = 64) -> WindowsOperatorApplicationSnapshot:
        assert 1 <= max_messages <= 256
        self.pump_cycles += 1
        self.events.append("pump")
        state = (
            WindowsOperatorApplicationState.CLOSED
            if self.close_on_pump
            else WindowsOperatorApplicationState.RUNNING
        )
        self._snapshot = _snapshot(
            state,
            shell_open=not self.close_on_pump,
            cycles=self.pump_cycles,
            pumped_messages=self.pump_cycles,
        )
        return self._snapshot

    async def close(self) -> WindowsOperatorApplicationSnapshot:
        self.events.append("close")
        self._snapshot = _snapshot(
            WindowsOperatorApplicationState.CLOSED,
            shell_open=False,
            cycles=self.pump_cycles,
            pumped_messages=self.pump_cycles,
        )
        return self._snapshot


def test_session_runs_complete_bounded_lifecycle_and_closes_shell() -> None:
    async def scenario() -> None:
        events: list[str] = []
        app = FakeApplication(events)
        session = BoundedWindowsOperatorSession(
            application_factory=lambda: app,
            poll_interval_seconds=0,
        )

        finished = await session.run(
            width=1280,
            height=900,
            layout=_layout(),
            streams=(),
        )

        assert finished.state == WindowsOperatorSessionState.COMPLETE
        assert finished.shell_open is False
        assert finished.cycles >= 1
        assert finished.viewport_count == 2
        assert finished.presentations == 1
        assert events[0:2] == ["open:1280x900", "start"]
        assert "pump" in events
        assert "wait" in events
        assert events[-1] == "close"

    asyncio.run(scenario())


def test_user_close_cancels_wait_and_returns_terminal_clean_state() -> None:
    async def scenario() -> None:
        events: list[str] = []
        app = FakeApplication(events, close_on_pump=True, never_complete=True)
        session = BoundedWindowsOperatorSession(
            application_factory=lambda: app,
            poll_interval_seconds=0,
        )

        finished = await session.run(
            width=900,
            height=700,
            layout=_layout(),
            streams=(),
        )

        assert finished.state == WindowsOperatorSessionState.USER_CLOSED
        assert finished.shell_open is False
        assert finished.cycles == 1
        assert events[-1] == "close"

    asyncio.run(scenario())


def test_session_cycle_limit_stops_then_fails_closed() -> None:
    async def scenario() -> None:
        events: list[str] = []
        app = FakeApplication(events, never_complete=True)
        session = BoundedWindowsOperatorSession(
            application_factory=lambda: app,
            max_cycles=2,
            poll_interval_seconds=0,
        )

        with pytest.raises(WindowsOperatorSessionError) as exc_info:
            await session.run(
                width=900,
                height=700,
                layout=_layout(),
                streams=(),
            )

        assert exc_info.value.code == WindowsOperatorSessionErrorCode.SESSION_LIMIT
        assert session.snapshot.state == WindowsOperatorSessionState.FAILED
        assert session.snapshot.shell_open is False
        assert session.snapshot.cycles == 2
        assert "stop" in events
        assert events[-1] == "close"

    asyncio.run(scenario())


def test_session_cancellation_closes_owned_application() -> None:
    async def scenario() -> None:
        events: list[str] = []
        app = FakeApplication(events, never_complete=True)
        session = BoundedWindowsOperatorSession(
            application_factory=lambda: app,
            poll_interval_seconds=0.05,
        )

        task = asyncio.create_task(
            session.run(
                width=900,
                height=700,
                layout=_layout(),
                streams=(),
            )
        )
        while "pump" not in events:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert session.snapshot.state == WindowsOperatorSessionState.CANCELLED
        assert session.snapshot.shell_open is False
        assert events[-1] == "close"

    asyncio.run(scenario())


def test_session_snapshot_retains_no_sensitive_or_native_identity() -> None:
    snapshot = BoundedWindowsOperatorSession(
        application_factory=lambda: FakeApplication([]),
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


@pytest.mark.parametrize(
    "kwargs",
    (
        {"max_cycles": 0},
        {"max_cycles": 1_000_001},
        {"max_messages_per_cycle": 0},
        {"max_messages_per_cycle": 257},
        {"poll_interval_seconds": -0.1},
        {"poll_interval_seconds": 1.1},
        {"cleanup_timeout_seconds": 0.01},
        {"cleanup_timeout_seconds": 31.0},
    ),
)
def test_session_bounds_are_explicit(kwargs: dict[str, object]) -> None:
    with pytest.raises(WindowsOperatorSessionError) as exc_info:
        BoundedWindowsOperatorSession(
            application_factory=lambda: FakeApplication([]),
            **kwargs,  # type: ignore[arg-type]
        )
    assert exc_info.value.code == WindowsOperatorSessionErrorCode.INVALID_CONFIGURATION
