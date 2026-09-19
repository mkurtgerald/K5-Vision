from __future__ import annotations

import pytest
from pydantic import ValidationError

from k5vision.media.viewport_geometry import (
    ViewportGeometry,
    ViewportLayout,
    ViewportPlacement,
)


def test_layout_supports_arbitrary_non_grid_geometry() -> None:
    layout = ViewportLayout(
        placements=(
            ViewportPlacement(
                logical_slot=7,
                geometry=ViewportGeometry(x=17, y=29, width=613, height=347, z_index=2),
            ),
            ViewportPlacement(
                logical_slot=2,
                geometry=ViewportGeometry(x=701, y=41, width=211, height=719, z_index=1),
            ),
        )
    )
    assert layout.by_slot()[7].width == 613
    assert layout.by_slot()[2].height == 719


def test_layout_rejects_duplicate_logical_slots() -> None:
    placement = ViewportPlacement(
        logical_slot=1,
        geometry=ViewportGeometry(x=0, y=0, width=100, height=100),
    )
    with pytest.raises(ValidationError):
        ViewportLayout(placements=(placement, placement))


@pytest.mark.parametrize(
    "kwargs",
    (
        {"x": -1, "y": 0, "width": 1, "height": 1},
        {"x": 0, "y": -1, "width": 1, "height": 1},
        {"x": 0, "y": 0, "width": 0, "height": 1},
        {"x": 0, "y": 0, "width": 1, "height": 0},
    ),
)
def test_geometry_rejects_invalid_bounds(kwargs: dict[str, int]) -> None:
    with pytest.raises(ValidationError):
        ViewportGeometry(**kwargs)


def test_serialized_layout_is_source_and_native_identity_free() -> None:
    layout = ViewportLayout(
        placements=(
            ViewportPlacement(
                logical_slot=3,
                geometry=ViewportGeometry(x=11, y=13, width=17, height=19),
            ),
        )
    )
    payload = layout.model_dump_json().casefold()
    for forbidden in (
        "rtsp://",
        "credential",
        "password",
        "source_id",
        "recording_id",
        "handle",
        "pointer",
        "payload",
    ):
        assert forbidden not in payload
