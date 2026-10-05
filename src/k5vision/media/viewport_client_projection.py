"""Bounded, reversible client projection of canonical source-free viewport geometry.

Logical editor/history/catalog coordinates are never rewritten. Shared logical
edges receive shared strictly ordered client edges, so tiny tiles do not overlap
through independent width rounding. Video aspect fitting remains surface-owned.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass

from k5vision.media.viewport_geometry import ViewportGeometry, ViewportLayout, ViewportPlacement

_MAX_CLIENT = 16_384
_MAX_LOGICAL = 2_000_000
_MIN_CONTENT = 192  # At most 129 distinct edge intervals for 64 placements.


@dataclass(frozen=True, slots=True)
class _AxisProjection:
    logical: tuple[int, ...]
    physical: tuple[int, ...]

    @classmethod
    def build(cls, edges: set[int], extent: int, pixels: int, offset: int) -> _AxisProjection:
        logical = tuple(sorted({0, extent, *edges}))
        if pixels < len(logical) - 1:
            raise ValueError("client projection has insufficient edge intervals")
        physical = [offset]
        for index, edge in enumerate(logical[1:-1], 1):
            remaining = len(logical) - index - 1
            selected = offset + edge * pixels // extent
            physical.append(min(offset + pixels - remaining, max(physical[-1] + 1, selected)))
        physical.append(offset + pixels)
        return cls(logical, tuple(physical))

    def edge(self, value: int) -> int:
        index = bisect_right(self.logical, value) - 1
        if index < 0 or self.logical[index] != value:
            raise ValueError("logical edge is not part of this projection")
        return self.physical[index]

    def inverse(self, pixel: int) -> int:
        index = bisect_right(self.physical, pixel) - 1
        if index < 0 or index >= len(self.physical) - 1:
            raise ValueError("point is outside the client content")
        logical_start, logical_end = self.logical[index : index + 2]
        physical_start, physical_end = self.physical[index : index + 2]
        # Use the last logical integer covered by this physical pixel. This keeps
        # half-open hit ownership exact at every shared projected placement edge.
        numerator = (pixel - physical_start + 1) * (logical_end - logical_start)
        denominator = physical_end - physical_start
        return logical_start + (numerator + denominator - 1) // denominator - 1


@dataclass(frozen=True, slots=True)
class ViewportClientProjection:
    physical_layout: ViewportLayout
    horizontal: _AxisProjection
    vertical: _AxisProjection
    client_width: int
    client_height: int
    content_top: int

    def logical_point(self, x: int, y: int) -> tuple[int, int] | None:
        if type(x) is not int or type(y) is not int:
            raise ValueError("client pointer coordinates must be integers")
        if not (0 <= x < self.client_width and self.content_top <= y < self.client_height):
            return None
        return self.horizontal.inverse(x), self.vertical.inverse(y)


def project_viewport_layout(
    layout: ViewportLayout,
    *,
    reference_width: int,
    reference_height: int,
    client_width: int,
    client_height: int,
    content_top: int = 0,
) -> ViewportClientProjection:
    """Fit canonical layout into a current positive client; never chain projections."""
    if not isinstance(layout, ViewportLayout):
        raise ValueError("client projection layout is invalid")
    if any(
        type(value) is not int
        for value in (
            reference_width,
            reference_height,
            client_width,
            client_height,
            content_top,
        )
    ) or not (
        1 <= reference_width <= _MAX_CLIENT
        and 1 <= reference_height <= _MAX_CLIENT
        and _MIN_CONTENT <= client_width <= _MAX_CLIENT
        and 0 <= content_top <= _MAX_CLIENT - _MIN_CONTENT
        and _MIN_CONTENT <= client_height - content_top <= _MAX_CLIENT
        and client_height <= _MAX_CLIENT
    ):
        raise ValueError("client projection geometry is invalid")
    right = max(p.geometry.x + p.geometry.width for p in layout.placements)
    bottom = max(p.geometry.y + p.geometry.height for p in layout.placements)
    width, height = max(reference_width, right), max(reference_height, bottom)
    if max(width, height) > _MAX_LOGICAL:
        raise ValueError("client projection logical extent exceeds bound")
    x_edges = {
        edge for p in layout.placements for edge in (p.geometry.x, p.geometry.x + p.geometry.width)
    }
    y_edges = {
        edge for p in layout.placements for edge in (p.geometry.y, p.geometry.y + p.geometry.height)
    }
    horizontal = _AxisProjection.build(x_edges, width, client_width, 0)
    vertical = _AxisProjection.build(y_edges, height, client_height - content_top, content_top)
    projected = []
    for placement in layout.placements:
        g = placement.geometry
        x, y = horizontal.edge(g.x), vertical.edge(g.y)
        projected.append(
            ViewportPlacement(
                logical_slot=placement.logical_slot,
                geometry=ViewportGeometry(
                    x=x,
                    y=y,
                    width=horizontal.edge(g.x + g.width) - x,
                    height=vertical.edge(g.y + g.height) - y,
                    z_index=g.z_index,
                ),
            )
        )
    return ViewportClientProjection(
        ViewportLayout(placements=tuple(projected)),
        horizontal,
        vertical,
        client_width,
        client_height,
        content_top,
    )
