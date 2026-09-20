"""Direct Win32 qualification for all-live operator application generations."""

from __future__ import annotations

import asyncio
import sys

import pytest

from k5vision.media.live_presentation import LivePresentationSnapshot, LivePresentationState
from k5vision.media.mixed_presentation import MixedLiveStream
from k5vision.media.presentation_frame import PixelFormat, PresentationVideoFrame
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


class SyntheticLiveDelivery:
    """Execution-only delivery that emits one transient frame and retains no source."""

    def __init__(self, elapsed_ms: int) -> None:
        self._elapsed_ms = elapsed_ms

    async def run(self, _source_uri: str, consumer: object) -> LivePresentationSnapshot:
        frame = PresentationVideoFrame(
            payload=memoryview(bytearray([1, 2, 3, 0] * 4)),
            width=2,
            height=2,
            stride_bytes=8,
            pixel_format=PixelFormat.BGRX,
            source_elapsed_ms=self._elapsed_ms,
        )
        await consumer(frame)  # type: ignore[operator]
        return LivePresentationSnapshot(
            state=LivePresentationState.COMPLETE,
            decoder_initialized=True,
            accepted_packets=1,
            rtp_valid_packets=1,
            rtp_invalid_packets=0,
            rtp_delivered_bytes=16,
            delivered_frames=1,
            delivered_frame_bytes=16,
            source_span_ms=self._elapsed_ms,
        )


def _layout(first_slot: int, second_slot: int, offset: int) -> ViewportLayout:
    return ViewportLayout(
        placements=(
            ViewportPlacement(
                logical_slot=first_slot,
                geometry=ViewportGeometry(
                    x=17 + offset,
                    y=29 + offset,
                    width=83,
                    height=47,
                    z_index=2,
                ),
            ),
            ViewportPlacement(
                logical_slot=second_slot,
                geometry=ViewportGeometry(
                    x=173 + offset,
                    y=83 + offset,
                    width=67,
                    height=53,
                    z_index=1,
                ),
            ),
        )
    )


def _streams(first_slot: int, second_slot: int) -> tuple[MixedLiveStream, MixedLiveStream]:
    return (
        MixedLiveStream(
            first_slot,
            "rtsp://synthetic.invalid/live-a",
            SyntheticLiveDelivery(10),
        ),
        MixedLiveStream(
            second_slot,
            "rtsp://synthetic.invalid/live-b",
            SyntheticLiveDelivery(20),
        ),
    )


def test_real_win32_application_runs_and_replaces_sparse_all_live_generations() -> None:
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
            )

        return BoundedWindowsOperatorHost(runtime_factory=runtime_factory)

    async def scenario() -> None:
        app = BoundedWindowsOperatorApplication(host_factory=host_factory)

        opened = await app.open(640, 480)
        assert opened.state == WindowsOperatorApplicationState.OPEN
        assert opened.shell_open is True

        first_started = await app.start(_layout(7, 4095, 0), _streams(7, 4095))
        assert first_started.state == WindowsOperatorApplicationState.RUNNING
        assert first_started.generation == 1
        assert first_started.viewport_count == 2
        assert first_started.open_surface_count == 2

        first_complete = await app.wait()
        assert first_complete.state == WindowsOperatorApplicationState.COMPLETE
        assert first_complete.shell_open is True
        assert first_complete.delivered_frames == 2
        assert first_complete.presentations == 2

        second_started = await app.replace(_layout(23, 4000, 31), _streams(23, 4000))
        assert second_started.state == WindowsOperatorApplicationState.RUNNING
        assert second_started.generation == 2
        assert second_started.viewport_count == 2
        assert second_started.open_surface_count == 2
        assert len(parent_handles) == 1

        second_complete = await app.wait()
        assert second_complete.state == WindowsOperatorApplicationState.COMPLETE
        assert second_complete.shell_open is True
        assert second_complete.delivered_frames == 4
        assert second_complete.presentations == 4

        closed = await app.close()
        assert closed.state == WindowsOperatorApplicationState.CLOSED
        assert closed.shell_open is False
        assert closed.open_surface_count == 0
        assert closed.delivered_frames == 4
        assert closed.presentations == 4

        retained = closed.model_dump_json().casefold()
        for forbidden in (
            "rtsp://",
            "synthetic.invalid",
            "4095",
            "4000",
            "logical_slot",
            "source_id",
            "recording_id",
            "path",
            "payload",
            "handle",
            "pointer",
        ):
            assert forbidden not in retained

    asyncio.run(scenario())
