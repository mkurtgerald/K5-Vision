"""Bounded source-free viewport edits above the accepted arbitrary layout contract."""

from __future__ import annotations

import enum
import typing

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from k5vision.media.viewport_geometry import ViewportGeometry, ViewportLayout, ViewportPlacement

_MAX_EDIT_DELTA = 1_000_000
_MAX_OPERATIONS = 1_000_000


class ViewportEditorErrorCode(enum.StrEnum):
    INVALID_CONFIGURATION = "invalid_configuration"
    INVALID_EDIT = "invalid_edit"
    SLOT_NOT_FOUND = "slot_not_found"
    OPERATION_LIMIT = "operation_limit"


class ViewportEditorError(RuntimeError):
    """Sanitized edit failure without source, payload, or native identity."""

    def __init__(self, code: ViewportEditorErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class ViewportMove(BaseModel):
    """Relative move for one logical slot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: typing.Literal["move"] = "move"
    logical_slot: int = Field(ge=0, le=4095)
    dx: int = Field(ge=-_MAX_EDIT_DELTA, le=_MAX_EDIT_DELTA)
    dy: int = Field(ge=-_MAX_EDIT_DELTA, le=_MAX_EDIT_DELTA)


class ViewportResize(BaseModel):
    """Relative resize for one logical slot while preserving position and z-order."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: typing.Literal["resize"] = "resize"
    logical_slot: int = Field(ge=0, le=4095)
    dwidth: int = Field(ge=-_MAX_EDIT_DELTA, le=_MAX_EDIT_DELTA)
    dheight: int = Field(ge=-_MAX_EDIT_DELTA, le=_MAX_EDIT_DELTA)


ViewportEdit = ViewportMove | ViewportResize


class ViewportEditorSnapshot(BaseModel):
    """Source-free retained editor state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: typing.Literal["1"] = "1"
    layout: ViewportLayout
    operations: int = Field(ge=0, le=_MAX_OPERATIONS)


def apply_viewport_edit(layout: ViewportLayout, edit: ViewportEdit) -> ViewportLayout:
    """Apply one validated relative edit while preserving every untouched placement."""
    if not isinstance(layout, ViewportLayout) or not isinstance(
        edit, (ViewportMove, ViewportResize)
    ):
        raise ViewportEditorError(
            ViewportEditorErrorCode.INVALID_CONFIGURATION,
            "viewport edit input is invalid",
        )

    selected: ViewportPlacement | None = None
    for placement in layout.placements:
        if placement.logical_slot == edit.logical_slot:
            selected = placement
            break
    if selected is None:
        raise ViewportEditorError(
            ViewportEditorErrorCode.SLOT_NOT_FOUND,
            "viewport edit logical slot is not active",
        )

    current = selected.geometry
    try:
        if isinstance(edit, ViewportMove):
            geometry = ViewportGeometry(
                x=current.x + edit.dx,
                y=current.y + edit.dy,
                width=current.width,
                height=current.height,
                z_index=current.z_index,
            )
        else:
            geometry = ViewportGeometry(
                x=current.x,
                y=current.y,
                width=current.width + edit.dwidth,
                height=current.height + edit.dheight,
                z_index=current.z_index,
            )
    except ValidationError:
        raise ViewportEditorError(
            ViewportEditorErrorCode.INVALID_EDIT,
            "viewport edit geometry is invalid",
        ) from None

    placements = tuple(
        ViewportPlacement(logical_slot=item.logical_slot, geometry=geometry)
        if item.logical_slot == edit.logical_slot
        else item
        for item in layout.placements
    )
    return ViewportLayout(placements=placements)


class BoundedViewportEditor:
    """Apply a bounded sequence of source-free viewport edits."""

    def __init__(self, layout: ViewportLayout, *, max_operations: int = 100_000) -> None:
        if not isinstance(layout, ViewportLayout) or not 1 <= max_operations <= _MAX_OPERATIONS:
            raise ViewportEditorError(
                ViewportEditorErrorCode.INVALID_CONFIGURATION,
                "viewport editor configuration is invalid",
            )
        self._layout = layout
        self._operations = 0
        self._max_operations = max_operations

    @property
    def snapshot(self) -> ViewportEditorSnapshot:
        return ViewportEditorSnapshot(layout=self._layout, operations=self._operations)

    def apply(self, edit: ViewportEdit) -> ViewportEditorSnapshot:
        if self._operations >= self._max_operations:
            raise ViewportEditorError(
                ViewportEditorErrorCode.OPERATION_LIMIT,
                "viewport editor operation limit reached",
            )
        candidate = apply_viewport_edit(self._layout, edit)
        self._layout = candidate
        self._operations += 1
        return self.snapshot
