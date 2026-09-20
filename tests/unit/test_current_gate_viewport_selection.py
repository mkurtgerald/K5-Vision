from __future__ import annotations

from k5vision.media.viewport_geometry import ViewportGeometry, ViewportLayout, ViewportPlacement
from k5vision.media.windows_operator_interaction import WindowsPointerEvent, WindowsPointerEventKind
from k5vision.media.windows_operator_selection import BoundedSelectableWindowsOperatorControl


def _layout(*, include_top: bool = True) -> ViewportLayout:
    placements = [
        ViewportPlacement(
            logical_slot=7,
            geometry=ViewportGeometry(x=10, y=10, width=200, height=200, z_index=1),
        )
    ]
    if include_top:
        placements.append(
            ViewportPlacement(
                logical_slot=4095,
                geometry=ViewportGeometry(x=50, y=50, width=100, height=100, z_index=3),
            )
        )
    return ViewportLayout(placements=tuple(placements))


def _event(kind: WindowsPointerEventKind, x: int, y: int) -> WindowsPointerEvent:
    return WindowsPointerEvent(kind=kind, x=x, y=y)


def _control() -> BoundedSelectableWindowsOperatorControl:
    control = BoundedSelectableWindowsOperatorControl()
    control._active_layout = _layout()
    return control


def _click(control: BoundedSelectableWindowsOperatorControl, x: int, y: int) -> None:
    control._consume_pointer_events(
        (
            _event(WindowsPointerEventKind.DOWN, x, y),
            _event(WindowsPointerEventKind.UP, x, y),
        )
    )


def test_click_selects_topmost_sparse_slot_without_geometry_change() -> None:
    control = _control()
    original = control._active_layout

    _click(control, 75, 75)

    snapshot = control.selection_snapshot
    assert snapshot.selected_logical_slot == 4095
    assert snapshot.selection_changes == 1
    assert control._active_layout is original
    assert control._controls.qsize() == 0


def test_click_on_background_clears_selection() -> None:
    control = _control()
    _click(control, 75, 75)
    _click(control, 500, 500)

    snapshot = control.selection_snapshot
    assert snapshot.selected_logical_slot is None
    assert snapshot.selection_changes == 2
    assert control._controls.qsize() == 0


def test_zero_delta_resize_handle_click_selects_without_edit() -> None:
    control = _control()

    _click(control, 205, 205)

    snapshot = control.selection_snapshot
    assert snapshot.selected_logical_slot == 7
    assert snapshot.control.viewport_edits == 0
    assert snapshot.control.interaction_edits == 0
    assert control._controls.qsize() == 0


def test_drag_or_cancel_does_not_reselect() -> None:
    control = _control()
    _click(control, 20, 20)
    assert control.selection_snapshot.selected_logical_slot == 7

    control._consume_pointer_events(
        (
            _event(WindowsPointerEventKind.DOWN, 75, 75),
            _event(WindowsPointerEventKind.MOVE, 76, 75),
            _event(WindowsPointerEventKind.CANCEL, 0, 0),
        )
    )

    snapshot = control.selection_snapshot
    assert snapshot.selected_logical_slot == 7
    assert snapshot.selection_changes == 1
    assert snapshot.control.cancelled_interactions == 1


def test_selection_reconciles_when_replacement_removes_slot() -> None:
    control = _control()
    _click(control, 75, 75)
    assert control.selection_snapshot.selected_logical_slot == 4095

    control._active_layout = _layout(include_top=False)
    control._reconcile_selection()

    snapshot = control.selection_snapshot
    assert snapshot.selected_logical_slot is None
    assert snapshot.selection_changes == 2


def test_selection_survives_geometry_only_relayout() -> None:
    control = _control()
    _click(control, 20, 20)

    control._active_layout = ViewportLayout(
        placements=(
            ViewportPlacement(
                logical_slot=7,
                geometry=ViewportGeometry(x=333, y=27, width=91, height=177, z_index=9),
            ),
            ViewportPlacement(
                logical_slot=4095,
                geometry=ViewportGeometry(x=1, y=2, width=3, height=4, z_index=0),
            ),
        )
    )
    control._reconcile_selection()

    snapshot = control.selection_snapshot
    assert snapshot.selected_logical_slot == 7
    assert snapshot.selection_changes == 1


def test_selection_snapshot_is_source_and_native_identity_free() -> None:
    control = _control()
    _click(control, 75, 75)

    payload = control.selection_snapshot.model_dump_json().casefold()
    for forbidden in (
        "rtsp://",
        "credential",
        "password",
        "source_id",
        "recording_id",
        "private_path",
        "native_handle",
        "pointer_value",
        "runner_identity",
    ):
        assert forbidden not in payload
