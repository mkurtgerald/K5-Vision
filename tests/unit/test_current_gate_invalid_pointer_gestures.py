from __future__ import annotations

import asyncio

import pytest
from test_current_gate_pointer_interaction import _event, _InteractiveFakeApplication

from k5vision.media.playback_control import PlaybackPauseControl
from k5vision.media.viewport_editor import ViewportMove
from k5vision.media.viewport_geometry import ViewportGeometry, ViewportLayout, ViewportPlacement
from k5vision.media.windows_operator_application import WindowsOperatorApplicationState
from k5vision.media.windows_operator_control import (
    BoundedWindowsOperatorControl,
    WindowsOperatorControlError,
    WindowsOperatorControlErrorCode,
)
from k5vision.media.windows_operator_history_control import BoundedHistoryWindowsOperatorControl
from k5vision.media.windows_operator_interaction import WindowsPointerEventKind
from k5vision.media.windows_operator_playback_ui import BoundedPlaybackWindowsOperatorControl
from k5vision.media.windows_operator_selection import BoundedSelectableWindowsOperatorControl
from k5vision.media.windows_operator_session import (
    WindowsOperatorSessionError,
    WindowsOperatorSessionState,
)


def _layout() -> ViewportLayout:
    return ViewportLayout(
        placements=(
            ViewportPlacement(
                logical_slot=7,
                geometry=ViewportGeometry(x=10, y=10, width=200, height=200),
            ),
            ViewportPlacement(
                logical_slot=4095,
                geometry=ViewportGeometry(x=300, y=10, width=200, height=200),
            ),
        )
    )


class _Application(_InteractiveFakeApplication):
    def __init__(self, batches=(), *, fail_relayout=False):
        super().__init__(batches)
        self.close_calls = 0
        self.fail_relayout = fail_relayout
        self.replacements = []

    async def replace(self, layout, streams):
        self.replacements.append(layout)
        return await super().replace(layout, streams)

    async def relayout(self, layout):
        if self.fail_relayout:
            raise RuntimeError("synthetic relayout failure")
        return await super().relayout(layout)

    async def close(self):
        self.close_calls += 1
        return await super().close()

    def drain_playback_commands(self, *, max_commands):
        return ()

    def refresh_playback_state(self):
        pass


@pytest.fixture(params=["base", "history", "selectable", "playback"])
def control_factory(request):
    classes = {
        "base": BoundedWindowsOperatorControl,
        "history": BoundedHistoryWindowsOperatorControl,
        "selectable": BoundedSelectableWindowsOperatorControl,
        "playback": BoundedPlaybackWindowsOperatorControl,
    }

    def build(app):
        kwargs = {"pause_control": PlaybackPauseControl()} if request.param == "playback" else {}
        return classes[request.param](
            application_factory=lambda: app,
            poll_interval_seconds=0,
            max_cycles=1000,
            **kwargs,
        )

    return build


async def _until(task, predicate):
    for _ in range(100):
        if task.done():
            await task
            raise AssertionError("operator session ended before the gesture settled")
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("operator gesture did not settle")


@pytest.mark.parametrize(
    ("down", "up"),
    [
        ((100, 100), (50, 100)),  # Both points inside the canvas; resulting x is negative.
        ((100, 100), (100, 50)),  # Resulting y is negative.
        ((205, 205), (0, 205)),  # Resize crosses the tile's left edge.
        ((205, 205), (205, 0)),  # Resize crosses the tile's top edge.
        ((205, 205), (5, 5)),  # Both dimensions would be zero.
    ],
)
def test_invalid_pointer_geometry_keeps_session_and_later_gesture_usable(control_factory, down, up):
    async def scenario():
        invalid = (
            _event(WindowsPointerEventKind.DOWN, *down),
            _event(WindowsPointerEventKind.UP, *up),
        )
        app = _Application((invalid, invalid))
        control = control_factory(app)
        original = _layout()
        task = asyncio.create_task(control.run(width=900, height=700, layout=original, streams=()))
        try:
            await app.wait_entered.wait()
            await _until(task, lambda: control.control_snapshot.cancelled_interactions == 2)
            snapshot = control.control_snapshot
            assert snapshot.active_layout == original
            assert snapshot.viewport_edits == snapshot.interaction_edits == snapshot.relayouts == 0
            assert snapshot.processed_controls == 0
            assert snapshot.session.state is WindowsOperatorSessionState.RUNNING
            assert snapshot.session.generation == 1
            assert snapshot.session.open_surface_count == 2
            assert app.relayouts == [] and app.close_calls == 0
            if isinstance(control, BoundedHistoryWindowsOperatorControl):
                history = control.history_control_snapshot
                assert history.undo_depth == history.redo_depth == history.history_operations == 0

            app.batches.append(
                (
                    _event(WindowsPointerEventKind.DOWN, 100, 100),
                    _event(WindowsPointerEventKind.UP, 120, 130),
                )
            )
            await _until(task, lambda: len(app.relayouts) == 1)
            snapshot = control.control_snapshot
            assert snapshot.active_layout.by_slot()[4095] == original.by_slot()[4095]
            moved = snapshot.active_layout.by_slot()[7]
            assert (moved.x, moved.y) == (30, 40)
            assert snapshot.viewport_edits == snapshot.interaction_edits == snapshot.relayouts == 1
            assert snapshot.cancelled_interactions == 2 and snapshot.replacements == 0
            if isinstance(control, BoundedHistoryWindowsOperatorControl):
                history = control.history_control_snapshot
                assert history.undo_depth == history.history_operations == 1
                assert history.redo_depth == 0
            control.request_stop()
            await task
            assert control.control_snapshot.session.state in {
                WindowsOperatorSessionState.COMPLETE,
                WindowsOperatorSessionState.USER_CLOSED,
            }
            assert app.close_calls == 1
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize("slot", [7, 123])
def test_explicit_invalid_or_missing_slot_edit_still_fails_closed(control_factory, slot):
    async def scenario():
        app = _Application()
        control = control_factory(app)
        original = _layout()
        task = asyncio.create_task(control.run(width=900, height=700, layout=original, streams=()))
        await app.wait_entered.wait()
        control.request_edit(ViewportMove(logical_slot=slot, dx=-50, dy=0))
        with pytest.raises(WindowsOperatorControlError) as caught:
            await task
        assert caught.value.code is WindowsOperatorControlErrorCode.INVALID_EDIT
        assert control.control_snapshot.session.state is WindowsOperatorSessionState.FAILED
        assert control.control_snapshot.active_layout == original
        assert control.control_snapshot.cancelled_interactions == 0
        assert app.relayouts == [] and app.close_calls == 1

    asyncio.run(scenario())


def test_valid_pointer_relayout_failure_still_fails_closed(control_factory):
    async def scenario():
        app = _Application(
            (
                (
                    _event(WindowsPointerEventKind.DOWN, 100, 100),
                    _event(WindowsPointerEventKind.UP, 120, 130),
                ),
            ),
            fail_relayout=True,
        )
        control = control_factory(app)
        original = _layout()
        with pytest.raises((WindowsOperatorSessionError, WindowsOperatorControlError)):
            await control.run(width=900, height=700, layout=original, streams=())
        assert control.control_snapshot.session.state is WindowsOperatorSessionState.FAILED
        assert control.control_snapshot.active_layout == original
        assert control.control_snapshot.cancelled_interactions == 0
        assert app.state is WindowsOperatorApplicationState.CLOSED and app.close_calls == 1

    asyncio.run(scenario())


def test_pointer_edit_of_slot_removed_by_earlier_queued_replacement_still_fails(control_factory):
    async def scenario():
        app = _Application()
        control = control_factory(app)
        original = _layout()
        reduced = ViewportLayout(placements=(original.placements[1],))
        task = asyncio.create_task(control.run(width=900, height=700, layout=original, streams=()))
        await app.wait_entered.wait()
        control.request_replace(reduced, ())
        # Pointer admission still sees the old layout; acceptance must validate
        # against the newer layout from the earlier serialized request.
        control._consume_pointer_events(
            (
                _event(WindowsPointerEventKind.DOWN, 100, 100),
                _event(WindowsPointerEventKind.UP, 120, 130),
            )
        )
        with pytest.raises(WindowsOperatorControlError) as caught:
            await task
        assert caught.value.code is WindowsOperatorControlErrorCode.INVALID_EDIT
        assert control.control_snapshot.session.state is WindowsOperatorSessionState.FAILED
        assert control.control_snapshot.active_layout == reduced
        assert control.control_snapshot.cancelled_interactions == 0
        assert app.replacements == [reduced] and app.relayouts == [] and app.close_calls == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "control_class", [BoundedHistoryWindowsOperatorControl, BoundedSelectableWindowsOperatorControl]
)
def test_rejected_pointer_geometry_preserves_existing_redo_history(control_class):
    async def scenario():
        app = _Application()
        control = control_class(
            application_factory=lambda: app, poll_interval_seconds=0, max_cycles=1000
        )
        original = _layout()
        task = asyncio.create_task(control.run(width=900, height=700, layout=original, streams=()))
        try:
            await app.wait_entered.wait()
            control.request_edit(ViewportMove(logical_slot=7, dx=20, dy=30))
            await _until(task, lambda: control.history_control_snapshot.undo_depth == 1)
            edited = control.control_snapshot.active_layout
            control.request_undo()
            await _until(task, lambda: control.history_control_snapshot.redo_depth == 1)
            before = control.history_control_snapshot
            app.batches.append(
                (
                    _event(WindowsPointerEventKind.DOWN, 100, 100),
                    _event(WindowsPointerEventKind.UP, 50, 100),
                )
            )
            await _until(task, lambda: control.control_snapshot.cancelled_interactions == 1)
            after = control.history_control_snapshot
            assert after.undo_depth == before.undo_depth == 0
            assert after.redo_depth == before.redo_depth == 1
            assert after.history_operations == before.history_operations == 2
            assert control.control_snapshot.active_layout == original
            assert app.close_calls == 0 and app.relayouts == [edited, original]
            control.request_redo()
            await _until(task, lambda: control.history_control_snapshot.redo_requests == 1)
            assert control.control_snapshot.active_layout == edited
            assert control.history_control_snapshot.history_operations == 3
            assert control.history_control_snapshot.redo_depth == 0
            control.request_stop()
            await task
            assert app.close_calls == 1
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
