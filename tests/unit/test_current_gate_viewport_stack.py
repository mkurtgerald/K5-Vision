from __future__ import annotations

import pytest

from k5vision.media.viewport_geometry import (
    ViewportGeometry,
    ViewportLayout,
    ViewportPlacement,
)
from k5vision.media.viewport_stack import (
    ViewportStackAction,
    ViewportStackEdit,
    ViewportStackError,
    ViewportStackErrorCode,
    apply_viewport_stack,
)
from k5vision.media.windows_operator_control import (
    WindowsOperatorControlError,
    WindowsOperatorControlErrorCode,
)
from k5vision.media.windows_operator_selection import (
    BoundedSelectableWindowsOperatorControl,
)
from k5vision.media.windows_operator_session import WindowsOperatorSessionState


def _layout() -> ViewportLayout:
    return ViewportLayout(
        placements=(
            ViewportPlacement(
                logical_slot=7,
                geometry=ViewportGeometry(
                    x=10,
                    y=20,
                    width=300,
                    height=200,
                    z_index=5,
                ),
            ),
            ViewportPlacement(
                logical_slot=4095,
                geometry=ViewportGeometry(
                    x=40,
                    y=50,
                    width=90,
                    height=80,
                    z_index=5,
                ),
            ),
            ViewportPlacement(
                logical_slot=2,
                geometry=ViewportGeometry(
                    x=5,
                    y=6,
                    width=70,
                    height=60,
                    z_index=1,
                ),
            ),
        )
    )


def _geometry_without_z(layout: ViewportLayout) -> dict[int, tuple[int, int, int, int]]:
    return {
        placement.logical_slot: (
            placement.geometry.x,
            placement.geometry.y,
            placement.geometry.width,
            placement.geometry.height,
        )
        for placement in layout.placements
    }


def test_bring_to_front_is_deterministic_for_equal_z_and_sparse_slot() -> None:
    layout = _layout()

    candidate = apply_viewport_stack(
        layout,
        ViewportStackEdit(logical_slot=7, action=ViewportStackAction.BRING_TO_FRONT),
    )

    assert tuple(item.logical_slot for item in candidate.placements) == (7, 4095, 2)
    assert _geometry_without_z(candidate) == _geometry_without_z(layout)
    assert candidate.by_slot()[7].z_index == 2
    assert candidate.by_slot()[4095].z_index == 1
    assert candidate.by_slot()[2].z_index == 0


def test_send_sparse_slot_to_back_preserves_non_z_geometry() -> None:
    layout = _layout()

    candidate = apply_viewport_stack(
        layout,
        ViewportStackEdit(
            logical_slot=4095,
            action=ViewportStackAction.SEND_TO_BACK,
        ),
    )

    assert _geometry_without_z(candidate) == _geometry_without_z(layout)
    assert candidate.by_slot()[4095].z_index == 0
    assert candidate.by_slot()[2].z_index == 1
    assert candidate.by_slot()[7].z_index == 2


def test_already_top_or_bottom_stack_request_is_noop() -> None:
    layout = _layout()

    assert (
        apply_viewport_stack(
            layout,
            ViewportStackEdit(
                logical_slot=4095,
                action=ViewportStackAction.BRING_TO_FRONT,
            ),
        )
        is layout
    )
    assert (
        apply_viewport_stack(
            layout,
            ViewportStackEdit(
                logical_slot=2,
                action=ViewportStackAction.SEND_TO_BACK,
            ),
        )
        is layout
    )


def test_missing_stack_slot_fails_without_mutation() -> None:
    layout = _layout()
    before = layout.model_dump_json()

    with pytest.raises(ViewportStackError) as exc_info:
        apply_viewport_stack(
            layout,
            ViewportStackEdit(
                logical_slot=99,
                action=ViewportStackAction.BRING_TO_FRONT,
            ),
        )

    assert exc_info.value.code == ViewportStackErrorCode.SLOT_NOT_FOUND
    assert layout.model_dump_json() == before


def test_selected_stack_queues_source_free_relayout() -> None:
    control = BoundedSelectableWindowsOperatorControl()
    control._state = WindowsOperatorSessionState.RUNNING
    control._application = object()
    control._active_layout = _layout()
    control._set_selection(7)

    snapshot = control.request_stack(ViewportStackAction.BRING_TO_FRONT)

    assert snapshot.selected_logical_slot == 7
    assert snapshot.stack_changes == 1
    assert snapshot.control.queued_controls == 1
    request = control._controls.get_nowait()
    assert request.layout is not None
    assert request.layout.by_slot()[7].z_index == 2
    assert _geometry_without_z(request.layout) == _geometry_without_z(
        control._active_layout
    )


def test_selected_stack_noop_does_not_queue_or_count() -> None:
    control = BoundedSelectableWindowsOperatorControl()
    control._state = WindowsOperatorSessionState.RUNNING
    control._application = object()
    control._active_layout = _layout()
    control._set_selection(4095)

    snapshot = control.request_stack(ViewportStackAction.BRING_TO_FRONT)

    assert snapshot.stack_changes == 0
    assert snapshot.control.queued_controls == 0


def test_stack_requires_selection() -> None:
    control = BoundedSelectableWindowsOperatorControl()
    control._state = WindowsOperatorSessionState.RUNNING
    control._application = object()
    control._active_layout = _layout()

    with pytest.raises(WindowsOperatorControlError) as exc_info:
        control.request_stack(ViewportStackAction.BRING_TO_FRONT)

    assert exc_info.value.code == WindowsOperatorControlErrorCode.INVALID_EDIT
    assert control.selection_snapshot.stack_changes == 0


def test_stack_snapshot_is_source_and_native_identity_free() -> None:
    control = BoundedSelectableWindowsOperatorControl()
    control._state = WindowsOperatorSessionState.RUNNING
    control._application = object()
    control._active_layout = _layout()
    control._set_selection(7)
    control.request_stack(ViewportStackAction.BRING_TO_FRONT)

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
        "payload",
    ):
        assert forbidden not in payload
