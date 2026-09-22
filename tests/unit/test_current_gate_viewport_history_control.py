from __future__ import annotations

import asyncio

import pytest

from k5vision.media.viewport_editor import ViewportMove
from k5vision.media.viewport_geometry import ViewportGeometry, ViewportLayout, ViewportPlacement
from k5vision.media.viewport_history_control import (
    TransactionalViewportHistoryControl,
    ViewportHistoryControlError,
    ViewportHistoryControlErrorCode,
)


def _layout(*, x: int = 17) -> ViewportLayout:
    return ViewportLayout(
        placements=(
            ViewportPlacement(
                logical_slot=7,
                geometry=ViewportGeometry(x=x, y=29, width=613, height=347, z_index=2),
            ),
        )
    )


class _Relayout:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.layouts: list[ViewportLayout] = []

    async def relayout(self, layout: ViewportLayout) -> object:
        if self.fail:
            raise RuntimeError("native detail must be sanitized")
        self.layouts.append(layout)
        return object()


def test_apply_undo_redo_commit_only_after_visible_acceptance() -> None:
    async def scenario() -> None:
        application = _Relayout()
        control = TransactionalViewportHistoryControl(_layout())
        original = control.snapshot.layout
        edited = await control.apply(application, ViewportMove(logical_slot=7, dx=31, dy=47))
        assert edited.layout != original
        assert edited.undo_depth == 1
        undone = await control.undo(application)
        assert undone.layout == original
        assert undone.redo_depth == 1
        redone = await control.redo(application)
        assert redone.layout == edited.layout
        assert application.layouts == [edited.layout, original, edited.layout]

    asyncio.run(scenario())


def test_relayout_failure_does_not_mutate_history() -> None:
    async def scenario() -> None:
        application = _Relayout(fail=True)
        control = TransactionalViewportHistoryControl(_layout())
        before = control.snapshot
        with pytest.raises(ViewportHistoryControlError) as caught:
            await control.apply(application, ViewportMove(logical_slot=7, dx=10, dy=5))
        assert caught.value.code == ViewportHistoryControlErrorCode.RELAYOUT_FAILURE
        assert str(caught.value) == "viewport relayout was rejected"
        assert control.snapshot == before

    asyncio.run(scenario())


def test_failed_undo_keeps_accepted_history_intact() -> None:
    async def scenario() -> None:
        application = _Relayout()
        control = TransactionalViewportHistoryControl(_layout())
        await control.apply(application, ViewportMove(logical_slot=7, dx=8, dy=9))
        before = control.snapshot
        application.fail = True
        with pytest.raises(ViewportHistoryControlError) as caught:
            await control.undo(application)
        assert caught.value.code == ViewportHistoryControlErrorCode.RELAYOUT_FAILURE
        assert control.snapshot == before

    asyncio.run(scenario())


def test_operation_limit_is_rejected_before_visible_relayout() -> None:
    async def scenario() -> None:
        application = _Relayout()
        control = TransactionalViewportHistoryControl(_layout(), max_operations=1)
        edited = await control.apply(application, ViewportMove(logical_slot=7, dx=8, dy=9))
        assert application.layouts == [edited.layout]
        before = control.snapshot
        with pytest.raises(ViewportHistoryControlError) as caught:
            await control.undo(application)
        assert caught.value.code == ViewportHistoryControlErrorCode.HISTORY_FAILURE
        assert str(caught.value) == "viewport history operation limit reached"
        assert control.snapshot == before
        assert application.layouts == [edited.layout]

    asyncio.run(scenario())


def test_explicit_rebase_clears_stale_history_and_preserves_operation_count() -> None:
    async def scenario() -> None:
        application = _Relayout()
        control = TransactionalViewportHistoryControl(_layout())
        edited = await control.apply(application, ViewportMove(logical_slot=7, dx=8, dy=9))
        rebased = control.rebase(_layout(x=101))
        assert rebased.layout == _layout(x=101)
        assert rebased.undo_depth == 0
        assert rebased.redo_depth == 0
        assert rebased.operations == 1
        with pytest.raises(ViewportHistoryControlError) as caught:
            await control.undo(application)
        assert caught.value.code == ViewportHistoryControlErrorCode.HISTORY_FAILURE
        assert application.layouts == [edited.layout]

    asyncio.run(scenario())


def test_observability_is_source_free_and_bounded() -> None:
    control = TransactionalViewportHistoryControl(_layout(), max_history=2, max_operations=5)
    payload = control.snapshot.model_dump_json().casefold()
    for forbidden in (
        "rtsp://",
        "credential",
        "password",
        "source_id",
        "recording_id",
        "payload",
        "handle",
        "pointer",
    ):
        assert forbidden not in payload
