from __future__ import annotations

import asyncio
from collections.abc import Sequence

from k5vision.media.live_presentation import LivePresentationSnapshot, LivePresentationState
from k5vision.media.mixed_presentation import MixedLiveStream, MixedPresentationStream
from k5vision.media.viewport_geometry import ViewportGeometry, ViewportLayout, ViewportPlacement
from k5vision.media.windows_operator_application import (
    WindowsOperatorApplicationSnapshot,
    WindowsOperatorApplicationState,
)
from k5vision.media.windows_operator_control import BoundedWindowsOperatorControl
from k5vision.media.windows_operator_session import WindowsOperatorSessionState


class UnusedLiveDelivery:
    async def run(self, _source_uri: str, _consumer: object) -> LivePresentationSnapshot:
        return LivePresentationSnapshot(
            state=LivePresentationState.COMPLETE,
            decoder_initialized=True,
            accepted_packets=0,
            rtp_valid_packets=0,
            rtp_invalid_packets=0,
            rtp_delivered_bytes=0,
            delivered_frames=0,
            delivered_frame_bytes=0,
            source_span_ms=0,
        )


def _layout(first_slot: int, second_slot: int, offset: int = 0) -> ViewportLayout:
    return ViewportLayout(
        placements=(
            ViewportPlacement(
                logical_slot=first_slot,
                geometry=ViewportGeometry(
                    x=17 + offset,
                    y=29,
                    width=613,
                    height=347,
                    z_index=2,
                ),
            ),
            ViewportPlacement(
                logical_slot=second_slot,
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


def _streams(first_slot: int, second_slot: int) -> tuple[MixedLiveStream, MixedLiveStream]:
    delivery = UnusedLiveDelivery()
    return (
        MixedLiveStream(first_slot, "rtsp://synthetic.invalid/live-a", delivery),
        MixedLiveStream(second_slot, "rtsp://synthetic.invalid/live-b", delivery),
    )


def _snapshot(
    state: WindowsOperatorApplicationState,
    *,
    shell_open: bool,
    generation: int,
    pumped: int = 0,
) -> WindowsOperatorApplicationSnapshot:
    return WindowsOperatorApplicationSnapshot(
        state=state,
        shell_open=shell_open,
        pump_cycles=pumped,
        pumped_messages=pumped,
        generation=generation,
        viewport_count=2,
        open_surface_count=2 if state == WindowsOperatorApplicationState.RUNNING else 0,
        delivered_frames=generation * 2,
        presentations=generation * 2,
    )


class RecordingAllLiveApplication:
    def __init__(self) -> None:
        self.generation = 0
        self.pumps = 0
        self.plans: list[tuple[tuple[int, ...], tuple[int, ...]]] = []
        self._wait = asyncio.Event()
        self._snapshot = _snapshot(
            WindowsOperatorApplicationState.READY,
            shell_open=False,
            generation=0,
        )

    @property
    def snapshot(self) -> WindowsOperatorApplicationSnapshot:
        return self._snapshot

    async def open(self, _width: int, _height: int) -> WindowsOperatorApplicationSnapshot:
        self._snapshot = _snapshot(
            WindowsOperatorApplicationState.OPEN,
            shell_open=True,
            generation=self.generation,
        )
        return self._snapshot

    @staticmethod
    def _plan(
        layout: ViewportLayout,
        streams: Sequence[MixedPresentationStream],
    ) -> tuple[tuple[int, ...], tuple[int, ...]]:
        assert all(isinstance(stream, MixedLiveStream) for stream in streams)
        return (
            tuple(item.logical_slot for item in layout.placements),
            tuple(stream.slot for stream in streams),
        )

    async def start(
        self,
        layout: ViewportLayout,
        streams: Sequence[MixedPresentationStream],
    ) -> WindowsOperatorApplicationSnapshot:
        self.plans.append(self._plan(layout, streams))
        self.generation = 1
        self._snapshot = _snapshot(
            WindowsOperatorApplicationState.RUNNING,
            shell_open=True,
            generation=self.generation,
        )
        return self._snapshot

    async def replace(
        self,
        layout: ViewportLayout,
        streams: Sequence[MixedPresentationStream],
    ) -> WindowsOperatorApplicationSnapshot:
        self.plans.append(self._plan(layout, streams))
        self.generation += 1
        self._snapshot = _snapshot(
            WindowsOperatorApplicationState.RUNNING,
            shell_open=True,
            generation=self.generation,
            pumped=self.pumps,
        )
        return self._snapshot

    async def wait(self) -> WindowsOperatorApplicationSnapshot:
        await self._wait.wait()
        return self._snapshot

    async def stop(self) -> WindowsOperatorApplicationSnapshot:
        self._snapshot = _snapshot(
            WindowsOperatorApplicationState.STOPPED,
            shell_open=True,
            generation=self.generation,
            pumped=self.pumps,
        )
        return self._snapshot

    async def pump(self, *, max_messages: int = 64) -> WindowsOperatorApplicationSnapshot:
        assert 1 <= max_messages <= 256
        self.pumps += 1
        self._snapshot = _snapshot(
            WindowsOperatorApplicationState.RUNNING,
            shell_open=True,
            generation=self.generation,
            pumped=self.pumps,
        )
        await asyncio.sleep(0)
        return self._snapshot

    async def close(self) -> WindowsOperatorApplicationSnapshot:
        self._snapshot = _snapshot(
            WindowsOperatorApplicationState.CLOSED,
            shell_open=False,
            generation=self.generation,
            pumped=self.pumps,
        )
        return self._snapshot


def test_control_preserves_all_live_sparse_plan_across_same_shell_replacement() -> None:
    async def scenario() -> None:
        app = RecordingAllLiveApplication()
        control = BoundedWindowsOperatorControl(
            application_factory=lambda: app,
            poll_interval_seconds=0.001,
            max_cycles=500,
        )
        task = asyncio.create_task(
            control.run(
                width=1280,
                height=900,
                layout=_layout(7, 4095),
                streams=_streams(7, 4095),
            )
        )

        while app.pumps == 0:
            await asyncio.sleep(0)
        control.request_replace(
            _layout(23, 4000, offset=37),
            _streams(23, 4000),
        )
        while control.control_snapshot.replacements == 0:
            await asyncio.sleep(0)

        control.request_stop()
        finished = await task
        assert finished.session.state == WindowsOperatorSessionState.COMPLETE
        assert finished.session.shell_open is False
        assert finished.replacements == 1
        assert finished.stop_requests == 1
        assert app.plans == [
            ((7, 4095), (7, 4095)),
            ((23, 4000), (23, 4000)),
        ]

        retained = finished.model_dump_json().casefold()
        for forbidden in (
            "rtsp://",
            "synthetic.invalid",
            "4095",
            "4000",
            "logical_slot",
            "source_id",
            "recording_id",
            "payload",
            "handle",
            "pointer",
        ):
            assert forbidden not in retained

    asyncio.run(scenario())
