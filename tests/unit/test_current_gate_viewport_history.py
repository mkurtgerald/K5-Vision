from __future__ import annotations

import pytest

from k5vision.media.viewport_editor import ViewportMove, ViewportResize
from k5vision.media.viewport_editor_history import (
    BoundedViewportEditorHistory,
    ViewportEditorHistoryError,
    ViewportEditorHistoryErrorCode,
)
from k5vision.media.viewport_geometry import ViewportGeometry, ViewportLayout, ViewportPlacement


def _layout(*, x: int = 17) -> ViewportLayout:
    return ViewportLayout(
        placements=(
            ViewportPlacement(
                logical_slot=7,
                geometry=ViewportGeometry(x=x, y=29, width=613, height=347, z_index=2),
            ),
        )
    )


def test_edit_undo_redo_round_trip_is_deterministic() -> None:
    history = BoundedViewportEditorHistory(_layout())
    original = history.snapshot.layout
    edited = history.apply(ViewportMove(logical_slot=7, dx=31, dy=47)).layout
    assert edited != original
    assert history.undo().layout == original
    assert history.redo().layout == edited


def test_new_edit_after_undo_clears_redo_branch() -> None:
    history = BoundedViewportEditorHistory(_layout())
    history.apply(ViewportMove(logical_slot=7, dx=10, dy=10))
    history.undo()
    history.apply(ViewportResize(logical_slot=7, dwidth=25, dheight=15))
    with pytest.raises(ViewportEditorHistoryError) as caught:
        history.redo()
    assert caught.value.code == ViewportEditorHistoryErrorCode.NOTHING_TO_REDO


def test_history_is_bounded() -> None:
    history = BoundedViewportEditorHistory(_layout(), max_history=2)
    for _ in range(3):
        history.apply(ViewportMove(logical_slot=7, dx=1, dy=0))
    assert history.snapshot.undo_depth == 2
    history.undo()
    history.undo()
    with pytest.raises(ViewportEditorHistoryError) as caught:
        history.undo()
    assert caught.value.code == ViewportEditorHistoryErrorCode.NOTHING_TO_UNDO


def test_rebase_clears_stale_branches_without_resetting_lifetime_budget() -> None:
    history = BoundedViewportEditorHistory(_layout(), max_operations=4)
    history.apply(ViewportMove(logical_slot=7, dx=10, dy=0))
    history.undo()
    assert history.snapshot.redo_depth == 1
    rebased = history.rebase(_layout(x=101))
    assert rebased.layout == _layout(x=101)
    assert rebased.undo_depth == 0
    assert rebased.redo_depth == 0
    assert rebased.operations == 2
    history.ensure_operation_available()


def test_operation_preflight_fails_without_mutating_state() -> None:
    history = BoundedViewportEditorHistory(_layout(), max_operations=1)
    history.apply(ViewportMove(logical_slot=7, dx=1, dy=0))
    before = history.snapshot
    with pytest.raises(ViewportEditorHistoryError) as caught:
        history.ensure_operation_available()
    assert caught.value.code == ViewportEditorHistoryErrorCode.OPERATION_LIMIT
    assert history.snapshot == before


def test_history_observability_is_source_free() -> None:
    history = BoundedViewportEditorHistory(_layout())
    history.apply(ViewportMove(logical_slot=7, dx=1, dy=2))
    payload = history.snapshot.model_dump_json().casefold()
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
