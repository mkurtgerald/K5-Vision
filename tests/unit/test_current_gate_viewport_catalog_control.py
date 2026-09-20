from __future__ import annotations

import pytest

from k5vision.media.viewport_catalog import (
    ViewportCatalogEntry,
    build_viewport_catalog,
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


def _payload(*entries: ViewportCatalogEntry) -> bytes:
    return serialize_viewport_catalog(build_viewport_catalog(entries))


def test_install_and_apply_preserves_exact_sparse_non_grid_geometry() -> None:
    control = _control()
    target = _layout(11)
    control._set_selection(4095)

    installed = control.install_catalog(
        _payload(
            ViewportCatalogEntry(view_id=9, layout=target),
            ViewportCatalogEntry(view_id=1, layout=_layout(5)),
        )
    )
    applied = control.apply_catalog_view(9)
    request = control._controls.get_nowait()

    assert installed.catalog_view_count == 2
    assert installed.catalog_installs == 1
    assert applied.catalog_applies == 1
    assert applied.selected_logical_slot == 4095
    assert request.kind.value == "relayout"
    assert request.layout == target
    assert request.layout.by_slot()[4095].x == 690
    assert request.layout.by_slot()[7].z_index == 2
    assert control._application.snapshot.generation == 1


def test_catalog_replacement_is_atomic_when_new_catalog_is_incompatible() -> None:
    control = _control()
    control.install_catalog(_payload(ViewportCatalogEntry(view_id=1, layout=_layout(4))))
    accepted = control._catalog

    with pytest.raises(WindowsOperatorControlError) as exc_info:
        control.install_catalog(
            _payload(ViewportCatalogEntry(view_id=2, layout=_layout(6, second_slot=2)))
        )

    assert exc_info.value.code == WindowsOperatorControlErrorCode.INVALID_EDIT
    assert control._catalog == accepted
    assert control.selection_snapshot.catalog_view_count == 1
    assert control.selection_snapshot.catalog_installs == 1


@pytest.mark.parametrize("payload", (b"{not-json", b"x" * 262_145))
def test_invalid_catalog_payload_does_not_replace_current_catalog(payload: bytes) -> None:
    control = _control()
    control.install_catalog(_payload(ViewportCatalogEntry(view_id=1, layout=_layout(4))))
    accepted = control._catalog

    with pytest.raises(WindowsOperatorControlError) as exc_info:
        control.install_catalog(payload)

    assert exc_info.value.code == WindowsOperatorControlErrorCode.INVALID_CONFIGURATION
    assert control._catalog == accepted
    assert control.selection_snapshot.catalog_installs == 1


def test_missing_and_invalid_catalog_view_identifiers_fail_closed() -> None:
    control = _control()
    control.install_catalog(_payload(ViewportCatalogEntry(view_id=1, layout=_layout(4))))

    with pytest.raises(WindowsOperatorControlError) as missing_error:
        control.apply_catalog_view(2)
    with pytest.raises(WindowsOperatorControlError) as invalid_error:
        control.apply_catalog_view(True)

    assert missing_error.value.code == WindowsOperatorControlErrorCode.INVALID_EDIT
    assert invalid_error.value.code == WindowsOperatorControlErrorCode.INVALID_CONFIGURATION
    assert control.selection_snapshot.catalog_applies == 0
    assert control._controls.empty()


def test_catalog_apply_queue_limit_preserves_active_layout_and_counter() -> None:
    control = _control(max_pending_controls=1)
    control.install_catalog(_payload(ViewportCatalogEntry(view_id=3, layout=_layout(8))))
    before = control._active_layout
    control.request_relayout(_layout(2))

    with pytest.raises(WindowsOperatorControlError) as exc_info:
        control.apply_catalog_view(3)

    assert exc_info.value.code == WindowsOperatorControlErrorCode.CONTROL_LIMIT
    assert control._active_layout == before
    assert control.selection_snapshot.catalog_applies == 0


def test_catalog_apply_noop_does_not_queue_or_increment() -> None:
    control = _control()
    control.install_catalog(_payload(ViewportCatalogEntry(view_id=4, layout=_layout())))

    snapshot = control.apply_catalog_view(4)

    assert snapshot.catalog_applies == 0
    assert control._controls.empty()


def test_catalog_operations_require_running_operator_state() -> None:
    control = BoundedSelectableWindowsOperatorControl()
    payload = _payload(ViewportCatalogEntry(view_id=1, layout=_layout()))

    with pytest.raises(WindowsOperatorControlError) as install_error:
        control.install_catalog(payload)
    with pytest.raises(WindowsOperatorControlError) as apply_error:
        control.apply_catalog_view(1)

    assert install_error.value.code == WindowsOperatorControlErrorCode.INVALID_STATE
    assert apply_error.value.code == WindowsOperatorControlErrorCode.INVALID_STATE


def test_catalog_snapshot_is_source_and_native_identity_free() -> None:
    control = _control()
    control.install_catalog(_payload(ViewportCatalogEntry(view_id=5, layout=_layout(3))))
    control.apply_catalog_view(5)
    payload = control.selection_snapshot.model_dump_json().casefold()

    assert '"catalog_view_count":1' in payload
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
