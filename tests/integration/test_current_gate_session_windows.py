"""Direct Win32 qualification for bounded operator application sessions."""

from __future__ import annotations

import asyncio
import sys

import pytest

from k5vision.media.viewport_geometry import (
    ViewportGeometry,
    ViewportLayout,
    ViewportPlacement,
)
from k5vision.media.windows_operator_application import BoundedWindowsOperatorApplication
from k5vision.media.windows_operator_host import (
    WindowsOperatorHostSnapshot,
    WindowsOperatorHostState,
)
from k5vision.media.windows_operator_session import (
    BoundedWindowsOperatorSession,
    WindowsOperatorSessionState,
)

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only qualification")


def _layout() -> ViewportLayout:
    return ViewportLayout(
        placements=(
            ViewportPlacement(
                logical_slot=7,
                geometry=ViewportGeometry(x=17, y=29, width=83, height=47),
            ),
            ViewportPlacement(
                logical_slot=4095,
                geometry=ViewportGeometry(x=173, y=83, width=67, height=53),
            ),
        )
    )


def _host_snapshot(
    state: WindowsOperatorHostState,
    *,
    open_surface_count: int,
) -> WindowsOperatorHostSnapshot:
    return WindowsOperatorHostSnapshot(
        state=state,
        generation=1,
        generation_limit=16,
        completed_generations=1 if state == WindowsOperatorHostState.COMPLETE else 0,
        stopped_generations=1 if state == WindowsOperatorHostState.STOPPED else 0,
        failures=0,
        viewport_count=2,
        open_surface_count=open_surface_count,
        stream_count=2,
        delivered_frames=1,
        presentations=1,
    )


class FiniteSyntheticHost:
    """Synthetic host boundary used only to qualify the real Win32 session shell."""

    def __init__(self) -> None:
        self._snapshot = _host_snapshot(
            WindowsOperatorHostState.READY,
            open_surface_count=0,
        )

    @property
    def snapshot(self) -> WindowsOperatorHostSnapshot:
        return self._snapshot

    async def start(self, layout: ViewportLayout, streams: object) -> WindowsOperatorHostSnapshot:
        assert {item.logical_slot for item in layout.placements} == {7, 4095}
        del streams
        self._snapshot = _host_snapshot(
            WindowsOperatorHostState.RUNNING,
            open_surface_count=2,
        )
        return self._snapshot

    async def replace(self, layout: ViewportLayout, streams: object) -> WindowsOperatorHostSnapshot:
        assert len(layout.placements) == 2
        del streams
        return self._snapshot

    async def wait(self) -> WindowsOperatorHostSnapshot:
        await asyncio.sleep(0.03)
        self._snapshot = _host_snapshot(
            WindowsOperatorHostState.COMPLETE,
            open_surface_count=0,
        )
        return self._snapshot

    async def stop(self) -> WindowsOperatorHostSnapshot:
        self._snapshot = _host_snapshot(
            WindowsOperatorHostState.STOPPED,
            open_surface_count=0,
        )
        return self._snapshot

    async def close(self) -> WindowsOperatorHostSnapshot:
        self._snapshot = _host_snapshot(
            WindowsOperatorHostState.CLOSED,
            open_surface_count=0,
        )
        return self._snapshot


def test_real_win32_session_pumps_to_completion_and_closes_shell() -> None:
    parent_handles: list[int] = []

    def application_factory() -> BoundedWindowsOperatorApplication:
        def host_factory(parent_handle: int) -> FiniteSyntheticHost:
            parent_handles.append(parent_handle)
            return FiniteSyntheticHost()

        return BoundedWindowsOperatorApplication(host_factory=host_factory)

    async def scenario() -> None:
        session = BoundedWindowsOperatorSession(
            application_factory=application_factory,
            max_cycles=100,
            poll_interval_seconds=0.005,
        )

        finished = await session.run(
            width=640,
            height=480,
            layout=_layout(),
            streams=(),
        )

        assert finished.state == WindowsOperatorSessionState.COMPLETE
        assert finished.shell_open is False
        assert finished.cycles >= 1
        assert finished.pumped_messages >= 0
        assert finished.viewport_count == 2
        assert finished.open_surface_count == 0
        assert finished.presentations == 1
        assert len(parent_handles) == 1
        assert parent_handles[0] > 0

        closed = await session.close()
        assert closed.state == WindowsOperatorSessionState.CLOSED
        assert closed.shell_open is False

    asyncio.run(scenario())
