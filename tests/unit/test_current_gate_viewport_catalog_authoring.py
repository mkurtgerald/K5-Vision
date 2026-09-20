from __future__ import annotations

import pytest

from k5vision.media.viewport_catalog import (
    ViewportCatalogEntry,
    build_viewport_catalog,
    parse_viewport_catalog,
    serialize_viewport_catalog,
)
from k5vision.media.viewport_geometry import (
    ViewportGeometry,
    ViewportLayout,
    ViewportPlacement,
)
from k5vision.media.windows_operator_application import (
    WindowsOperatorApplicationSnapshot,
    WindowsOperatorApplicationState,
)
from k5vision.media.windows_operator_catalog_authoring import (
    BoundedAuthoringWindowsOperatorControl,
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


def _control() -> BoundedAuthoringWindowsOperatorControl:
    control = BoundedAuthoringWindowsOperatorControl()
    control._state = WindowsOperatorSessionState.RUNNING
    control._application = _FakeApplication()
    control._active_layout = _layout()
    return control


def test_save_replace_and_export_round_trip_preserves_exact_geometry() -> None:
    control = _control()

    first = control.save_catalog_view(9)
    control._active_layout = _layout(11)
    second = control.save_catalog_view(9)
    payload = control.export_catalog()
    restored = parse_viewport_catalog(payload)

    assert first.selection.catalog_view_count == 1
    assert first.catalog_saves == 1
    assert second.selection.catalog_view_count == 1
    assert second.catalog_saves == 2
    assert restored.by_id()[9] == _layout(11)
    assert restored.by_id()[9].by_slot()[4095].x == 690
    assert restored.by_id()[9].by_slot()[7].z_index == 2
    assert control.catalog_authoring_snapshot.catalog_exports == 1


def test_save_preserves_existing_catalog_entries_and_canonical_order() -> None:
    control = _control()
    initial = build_viewport_catalog(
        (
            ViewportCatalogEntry(view_id=8, layout=_layout(8)),
            ViewportCatalogEntry(view_id=2, layout=_layout(2)),
        )
    )
    control.install_catalog(serialize_viewport_catalog(initial))
    control._active_layout = _layout(5)

    control.save_catalog_view(4)
    restored = parse_viewport_catalog(control.export_catalog())

    assert tuple(entry.view_id for entry in restored.views) == (2, 4, 8)
    assert restored.by_id()[2] == _layout(2)
    assert restored.by_id()[4] == _layout(5)
    assert restored.by_id()[8] == _layout(8)


def test_delete_is_atomic_and_missing_delete_fails_closed() -> None:
    control = _control()
    control.save_catalog_view(3)
    control._active_layout = _layout(4)
    control.save_catalog_view(6)

    deleted = control.delete_catalog_view(3)
    accepted = control._catalog

    with pytest.raises(WindowsOperatorControlError) as exc_info:
        control.delete_catalog_view(3)

    assert deleted.selection.catalog_view_count == 1
    assert deleted.catalog_deletes == 1
    assert control._catalog == accepted
    assert control._catalog.by_id()[6] == _layout(4)
    assert exc_info.value.code == WindowsOperatorControlErrorCode.INVALID_EDIT


@pytest.mark.parametrize("view_id", (-1, 64, True))
def test_invalid_view_identifier_never_mutates_catalog(view_id: int) -> None:
    control = _control()
    control.save_catalog_view(1)
    accepted = control._catalog

    with pytest.raises(WindowsOperatorControlError) as save_error:
        control.save_catalog_view(view_id)
    with pytest.raises(WindowsOperatorControlError) as delete_error:
        control.delete_catalog_view(view_id)

    assert save_error.value.code == WindowsOperatorControlErrorCode.INVALID_CONFIGURATION
    assert delete_error.value.code == WindowsOperatorControlErrorCode.INVALID_CONFIGURATION
    assert control._catalog == accepted
    assert control.catalog_authoring_snapshot.catalog_saves == 1
    assert control.catalog_authoring_snapshot.catalog_deletes == 0


def test_authoring_operations_require_running_operator_state() -> None:
    control = BoundedAuthoringWindowsOperatorControl()

    with pytest.raises(WindowsOperatorControlError) as save_error:
        control.save_catalog_view(1)
    with pytest.raises(WindowsOperatorControlError) as delete_error:
        control.delete_catalog_view(1)
    with pytest.raises(WindowsOperatorControlError) as export_error:
        control.export_catalog()

    assert save_error.value.code == WindowsOperatorControlErrorCode.INVALID_STATE
    assert delete_error.value.code == WindowsOperatorControlErrorCode.INVALID_STATE
    assert export_error.value.code == WindowsOperatorControlErrorCode.INVALID_STATE


def test_authoring_does_not_restart_generation_or_enqueue_control() -> None:
    control = _control()
    generation = control._application.snapshot.generation

    control.save_catalog_view(5)
    control.export_catalog()
    control.delete_catalog_view(5)

    assert control._application.snapshot.generation == generation
    assert control._controls.empty()
    snapshot = control.catalog_authoring_snapshot
    assert snapshot.catalog_saves == 1
    assert snapshot.catalog_deletes == 1
    assert snapshot.catalog_exports == 1


def test_exported_catalog_and_authoring_snapshot_are_identity_free() -> None:
    control = _control()
    control._set_selection(4095)
    control.save_catalog_view(5)
    catalog_payload = control.export_catalog().decode("utf-8").casefold()
    snapshot_payload = control.catalog_authoring_snapshot.model_dump_json().casefold()

    assert '"catalog_view_count":1' in snapshot_payload
    assert '"selected_logical_slot":4095' in snapshot_payload
    for payload in (catalog_payload, snapshot_payload):
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
        ):
            assert forbidden not in payload
