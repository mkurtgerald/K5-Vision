"""Direct Win32 qualification for the visible operator application shell."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Sequence

import pytest

from k5vision.media.mixed_presentation import MixedPresentationStream
from k5vision.media.presentation_frame import PixelFormat, PresentationVideoFrame
from k5vision.media.presentation_runtime import (
    PresentationRuntimeSnapshot,
    PresentationRuntimeState,
)
from k5vision.media.viewport_dispatch import ViewportBinding
from k5vision.media.viewport_geometry import ViewportGeometry, ViewportLayout, ViewportPlacement
from k5vision.media.windows_operator_application import (
    BoundedWindowsOperatorApplication,
    WindowsOperatorApplicationState,
)
from k5vision.media.windows_operator_host import BoundedWindowsOperatorHost
from k5vision.media.windows_operator_runtime import BoundedWindowsOperatorRuntime
from k5vision.media.windows_presentation_target import BoundedWindowsPresentationTarget
from k5vision.media.windows_viewport_layout import BoundedWindowsViewportLayout
from k5vision.media.windows_viewport_runtime import BoundedWindowsViewportRuntime

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only qualification")


def _presentation_snapshot(
    state: PresentationRuntimeState,
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
        max_source_span_ms=1 if delivered_frames else 0,
    )


class SyntheticPresentationRuntime:
    """Synthetic media boundary driving real child Win32 viewport targets."""

    def __init__(self, bindings: Sequence[ViewportBinding]) -> None:
        self._bindings = tuple(bindings)
        self._snapshot = _presentation_snapshot(PresentationRuntimeState.CREATED)

    @property
    def snapshot(self) -> PresentationRuntimeSnapshot:
        return self._snapshot

    async def start(
        self,
        _streams: Sequence[MixedPresentationStream],
    ) -> PresentationRuntimeSnapshot:
        frame = PresentationVideoFrame(
            payload=memoryview(bytearray([1, 2, 3, 0] * 4)),
            width=2,
            height=2,
            stride_bytes=8,
            pixel_format=PixelFormat.BGRX,
            source_elapsed_ms=1,
        )
        selected = {binding.slot: binding for binding in self._bindings}
        await selected[4095].consumer(frame)
        self._snapshot = _presentation_snapshot(
            PresentationRuntimeState.RUNNING,
            delivered_frames=1,
        )
        return self._snapshot

    async def wait(self) -> PresentationRuntimeSnapshot:
        self._snapshot = _presentation_snapshot(
            PresentationRuntimeState.COMPLETE,
            delivered_frames=1,
        )
        return self._snapshot

    async def stop(self) -> PresentationRuntimeSnapshot:
        self._snapshot = _presentation_snapshot(
            PresentationRuntimeState.STOPPED,
            delivered_frames=1,
        )
        return self._snapshot

    async def close(self) -> PresentationRuntimeSnapshot:
        self._snapshot = _presentation_snapshot(
            PresentationRuntimeState.CLOSED,
            delivered_frames=1,
        )
        return self._snapshot


def _layout(offset: int) -> ViewportLayout:
    return ViewportLayout(
        placements=(
            ViewportPlacement(
                logical_slot=7,
                geometry=ViewportGeometry(
                    x=17 + offset,
                    y=29 + offset,
                    width=83,
                    height=47,
                ),
            ),
            ViewportPlacement(
                logical_slot=4095,
                geometry=ViewportGeometry(
                    x=173 + offset,
                    y=83 + offset,
                    width=67,
                    height=53,
                ),
            ),
        )
    )


def test_real_win32_application_hosts_and_replaces_sparse_child_layouts() -> None:
    parent_handles: list[int] = []

    def host_factory(parent_handle: int) -> BoundedWindowsOperatorHost:
        parent_handles.append(parent_handle)

        def target_factory() -> BoundedWindowsPresentationTarget:
            return BoundedWindowsPresentationTarget(parent_handle=parent_handle)

        def layout_factory(layout: ViewportLayout) -> BoundedWindowsViewportLayout:
            return BoundedWindowsViewportLayout(layout, target_factory=target_factory)

        def windows_runtime_factory(layout: ViewportLayout) -> BoundedWindowsViewportRuntime:
            return BoundedWindowsViewportRuntime(layout, layout_factory=layout_factory)

        def runtime_factory(layout: ViewportLayout) -> BoundedWindowsOperatorRuntime:
            return BoundedWindowsOperatorRuntime(
                layout,
                windows_runtime_factory=windows_runtime_factory,
                presentation_runtime_factory=SyntheticPresentationRuntime,
            )

        return BoundedWindowsOperatorHost(runtime_factory=runtime_factory)

    async def scenario() -> None:
        app = BoundedWindowsOperatorApplication(host_factory=host_factory)

        opened = await app.open(640, 480)
        assert opened.state == WindowsOperatorApplicationState.OPEN
        assert opened.shell_open is True

        first = await app.start(_layout(0), ())
        assert first.state == WindowsOperatorApplicationState.RUNNING
        assert first.generation == 1
        assert first.viewport_count == 2
        assert first.open_surface_count == 2
        assert first.presentations == 1

        pumped = await app.pump(max_messages=32)
        assert pumped.shell_open is True
        assert pumped.pump_cycles == 1

        second = await app.replace(_layout(40), ())
        assert second.state == WindowsOperatorApplicationState.RUNNING
        assert second.generation == 2
        assert second.open_surface_count == 2
        assert second.presentations == 2
        assert len(parent_handles) == 1

        stopped = await app.stop()
        assert stopped.state == WindowsOperatorApplicationState.STOPPED
        assert stopped.shell_open is True
        assert stopped.open_surface_count == 0

        closed = await app.close()
        assert closed.state == WindowsOperatorApplicationState.CLOSED
        assert closed.shell_open is False
        assert closed.open_surface_count == 0
        assert closed.presentations == 2

    asyncio.run(scenario())
