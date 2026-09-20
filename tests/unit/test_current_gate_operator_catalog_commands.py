from __future__ import annotations

import pytest
from pydantic import ValidationError

from k5vision.media.viewport_geometry import (
    ViewportGeometry,
    ViewportLayout,
    ViewportPlacement,
)
from k5vision.media.windows_operator_application import (
    WindowsOperatorApplicationSnapshot,
    WindowsOperatorApplicationState,
)
from k5vision.media.windows_operator_catalog_commands import (
    BoundedCommandWindowsOperatorControl,
    WindowsOperatorCatalogCommand,
    WindowsOperatorCatalogCommandKind,
)
from k5vision.media.windows_operator_control import (
    WindowsOperatorControlError,
    WindowsOperatorControlErrorCode,
)
from k5vision.media.windows_operator_session import WindowsOperatorSessionState


def _layout(offset: int = 0) -> ViewportLayout:
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
                logical_slot=4095,
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
            generation=3,
            viewport_count=2,
            open_surface_count=2,
            delivered_frames=0,
            presentations=0,
        )


def _control(*, max_pending_controls: int = 16) -> BoundedCommandWindowsOperatorControl:
    control = BoundedCommandWindowsOperatorControl(max_pending_controls=max_pending_controls)
    control._state = WindowsOperatorSessionState.RUNNING
    control._application = _FakeApplication()
    control._active_layout = _layout()
    return control


def _command(
    kind: WindowsOperatorCatalogCommandKind,
    view_id: int,
) -> WindowsOperatorCatalogCommand:
    return WindowsOperatorCatalogCommand(kind=kind, view_id=view_id)


def test_save_apply_delete_dispatch_preserves_arbitrary_geometry_and_generation() -> None:
    control = _control()
    generation = control._application.snapshot.generation

    saved = control.dispatch_catalog_command(_command(WindowsOperatorCatalogCommandKind.SAVE, 9))
    accepted_layout = control._catalog.by_id()[9]
    control._active_layout = _layout(11)
    applied = control.dispatch_catalog_command(_command(WindowsOperatorCatalogCommandKind.APPLY, 9))

    request = control._controls.get_nowait()
    assert request.layout == accepted_layout
    assert request.layout.by_slot()[4095].x == 701
    assert request.layout.by_slot()[7].z_index == 2
    assert control._active_layout == _layout(11)
    assert control._application.snapshot.generation == generation

    deleted = control.dispatch_catalog_command(
        _command(WindowsOperatorCatalogCommandKind.DELETE, 9)
    )

    assert saved.catalog_commands == 1
    assert saved.catalog_save_commands == 1
    assert applied.catalog_commands == 2
    assert applied.catalog_apply_commands == 1
    assert deleted.catalog_commands == 3
    assert deleted.catalog_delete_commands == 1
    assert deleted.authoring.selection.catalog_view_count == 0
    assert control._application.snapshot.generation == generation


def test_missing_or_invalid_dispatch_fails_closed_without_counting_success() -> None:
    control = _control()
    control.dispatch_catalog_command(_command(WindowsOperatorCatalogCommandKind.SAVE, 4))
    accepted = control._catalog
    before = control.catalog_command_snapshot

    with pytest.raises(WindowsOperatorControlError) as missing_apply:
        control.dispatch_catalog_command(_command(WindowsOperatorCatalogCommandKind.APPLY, 8))
    with pytest.raises(WindowsOperatorControlError) as missing_delete:
        control.dispatch_catalog_command(_command(WindowsOperatorCatalogCommandKind.DELETE, 8))
    with pytest.raises(WindowsOperatorControlError) as invalid_type:
        control.dispatch_catalog_command({"kind": "save", "view_id": 4})  # type: ignore[arg-type]

    assert missing_apply.value.code == WindowsOperatorControlErrorCode.INVALID_EDIT
    assert missing_delete.value.code == WindowsOperatorControlErrorCode.INVALID_EDIT
    assert invalid_type.value.code == WindowsOperatorControlErrorCode.INVALID_CONFIGURATION
    assert control._catalog == accepted
    assert control.catalog_command_snapshot == before
    assert control._controls.empty()


@pytest.mark.parametrize(
    "kwargs",
    (
        {"kind": "save", "view_id": -1},
        {"kind": "save", "view_id": 64},
        {"kind": "save", "view_id": True},
        {"kind": "unknown", "view_id": 1},
        {"kind": "save", "view_id": 1, "source_id": "forbidden"},
    ),
)
def test_command_contract_rejects_invalid_or_extra_input(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        WindowsOperatorCatalogCommand(**kwargs)


def test_queue_limit_failure_is_atomic_and_does_not_increment_command_counter() -> None:
    control = _control(max_pending_controls=1)
    control.dispatch_catalog_command(_command(WindowsOperatorCatalogCommandKind.SAVE, 2))
    control._active_layout = _layout(9)

    first = control.dispatch_catalog_command(_command(WindowsOperatorCatalogCommandKind.APPLY, 2))
    accepted_catalog = control._catalog

    with pytest.raises(WindowsOperatorControlError) as exc_info:
        control.dispatch_catalog_command(_command(WindowsOperatorCatalogCommandKind.APPLY, 2))

    assert exc_info.value.code == WindowsOperatorControlErrorCode.CONTROL_LIMIT
    assert control._catalog == accepted_catalog
    assert control._active_layout == _layout(9)
    assert control._controls.qsize() == 1
    assert first.catalog_commands == 2
    assert first.catalog_apply_commands == 1
    assert control.catalog_command_snapshot == first


def test_command_dispatch_requires_accepted_running_state() -> None:
    control = BoundedCommandWindowsOperatorControl()

    with pytest.raises(WindowsOperatorControlError) as exc_info:
        control.dispatch_catalog_command(_command(WindowsOperatorCatalogCommandKind.SAVE, 1))

    assert exc_info.value.code == WindowsOperatorControlErrorCode.INVALID_STATE
    snapshot = control.catalog_command_snapshot
    assert snapshot.catalog_commands == 0
    assert snapshot.catalog_save_commands == 0


def test_command_snapshot_is_identity_and_argument_free() -> None:
    control = _control()
    control._set_selection(4095)
    control.dispatch_catalog_command(_command(WindowsOperatorCatalogCommandKind.SAVE, 5))
    payload = control.catalog_command_snapshot.model_dump_json().casefold()

    assert '"selected_logical_slot":4095' in payload
    assert '"catalog_commands":1' in payload
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
        "camera_id",
        "stream_id",
        '"view_id"',
        '"kind"',
    ):
        assert forbidden not in payload
