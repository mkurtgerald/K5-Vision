from __future__ import annotations

import json

import pytest

from k5vision.media.viewport_catalog import (
    ViewportCatalogEntry,
    ViewportCatalogError,
    ViewportCatalogErrorCode,
    build_viewport_catalog,
    catalog_view_for_active_layout,
    parse_viewport_catalog,
    serialize_viewport_catalog,
)
from k5vision.media.viewport_geometry import (
    ViewportGeometry,
    ViewportLayout,
    ViewportPlacement,
)


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


def test_catalog_round_trip_is_canonical_and_preserves_exact_geometry() -> None:
    catalog = build_viewport_catalog(
        (
            ViewportCatalogEntry(view_id=9, layout=_layout(9)),
            ViewportCatalogEntry(view_id=1, layout=_layout()),
        )
    )

    first = serialize_viewport_catalog(catalog)
    restored = parse_viewport_catalog(first)
    second = serialize_viewport_catalog(restored)

    assert first == second
    assert tuple(entry.view_id for entry in restored.views) == (1, 9)
    assert restored.by_id()[1] == _layout()
    assert restored.by_id()[1].by_slot()[4095].height == 719


def test_duplicate_view_identifier_is_rejected() -> None:
    payload = json.dumps(
        {
            "schema_version": "1",
            "views": [
                {
                    "schema_version": "1",
                    "view_id": 3,
                    "layout": _layout().model_dump(mode="json"),
                },
                {
                    "schema_version": "1",
                    "view_id": 3,
                    "layout": _layout(4).model_dump(mode="json"),
                },
            ],
        }
    )

    with pytest.raises(ViewportCatalogError) as exc_info:
        parse_viewport_catalog(payload)

    assert exc_info.value.code == ViewportCatalogErrorCode.INVALID_PAYLOAD


def test_unsupported_schema_and_malformed_payload_fail_closed() -> None:
    unsupported = json.dumps({"schema_version": "2", "views": []})

    with pytest.raises(ViewportCatalogError) as version_error:
        parse_viewport_catalog(unsupported)
    with pytest.raises(ViewportCatalogError) as malformed_error:
        parse_viewport_catalog("{not-json")

    assert version_error.value.code == ViewportCatalogErrorCode.INVALID_PAYLOAD
    assert malformed_error.value.code == ViewportCatalogErrorCode.INVALID_PAYLOAD


def test_oversized_payload_is_rejected_before_parsing() -> None:
    with pytest.raises(ViewportCatalogError) as exc_info:
        parse_viewport_catalog(b"x" * 262_145)

    assert exc_info.value.code == ViewportCatalogErrorCode.PAYLOAD_LIMIT


def test_catalog_capacity_is_bounded() -> None:
    entry = ViewportCatalogEntry(view_id=0, layout=_layout())

    with pytest.raises(ViewportCatalogError) as exc_info:
        build_viewport_catalog([entry] * 65)

    assert exc_info.value.code == ViewportCatalogErrorCode.INVALID_CONFIGURATION


def test_restored_view_requires_same_active_logical_slot_set() -> None:
    catalog = build_viewport_catalog((ViewportCatalogEntry(view_id=4, layout=_layout()),))
    incompatible = _layout(2, second_slot=2)

    with pytest.raises(ViewportCatalogError) as exc_info:
        catalog_view_for_active_layout(catalog, 4, incompatible)

    assert exc_info.value.code == ViewportCatalogErrorCode.INCOMPATIBLE_LAYOUT


def test_restored_view_returns_exact_non_grid_layout_for_sparse_slot() -> None:
    expected = _layout(11)
    catalog = build_viewport_catalog((ViewportCatalogEntry(view_id=6, layout=expected),))

    restored = catalog_view_for_active_layout(catalog, 6, _layout())

    assert restored == expected
    assert restored.by_slot()[4095].x == 690
    assert restored.by_slot()[7].z_index == 2


def test_missing_view_and_invalid_identifier_fail_without_fallback() -> None:
    catalog = build_viewport_catalog((ViewportCatalogEntry(view_id=1, layout=_layout()),))

    with pytest.raises(ViewportCatalogError) as missing_error:
        catalog_view_for_active_layout(catalog, 2, _layout())
    with pytest.raises(ViewportCatalogError) as invalid_error:
        catalog_view_for_active_layout(catalog, True, _layout())

    assert missing_error.value.code == ViewportCatalogErrorCode.VIEW_NOT_FOUND
    assert invalid_error.value.code == ViewportCatalogErrorCode.INVALID_CONFIGURATION


def test_serialized_catalog_is_source_and_native_identity_free() -> None:
    catalog = build_viewport_catalog((ViewportCatalogEntry(view_id=5, layout=_layout()),))
    payload = serialize_viewport_catalog(catalog).decode("utf-8").casefold()

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
