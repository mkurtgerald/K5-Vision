"""Direct Win32 qualification for bounded live operator control."""

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
from k5vision.media.windows_operator_control import BoundedWindowsOperatorControl
from k5vision.media.windows_operator_host import (
    WindowsOperatorHostSnapshot,
    WindowsOperatorHostState,
)
from k5vision.media.windows_operator_session import WindowsOperatorSessionState

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only qualification")


def _layout(offset: int = 0) -> ViewportLayout:
    return ViewportLayout(
        placements=(
            ViewportPlacement(
                logical_slot=7,
                geometry=ViewportGeometry(
                    x=17 + offset,
                    y=29,
                    width=83,
                    height=47,
                ),
            ),
            ViewportPlacement(
                logical_slot=4095,
                geometry=ViewportGeometry(
                    x=173,
                    y=83 + offset,
                    width=67,
                    height=53,
                ),
            ),
        )
    )


def _host_snapshot(
    state: WindowsOperatorHostState,
    *,
    generation: int,
    open_surface_count: int,
) -> WindowsOperatorHostSnapshot:
    return WindowsOperatorHostSnapshot(
        state=state,
        generation=generation,
        generation_limit=16,
        completed_generations=0,
        stopped_generations=1 if state == WindowsOperatorHostState.STOPPED else 0,
        failures=0,
        viewport_count=2,
        open_surface_count=open_surface_count,
        stream_count=2,
        delivered_frames=generation,
        presentations=generation,
    )


class ControllableSyntheticHost:
    """Synthetic media boundary used only to exercise the real Win32 shell."""

    def __init__(self) -> None:
        self.generation = 0
        self._wait_release = asyncio.Event()
        self._snapshot = _host_snapshot(
            WindowsOperatorHostState.READY,
            generation=0,
            open_surface_count=0,
        )

    @property
    def snapshot(self) -> WindowsOperatorHostSnapshot:
        return self._snapshot

    async def start(self, layout: ViewportLayout, streams: object) -> WindowsOperatorHostSnapshot:
        del streams
        assert {item.logical_slot for item in layout.placements} == {7, 4095}
        self.generation = 1
        self._snapshot = _host_snapshot(
            WindowsOperatorHostState.RUNNING,
            generation=self.generation,
            open_surface_count=2,
        )
        return self._snapshot

    async def replace(self, layout: ViewportLayout, streams: object) -> WindowsOperatorHostSnapshot:
        del streams
        assert len(layout.placements) == 2
        self.generation += 1
        self._snapshot = _host_snapshot(
            WindowsOperatorHostState.RUNNING,
            generation=self.generation,
            open_surface_count=2,
        )
        return self._snapshot

    async def wait(self) -> WindowsOperatorHostSnapshot:
        await self._wait_release.wait()
        return self._snapshot

    async def stop(self) -> WindowsOperatorHostSnapshot:
        self._snapshot = _host_snapshot(
            WindowsOperatorHostState.STOPPED,
            generation=self.generation,
            open_surface_count=0,
        )
        self._wait_release.set()
        return self._snapshot

    async def close(self) -> WindowsOperatorHostSnapshot:
        self._wait_release.set()
        self._snapshot = _host_snapshot(
            WindowsOperatorHostState.CLOSED,
            generation=self.generation,
            open_surface_count=0,
        )
        return self._snapshot


def test_real_win32_shell_accepts_same_shell_replace_then_explicit_stop() -> None:
    parent_handles: list[int] = []

    def application_factory() -> BoundedWindowsOperatorApplication:
        def host_factory(parent_handle: int) -> ControllableSyntheticHost:
            parent_handles.append(parent_handle)
            return ControllableSyntheticHost()

        return BoundedWindowsOperatorApplication(host_factory=host_factory)

    async def scenario() -> None:
        control = BoundedWindowsOperatorControl(
            application_factory=application_factory,
            max_cycles=500,
            poll_interval_seconds=0.002,
        )
        task = asyncio.create_task(
            control.run(
                width=640,
                height=480,
                layout=_layout(),
                streams=(),
            )
        )

        while control.control_snapshot.session.generation != 1:
            await asyncio.sleep(0.001)
        assert len(parent_handles) == 1
        original_parent = parent_handles[0]
        assert original_parent > 0

        control.request_replace(_layout(offset=11), ())
        while control.control_snapshot.replacements != 1:
            await asyncio.sleep(0.001)
        assert control.control_snapshot.session.generation == 2
        assert parent_handles == [original_parent]

        control.request_stop()
        finished = await task
        assert finished.session.state == WindowsOperatorSessionState.COMPLETE
        assert finished.session.shell_open is False
        assert finished.replacements == 1
        assert finished.stop_requests == 1
        assert finished.processed_controls == 2

    asyncio.run(scenario())
