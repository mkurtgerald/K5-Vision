"""Authenticated composition through the actual control loop with source-only shells."""

from __future__ import annotations

import asyncio
from collections import deque
from uuid import uuid4

import pytest
from test_operator_playback import (
    _PresentationDelivery,
    _principal,
    _registry,
    _write_recording_pair,
)

from k5vision.media.playback_control import PlaybackControlState, PlaybackPauseControl
from k5vision.media.playback_schedule import PlaybackRate
from k5vision.media.recording_descriptor import RecordingStreamDescriptor
from k5vision.media.viewport_geometry import ViewportGeometry, ViewportLayout, ViewportPlacement
from k5vision.media.windows_operator_application import (
    WindowsOperatorApplicationSnapshot,
    WindowsOperatorApplicationState,
)
from k5vision.media.windows_operator_playback_ui import (
    BoundedPlaybackWindowsOperatorApplication,
    BoundedPlaybackWindowsOperatorControl,
    WindowsPlaybackCommand,
)
from k5vision.operator_launch import ResolvedLiveSource
from k5vision.operator_playback import (
    BoundedOperatorPlaybackCoordinator,
    OperatorPlaybackControlAction,
    OperatorPlaybackError,
    OperatorPlaybackErrorCode,
    OperatorPlaybackRequest,
    WindowsMixedOperatorPlaybackLauncher,
)
from k5vision.operator_runtime import _stage_one_presentation_runtime_factory


class Application:
    def __init__(self, pause, *, fail=None):
        self.pause = pause
        self.fail = fail
        self.state = WindowsOperatorApplicationState.READY
        self.commands = deque()
        self.closed = 0
        self.shell_open = False
        self.started = asyncio.Event()
        self.finish = asyncio.Event()
        self.user_close = False
        self.streams = ()
        self.layouts = []
        self.pumps = 0
        self.visible_state = None

    @property
    def snapshot(self):
        return WindowsOperatorApplicationSnapshot(
            state=self.state,
            shell_open=self.shell_open,
            pump_cycles=self.pumps,
            pumped_messages=self.pumps,
            generation=int(bool(self.layouts)),
            viewport_count=len(self.streams),
            open_surface_count=2 if self.shell_open else 0,
            delivered_frames=14 if self.layouts else 0,
            presentations=14 if self.layouts else 0,
        )

    async def open(self, width, height):
        self.shell_open = True
        self.state = WindowsOperatorApplicationState.OPEN
        if self.fail == "open":
            raise RuntimeError("private source")
        return self.snapshot

    async def start(self, layout, streams):
        self.layouts.append(layout)
        self.streams = streams
        if self.fail == "start":
            raise RuntimeError("private source")
        self.state = WindowsOperatorApplicationState.RUNNING
        self.started.set()
        return self.snapshot

    async def replace(self, layout, streams):
        raise AssertionError("playback controls must not replace a media generation")

    async def pump(self, **kwargs):
        self.pumps += 1
        if self.user_close:
            await self.close()
        return self.snapshot

    async def wait(self):
        await self.finish.wait()
        if self.fail == "runtime":
            raise RuntimeError("private source")
        self.state = WindowsOperatorApplicationState.COMPLETE
        return self.snapshot

    async def stop(self):
        self.state = WindowsOperatorApplicationState.STOPPED
        return self.snapshot

    async def close(self):
        if self.state is not WindowsOperatorApplicationState.CLOSED:
            self.closed += 1
        self.shell_open = False
        self.state = WindowsOperatorApplicationState.CLOSED
        if self.fail == "cleanup":
            raise RuntimeError("private source")
        return self.snapshot

    def drain_playback_commands(self, *, max_commands):
        result = []
        while self.commands and len(result) < max_commands:
            result.append(self.commands.popleft())
        return tuple(result)

    def refresh_playback_state(self):
        self.visible_state = self.pause.snapshot.state


async def eventually(predicate):
    # The coordinator verifies local descriptors on to_thread(). Yield real time
    # to its worker rather than consuming an arbitrary number of zero-time ticks.
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.001)


def make_launcher(apps, deliveries, *, fail=None):
    def control_factory(pause):
        app = Application(pause, fail=fail)
        apps.append(app)
        return BoundedPlaybackWindowsOperatorControl(
            pause_control=pause,
            application_factory=lambda: app,
            poll_interval_seconds=0,
        )

    def playback_factory(path, descriptor, start, end, rate, *, pause_control):
        deliveries.append((path, descriptor, pause_control))
        return _PresentationDelivery()

    return WindowsMixedOperatorPlaybackLauncher(
        control_factory=control_factory,
        live_delivery_factory=lambda _: _PresentationDelivery(),
        playback_factory=playback_factory,
    )


async def pair(tmp_path):
    recording_id, source_id = uuid4(), uuid4()
    await _write_recording_pair(tmp_path, recording_id, source_id)
    return (
        tmp_path / f"{recording_id}.k5r",
        RecordingStreamDescriptor.model_validate_json(
            (tmp_path / f"{recording_id}.k5d").read_text()
        ),
    )


def test_default_constructor_is_parent_bound_and_keeps_stage_one_budget():
    pause = PlaybackPauseControl()
    session = WindowsMixedOperatorPlaybackLauncher._default_control_factory(pause)
    app = session._application_factory()
    assert isinstance(app, BoundedPlaybackWindowsOperatorApplication)
    assert app._pause_control is session._pause_control is pause
    host = app._host_factory(71)
    layout = ViewportLayout(
        placements=tuple(
            ViewportPlacement(
                logical_slot=i, geometry=ViewportGeometry(x=i * 640, y=0, width=640, height=720)
            )
            for i in range(2)
        )
    )
    runtime = host._runtime_factory(layout)
    assert runtime._presentation_runtime_factory is _stage_one_presentation_runtime_factory
    windows = runtime._windows_runtime_factory(layout)
    targets = windows._layout_factory(layout)
    assert targets._target_factory()._parent_handle == 71
    assert session._max_controls_per_cycle == 4
    assert app._content_top == 40


@pytest.mark.parametrize("terminal", ["complete", "close", "stop"])
def test_launcher_uses_real_session_counts_and_completion_semantics(tmp_path, terminal):
    async def scenario():
        path, descriptor = await pair(tmp_path)
        apps, deliveries = [], []
        launcher = make_launcher(apps, deliveries)
        task = asyncio.create_task(
            launcher.run(
                ResolvedLiveSource("rtsp://synthetic@192.0.2.20/live", 96),
                path,
                descriptor,
                start_ms=0,
                end_ms=100,
                rate=PlaybackRate.NORMAL,
                width=1280,
                height=720,
            )
        )
        await eventually(lambda: apps and apps[0].started.is_set())
        app = apps[0]
        assert app.pause is deliveries[0][2]
        app.commands.append(WindowsPlaybackCommand.PAUSE)
        await eventually(lambda: app.pause.snapshot.state is PlaybackControlState.PAUSED)
        if terminal == "complete":
            await app.pause.resume()
            app.finish.set()
        elif terminal == "close":
            app.user_close = True
        else:
            app.commands.append(WindowsPlaybackCommand.STOP)
        result = await task
        assert result.completed is (terminal == "complete")
        assert result.processed_controls == (2 if terminal == "stop" else 1)
        assert result.delivered_frames == result.presentations == 14
        assert app.closed == 1 and not app.shell_open
        assert len(app.layouts) == 1
        assert [stream.slot for stream in app.streams] == [0, 1]
        assert deliveries[0][1] is descriptor
        assert "192.0.2.20" not in result.model_dump_json()

    asyncio.run(scenario())


@pytest.mark.parametrize("fail", ["open", "start", "runtime", "cleanup", "cancel"])
def test_launcher_failure_or_cancel_never_returns_success_and_cleans_once(tmp_path, fail):
    async def scenario():
        path, descriptor = await pair(tmp_path)
        apps, deliveries = [], []
        launcher = make_launcher(apps, deliveries, fail=fail)
        pause = PlaybackPauseControl()
        await pause.pause()
        task = asyncio.create_task(
            launcher.run(
                ResolvedLiveSource("rtsp://synthetic@192.0.2.20/live", 96),
                path,
                descriptor,
                start_ms=0,
                end_ms=100,
                rate=PlaybackRate.NORMAL,
                width=1280,
                height=720,
                pause_control=pause,
            )
        )
        await eventually(lambda: bool(apps))
        if fail not in {"open", "start"}:
            await apps[0].started.wait()
            if fail == "cancel":
                task.cancel()
            else:
                apps[0].finish.set()
        with pytest.raises(
            asyncio.CancelledError if fail == "cancel" else OperatorPlaybackError
        ) as caught:
            await task
        assert "private source" not in str(caught.value)
        assert apps[0].closed == 1 and deliveries[0][2] is pause

    asyncio.run(scenario())


def test_two_authenticated_runs_keep_identity_capacity_and_independent_controls(tmp_path):
    async def scenario():
        registry, device = _registry(tmp_path)
        root = tmp_path / "recordings"
        recording_ids = (uuid4(), uuid4())
        for rid in recording_ids:
            await _write_recording_pair(root, rid, device.id)
        apps, deliveries, resolved = [], [], []

        class Resolver:
            async def resolve(self, device, token):
                value = ResolvedLiveSource(f"rtsp://synthetic@{device.host}/{token}", 96)
                resolved.append(value)
                return value

        coordinator = BoundedOperatorPlaybackCoordinator(
            registry, Resolver(), root, make_launcher(apps, deliveries)
        )
        principal = _principal()
        controls = (uuid4(), uuid4())
        tasks = [
            asyncio.create_task(
                coordinator.play(
                    principal,
                    OperatorPlaybackRequest(
                        recording_id=rid,
                        control_id=cid,
                        stream_token=token,
                    ),
                )
            )
            for rid, cid, token in zip(recording_ids, controls, ("main", "sub"), strict=True)
        ]
        try:
            await eventually(lambda: len(apps) == 2 and all(a.started.is_set() for a in apps))
            assert coordinator.active_playbacks == 2
            with pytest.raises(OperatorPlaybackError) as busy:
                await coordinator.play(
                    principal, OperatorPlaybackRequest(recording_id=recording_ids[0])
                )
            assert busy.value.code is OperatorPlaybackErrorCode.PLAYBACK_BUSY
            assert apps[0].pause is not apps[1].pause
            for app, delivered, source in zip(apps, deliveries, resolved, strict=True):
                assert app.pause is delivered[2]
                assert app.streams[0].source_uri == source.source_uri
            # Map by descriptor, not nondeterministic task scheduling order.
            by_id = {
                delivery[1].recording_id: app
                for delivery, app in zip(deliveries, apps, strict=True)
            }
            first, second = (by_id[rid] for rid in recording_ids)
            await coordinator.control(principal, controls[0], OperatorPlaybackControlAction.PAUSE)
            await eventually(lambda: first.visible_state is PlaybackControlState.PAUSED)
            assert second.pause.snapshot.state is PlaybackControlState.RUNNING
            with pytest.raises(OperatorPlaybackError) as denied:
                await coordinator.control(
                    _principal(), controls[0], OperatorPlaybackControlAction.RESUME
                )
            assert denied.value.code is OperatorPlaybackErrorCode.CONTROL_FORBIDDEN
            first.commands.append(WindowsPlaybackCommand.STOP)
            one = await tasks[0]
            assert not one.completed and coordinator.active_playbacks == 1
            assert not tasks[1].done() and second.pumps > 0
            with pytest.raises(OperatorPlaybackError) as gone:
                await coordinator.control(
                    principal, controls[0], OperatorPlaybackControlAction.RESUME
                )
            assert gone.value.code is OperatorPlaybackErrorCode.CONTROL_NOT_FOUND
            second.finish.set()
            two = await tasks[1]
            assert two.completed and coordinator.active_playbacks == 0
            assert all(app.closed == 1 for app in apps)
            assert not coordinator._controls
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            registry.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "failure", ["unauthorized", "source_scope", "source_failure", "descriptor"]
)
def test_authorization_and_current_source_fail_before_shell_construction(tmp_path, failure):
    async def scenario():
        registry, device = _registry(tmp_path)
        root, rid, cid = tmp_path / "recordings", uuid4(), uuid4()
        await _write_recording_pair(root, rid, device.id)
        apps, deliveries = [], []

        class Resolver:
            async def resolve(self, device, token):
                if failure == "source_failure":
                    raise RuntimeError("private credential")
                return ResolvedLiveSource("rtsp://192.0.2.99/live", 96)

        coordinator = BoundedOperatorPlaybackCoordinator(
            registry, Resolver(), root, make_launcher(apps, deliveries)
        )
        principal = _principal()
        if failure == "unauthorized":
            principal = principal.model_copy(update={"enabled": False})
        if failure == "descriptor":
            (root / f"{rid}.k5d").write_text("invalid")
        try:
            with pytest.raises(OperatorPlaybackError) as caught:
                await coordinator.play(
                    principal, OperatorPlaybackRequest(recording_id=rid, control_id=cid)
                )
            assert "private credential" not in str(caught.value)
            assert not apps and not deliveries
            assert coordinator.active_playbacks == 0 and not coordinator._controls
        finally:
            registry.close()

    asyncio.run(scenario())
