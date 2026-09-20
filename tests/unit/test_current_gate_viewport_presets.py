from __future__ import annotations

import pytest

from k5vision.media.viewport_geometry import (
    ViewportGeometry,
    ViewportLayout,
    ViewportPlacement,
)
from k5vision.media.windows_operator_application import (
    WindowsOperatorApplicationSnapshot,
    WindowsOperatorApplicationState,
)
from k5vision.media.windows_operator_control import (
    WindowsOperatorControlError,
    WindowsOperatorControlErrorCode,
)
from k5vision.media.windows_operator_selection import (
    BoundedSelectableWindowsOperatorControl,
)
from k5vision.media.windows_operator_session import WindowsOperatorSessionState


def _layout(offset: int = 0, *, second_slot: int = 4095) -> ViewportLayout:
    return ViewportLayout(
        placements=(
            ViewportPlacement(
                logical_slot=7,
                geometry=ViewportGeometry(
                    x=17 + offset,
                    y=29 + offset,
                    width=613 - offset,
                    height=347 + offset,
                    z_index=2,
                ),
            ),
            ViewportPlacement(
                logical_slot=second_slot,
                geometry=ViewportGeometry(
                    x=701 - offset,
                    y=41 + offset,
                    width=211 + offset,
                    height=719 - offset,
                    z_index=1,
                ),
            ),
        )
    )


class _FakeApplication:
    @property
    def snapshot(self) -> WindowsOperatorApplicationSnapshot:
        return WindowsOperatorApplicationSnapshot(
            state=WindowsOperatorApplicationState.RUNNING,
            shell_open=True,
            pump_cycles=0,
            pumped_messages=0,
            generation=1,
            viewport_count=2,
            open_surface_count=2,
            delivered_frames=0,
            presentations=0,
        )


def _control(*, max_pending_controls: int = 16) -> BoundedSelectableWindowsOperatorControl:
    control = BoundedSelectableWindowsOperatorControl(max_pending_controls=max_pending_controls)
    control._state = WindowsOperatorSessionState.RUNNING
    control._application = _FakeApplication()
    control._active_layout = _layout()
    return control


def test_save_and_restore_preserves_exact_arbitrary_geometry() -> None:
    control = _control()
    original = control._active_layout
    assert original is not None
    control._set_selection(4095)

    saved = control.save_preset(3)
    assert saved.preset_count == 1
    assert saved.preset_saves == 1

    control._active_layout = _layout(37)
    restored = control.restore_preset(3)

    assert restored.preset_restores == 1
    assert restored.selected_logical_slot == 4095
    request = control._controls.get_nowait()
    assert request.layout == original
    assert tuple(item.logical_slot for item in request.layout.placements) == (7, 4095)


def test_save_overwrites_same_preset_without_growing_capacity() -> None:
    control = _control()
    control.save_preset(1)
    replacement = _layout(23)
    control._active_layout = replacement

    snapshot = control.save_preset(1)
    assert snapshot.preset_count == 1
    assert snapshot.preset_saves == 2

    control._active_layout = _layout(9)
    control.restore_preset(1)
    request = control._controls.get_nowait()
    assert request.layout == replacement


@pytest.mark.parametrize("preset_slot", (-1, 16, True, "1"))
def test_invalid_preset_slot_fails_closed(preset_slot: object) -> None:
    control = _control()

    with pytest.raises(WindowsOperatorControlError) as exc_info:
        control.save_preset(preset_slot)  # type: ignore[arg-type]

    assert exc_info.value.code == WindowsOperatorControlErrorCode.INVALID_CONFIGURATION
    assert control.selection_snapshot.preset_count == 0


def test_missing_preset_does_not_mutate_active_layout() -> None:
    control = _control()
    before = control._active_layout

    with pytest.raises(WindowsOperatorControlError) as exc_info:
        control.restore_preset(4)

    assert exc_info.value.code == WindowsOperatorControlErrorCode.INVALID_EDIT
    assert control._active_layout == before
    assert control.selection_snapshot.preset_restores == 0


def test_incompatible_slot_set_fails_without_losing_saved_preset() -> None:
    control = _control()
    control.save_preset(2)
    control._active_layout = _layout(10, second_slot=2)
    before = control._active_layout

    with pytest.raises(WindowsOperatorControlError) as exc_info:
        control.restore_preset(2)

    assert exc_info.value.code == WindowsOperatorControlErrorCode.INVALID_EDIT
    assert control._active_layout == before
    assert control.selection_snapshot.preset_count == 1
    assert control.selection_snapshot.preset_restores == 0


def test_restore_queue_saturation_preserves_active_and_preset() -> None:
    control = _control(max_pending_controls=1)
    control.save_preset(0)
    saved_layout = control._active_layout
    control._active_layout = _layout(31)
    before = control._active_layout
    control.request_relayout(_layout(12))

    with pytest.raises(WindowsOperatorControlError) as exc_info:
        control.restore_preset(0)

    assert exc_info.value.code == WindowsOperatorControlErrorCode.CONTROL_LIMIT
    assert control._active_layout == before
    assert control.selection_snapshot.preset_count == 1
    assert control.selection_snapshot.preset_restores == 0
    assert saved_layout is not None


def test_save_and_restore_require_running_state() -> None:
    control = BoundedSelectableWindowsOperatorControl()

    with pytest.raises(WindowsOperatorControlError) as save_error:
        control.save_preset(0)
    with pytest.raises(WindowsOperatorControlError) as restore_error:
        control.restore_preset(0)

    assert save_error.value.code == WindowsOperatorControlErrorCode.INVALID_STATE
    assert restore_error.value.code == WindowsOperatorControlErrorCode.INVALID_STATE


def test_preset_snapshot_is_source_and_native_identity_free() -> None:
    control = _control()
    control.save_preset(5)
    payload = control.selection_snapshot.model_dump_json().casefold()

    assert '"preset_count":1' in payload
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
        "preset_name",
    ):
        assert forbidden not in payload
