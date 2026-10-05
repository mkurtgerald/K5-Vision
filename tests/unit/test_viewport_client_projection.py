from __future__ import annotations

import random

import pytest

from k5vision.media.viewport_client_projection import project_viewport_layout
from k5vision.media.viewport_geometry import ViewportGeometry, ViewportLayout, ViewportPlacement


def _layout(rectangles):
    return ViewportLayout(
        placements=tuple(
            ViewportPlacement(
                logical_slot=index * 7,
                geometry=ViewportGeometry(x=x, y=y, width=w, height=h, z_index=index % 3),
            )
            for index, (x, y, w, h) in enumerate(rectangles)
        )
    )


def _project(layout, width=640, height=360, top=0):
    return project_viewport_layout(
        layout,
        reference_width=1280,
        reference_height=720,
        client_width=width,
        client_height=height,
        content_top=top,
    )


def _contains(g, x, y):
    return g.x <= x < g.x + g.width and g.y <= y < g.y + g.height


def test_shared_edges_preserve_half_open_hit_ownership_and_camera_assignments():
    layout = _layout([(0, 0, 641, 720), (641, 0, 639, 720)])
    projected = _project(layout, 777, 431)
    left, right = projected.physical_layout.placements
    assert left.geometry.x + left.geometry.width == right.geometry.x
    assert right.geometry.x + right.geometry.width == 777
    for x in range(777):
        point = projected.logical_point(x, 100)
        for logical, physical in zip(
            layout.placements, projected.physical_layout.placements, strict=True
        ):
            assert logical.logical_slot == physical.logical_slot
            assert _contains(logical.geometry, *point) == _contains(physical.geometry, x, 100)


def test_distant_one_unit_tiles_remain_distinct_and_invertible():
    layout = _layout([(0, 0, 1, 1), (999_999, 999_999, 1, 1), (1_000_000, 1_000_000, 1, 1)])
    projected = _project(layout, 192, 192)
    for logical, physical in zip(
        layout.placements, projected.physical_layout.placements, strict=True
    ):
        g = physical.geometry
        assert g.width >= 1 and g.height >= 1
        assert _contains(logical.geometry, *projected.logical_point(g.x, g.y))
    xs = [p.geometry.x for p in projected.physical_layout.placements]
    assert xs == sorted(set(xs))


def test_many_tiny_tiles_and_overlaps_have_exact_boundary_ownership():
    rng = random.Random(371)
    for _ in range(20):
        rectangles = []
        for _slot in range(16):
            x, y = rng.randrange(1000), rng.randrange(600)
            rectangles.append((x, y, rng.randrange(1, 300), rng.randrange(1, 180)))
        layout = _layout(rectangles)
        p = _project(layout, 192, 232, 40)
        for physical in p.physical_layout.placements:
            g = physical.geometry
            assert g.x >= 0 and g.y >= 40 and g.x + g.width <= 192 and g.y + g.height <= 232
            for x in (g.x, g.x + g.width - 1):
                for y in (g.y, g.y + g.height - 1):
                    point = p.logical_point(x, y)
                    for logical, visible in zip(
                        layout.placements, p.physical_layout.placements, strict=True
                    ):
                        assert _contains(logical.geometry, *point) == _contains(
                            visible.geometry, x, y
                        )


def test_repeated_sizes_recompute_from_canonical_without_rounding_drift():
    layout = _layout([(17, 29, 613, 347), (701, 41, 211, 679)])
    canonical = layout.model_dump_json()
    initial = _project(layout, 913, 557)
    for _ in range(20):
        for width, height in ((192, 192), (777, 431), (1200, 850), (913, 557)):
            candidate = _project(layout, width, height)
    assert candidate == initial
    assert layout.model_dump_json() == canonical


def test_shared_projection_maps_drag_endpoint_deltas_in_canonical_units():
    layout = _layout([(0, 0, 640, 720), (640, 0, 640, 720)])
    p = _project(layout, 640, 360)
    down, up = p.logical_point(100, 100), p.logical_point(117, 109)
    assert tuple(b - a for a, b in zip(down, up, strict=True)) == (34, 18)
    # Repeated outer resize never changes the canonical result at the same client size.
    assert _project(layout, 640, 360).logical_point(117, 109) == up


def test_catalog_strip_and_outside_client_have_no_logical_hit():
    p = _project(_layout([(0, 0, 1280, 720)]), 608, 232, 40)
    assert p.logical_point(20, 39) is None
    assert p.logical_point(-1, 100) is None
    assert p.logical_point(608, 100) is None
    assert p.logical_point(20, 232) is None
    assert p.logical_point(0, 40) is not None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"client_width": 0},
        {"client_height": 0},
        {"client_width": 191},
        {"client_height": 200, "content_top": 40},
        {"client_width": 16_385},
        {"reference_width": 0},
        {"client_width": True},
        {"content_top": -1},
    ],
)
def test_invalid_or_unpaintable_geometry_is_rejected_before_projection(kwargs):
    values = dict(reference_width=1280, reference_height=720, client_width=640, client_height=360)
    values.update(kwargs)
    with pytest.raises(ValueError):
        project_viewport_layout(_layout([(0, 0, 1280, 720)]), **values)
