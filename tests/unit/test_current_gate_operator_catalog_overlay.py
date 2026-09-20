from __future__ import annotations

import ctypes

import pytest

from k5vision.media.viewport_geometry import (
    ViewportGeometry,
    ViewportLayout,
    ViewportPlacement,
)
from k5vision.media.windows_operator_application import (
    _NativeShellError,
    _NativeShellFailure,
)
from k5vision.media.windows_operator_catalog_overlay import (
    _MAX_CATALOG_Z_ORDER_CONTROLS,
    _Z_ORDER_FLAGS,
    BoundedOverlayCatalogUiWindowsOperatorControl,
    _OverlayCatalogWin32OperatorShellApi,
)
from k5vision.media.windows_operator_catalog_ui import _CatalogWin32OperatorShellApi


def _overlapping_layout() -> ViewportLayout:
    return ViewportLayout(
        placements=(
            ViewportPlacement(
                logical_slot=7,
                geometry=ViewportGeometry(x=0, y=0, width=640, height=360, z_index=2),
            ),
            ViewportPlacement(
                logical_slot=4095,
                geometry=ViewportGeometry(x=211, y=9, width=777, height=503, z_index=1),
            ),
        )
    )


def test_catalog_controls_are_raised_in_bounded_creation_order() -> None:
    api = object.__new__(_OverlayCatalogWin32OperatorShellApi)
    api._catalog_z_order_handles = [101, 202, 303]
    calls: list[tuple[int, int]] = []

    def _set_window_pos(
        handle: ctypes.c_void_p,
        _insert_after: ctypes.c_void_p,
        _x: int,
        _y: int,
        _cx: int,
        _cy: int,
        flags: int,
    ) -> int:
        calls.append((int(handle.value or 0), flags))
        return 1

    api._set_window_pos = _set_window_pos
    api._raise_catalog_controls()

    assert calls == [(101, _Z_ORDER_FLAGS), (202, _Z_ORDER_FLAGS), (303, _Z_ORDER_FLAGS)]


def test_catalog_z_order_refresh_fails_closed_and_sanitized() -> None:
    api = object.__new__(_OverlayCatalogWin32OperatorShellApi)
    api._catalog_z_order_handles = [101]
    api._set_window_pos = lambda *_args: 0

    with pytest.raises(_NativeShellError) as failed:
        api._raise_catalog_controls()
    assert failed.value.failure == _NativeShellFailure.PUMP

    def _explode(*_args: object) -> int:
        raise RuntimeError("native detail")

    api._set_window_pos = _explode
    with pytest.raises(_NativeShellError) as exploded:
        api._raise_catalog_controls()
    assert exploded.value.failure == _NativeShellFailure.PUMP
    assert "native detail" not in str(exploded.value)


def test_catalog_z_order_refresh_is_strictly_bounded() -> None:
    api = object.__new__(_OverlayCatalogWin32OperatorShellApi)
    api._catalog_z_order_handles = list(range(_MAX_CATALOG_Z_ORDER_CONTROLS + 1))
    api._set_window_pos = lambda *_args: 1

    with pytest.raises(_NativeShellError) as failed:
        api._raise_catalog_controls()
    assert failed.value.failure == _NativeShellFailure.PUMP


def test_child_creation_records_only_ephemeral_native_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = object.__new__(_OverlayCatalogWin32OperatorShellApi)
    api._catalog_z_order_handles = []

    handles = iter((707, 808))
    monkeypatch.setattr(
        _CatalogWin32OperatorShellApi,
        "_create_child",
        lambda _self, **_kwargs: next(handles),
    )

    first = api._create_child(shell=1)
    second = api._create_child(shell=1)

    assert (first, second) == (707, 808)
    assert api._catalog_z_order_handles == [707, 808]


def test_message_pump_refreshes_z_order_before_processing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    api = object.__new__(_OverlayCatalogWin32OperatorShellApi)
    order: list[str] = []
    api._raise_catalog_controls = lambda: order.append("raise")

    monkeypatch.setattr(
        _CatalogWin32OperatorShellApi,
        "pump_messages",
        lambda _self, _shell, _max_messages: (order.append("pump") or (0, False)),
    )

    assert api.pump_messages(1, 16) == (0, False)
    assert order == ["raise", "pump"]


def test_overlap_guard_never_rewrites_arbitrary_viewport_geometry() -> None:
    layout = _overlapping_layout()
    before = layout.model_dump_json()

    api = object.__new__(_OverlayCatalogWin32OperatorShellApi)
    api._catalog_z_order_handles = [101, 202]
    api._set_window_pos = lambda *_args: 1
    api._raise_catalog_controls()

    assert layout.model_dump_json() == before
    assert layout.by_slot()[7].x == 0
    assert layout.by_slot()[7].z_index == 2
    assert layout.by_slot()[4095].y == 9


def test_overlay_control_retains_no_native_z_order_identity() -> None:
    control = BoundedOverlayCatalogUiWindowsOperatorControl()
    payload = control.catalog_ui_snapshot.model_dump_json().casefold()

    for forbidden in (
        "native_handle",
        "pointer_value",
        "runner_identity",
        "setwindowpos",
        "hwnd",
        "rtsp://",
        "credential",
        "password",
        "source_id",
        "camera_id",
        "private_path",
    ):
        assert forbidden not in payload
