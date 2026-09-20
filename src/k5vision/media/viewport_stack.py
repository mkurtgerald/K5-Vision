"""Deterministic source-free viewport stack ordering for arbitrary layouts."""

from __future__ import annotations

import enum

from pydantic import BaseModel, ConfigDict, Field

from k5vision.media.viewport_geometry import ViewportLayout, ViewportPlacement


class ViewportStackAction(enum.StrEnum):
    BRING_TO_FRONT = "bring_to_front"
    SEND_TO_BACK = "send_to_back"


class ViewportStackErrorCode(enum.StrEnum):
    INVALID_CONFIGURATION = "invalid_configuration"
    SLOT_NOT_FOUND = "slot_not_found"


class ViewportStackError(ValueError):
    """Sanitized source-free stack edit failure."""

    def __init__(self, code: ViewportStackErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class ViewportStackEdit(BaseModel):
    """One bounded stack-order edit against a logical viewport slot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "1"
    logical_slot: int = Field(ge=0, le=4095)
    action: ViewportStackAction


def _stack_order(layout: ViewportLayout) -> list[int]:
    return sorted(
        range(len(layout.placements)),
        key=lambda index: (layout.placements[index].geometry.z_index, index),
    )


def apply_viewport_stack(layout: ViewportLayout, edit: ViewportStackEdit) -> ViewportLayout:
    """Move one logical viewport to the front/back while preserving its geometry."""
    if not isinstance(layout, ViewportLayout) or not isinstance(edit, ViewportStackEdit):
        raise ViewportStackError(
            ViewportStackErrorCode.INVALID_CONFIGURATION,
            "viewport stack request is invalid",
        )

    target_index = next(
        (
            index
            for index, placement in enumerate(layout.placements)
            if placement.logical_slot == edit.logical_slot
        ),
        None,
    )
    if target_index is None:
        raise ViewportStackError(
            ViewportStackErrorCode.SLOT_NOT_FOUND,
            "viewport stack target is unavailable",
        )

    order = _stack_order(layout)
    target_position = order.index(target_index)
    if (
        edit.action == ViewportStackAction.BRING_TO_FRONT
        and target_position == len(order) - 1
    ) or (
        edit.action == ViewportStackAction.SEND_TO_BACK
        and target_position == 0
    ):
        return layout

    order.remove(target_index)
    if edit.action == ViewportStackAction.BRING_TO_FRONT:
        order.append(target_index)
    else:
        order.insert(0, target_index)

    z_by_index = {index: z_index for z_index, index in enumerate(order)}
    placements = tuple(
        ViewportPlacement(
            logical_slot=placement.logical_slot,
            geometry=placement.geometry.model_copy(
                update={"z_index": z_by_index[index]},
            ),
        )
        for index, placement in enumerate(layout.placements)
    )
    return ViewportLayout(placements=placements)
