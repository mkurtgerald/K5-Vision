"""The generated source changes only the intended region without RTSP switches."""

from __future__ import annotations

import runpy
from pathlib import Path

import pytest

_FIXTURE = runpy.run_path(str(Path(__file__).parents[1] / "integration/motion_camera_fixture.py"))


@pytest.mark.parametrize("size", [(320, 240), (640, 360)])
def test_stationary_source_is_exactly_stable_at_both_real_geometries(size):
    width, height = size
    first = _FIXTURE["generated_frame"]("idle", 0, width=width, height=height)
    assert first == _FIXTURE["generated_frame"]("idle", 19, width=width, height=height)
    assert len(first) == width * height * 3


@pytest.mark.parametrize(
    "mode,bounds", [("included", (0.02, 0.49)), ("excluded", (0.49, 0.8)), ("outside", (0.8, 1))]
)
def test_active_pixels_stay_inside_the_designated_motion_zone(mode, bounds):
    first, second = (_FIXTURE["generated_frame"](mode, n) for n in (0, 10))
    different = [
        i // 3 % 320 / 320 for i in range(0, len(first), 3) if first[i : i + 3] != second[i : i + 3]
    ]
    assert different and all(bounds[0] < x < bounds[1] for x in different)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"mode": "PRIVATE", "index": 0},
        {"mode": "idle", "index": -1},
        {"mode": "idle", "index": True},
        {"mode": "idle", "index": 0, "width": 9999},
    ],
)
def test_fixture_refuses_unbounded_or_unrecognized_inputs(kwargs):
    with pytest.raises(ValueError):
        _FIXTURE["generated_frame"](**kwargs)
