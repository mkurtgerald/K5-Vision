"""Bounded undo/redo history above the accepted source-free viewport editor."""

from __future__ import annotations

import enum
import typing

from pydantic import BaseModel, ConfigDict, Field

from k5vision.media.viewport_editor import ViewportEdit, apply_viewport_edit
from k5vision.media.viewport_geometry import ViewportLayout

_MAX_HISTORY = 1024
_MAX_OPERATIONS = 1_000_000


class ViewportEditorHistoryErrorCode(enum.StrEnum):
    INVALID_CONFIGURATION = "invalid_configuration"
    NOTHING_TO_UNDO = "nothing_to_undo"
    NOTHING_TO_REDO = "nothing_to_redo"
    OPERATION_LIMIT = "operation_limit"


class ViewportEditorHistoryError(RuntimeError):
    def __init__(self, code: ViewportEditorHistoryErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class ViewportEditorHistorySnapshot(BaseModel):
    """Source-free history state; layouts remain logical geometry only."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: typing.Literal["1"] = "1"
    layout: ViewportLayout
    undo_depth: int = Field(ge=0, le=_MAX_HISTORY)
    redo_depth: int = Field(ge=0, le=_MAX_HISTORY)
    operations: int = Field(ge=0, le=_MAX_OPERATIONS)


class BoundedViewportEditorHistory:
    """Apply edits with deterministic bounded undo/redo semantics."""

    def __init__(
        self,
        layout: ViewportLayout,
        *,
        max_history: int = 128,
        max_operations: int = 100_000,
    ) -> None:
        if (
            not isinstance(layout, ViewportLayout)
            or not 1 <= max_history <= _MAX_HISTORY
            or not 1 <= max_operations <= _MAX_OPERATIONS
        ):
            raise ViewportEditorHistoryError(
                ViewportEditorHistoryErrorCode.INVALID_CONFIGURATION,
                "viewport history configuration is invalid",
            )
        self._layout = layout
        self._max_history = max_history
        self._max_operations = max_operations
        self._operations = 0
        self._undo: list[ViewportLayout] = []
        self._redo: list[ViewportLayout] = []

    @property
    def snapshot(self) -> ViewportEditorHistorySnapshot:
        return ViewportEditorHistorySnapshot(
            layout=self._layout,
            undo_depth=len(self._undo),
            redo_depth=len(self._redo),
            operations=self._operations,
        )

    def _consume(self) -> None:
        if self._operations >= self._max_operations:
            raise ViewportEditorHistoryError(
                ViewportEditorHistoryErrorCode.OPERATION_LIMIT,
                "viewport history operation limit reached",
            )
        self._operations += 1

    def apply(self, edit: ViewportEdit) -> ViewportEditorHistorySnapshot:
        candidate = apply_viewport_edit(self._layout, edit)
        self._consume()
        self._undo.append(self._layout)
        if len(self._undo) > self._max_history:
            del self._undo[0]
        self._layout = candidate
        self._redo.clear()
        return self.snapshot

    def undo(self) -> ViewportEditorHistorySnapshot:
        if not self._undo:
            raise ViewportEditorHistoryError(
                ViewportEditorHistoryErrorCode.NOTHING_TO_UNDO,
                "no viewport edit is available to undo",
            )
        self._consume()
        previous = self._undo.pop()
        self._redo.append(self._layout)
        self._layout = previous
        return self.snapshot

    def redo(self) -> ViewportEditorHistorySnapshot:
        if not self._redo:
            raise ViewportEditorHistoryError(
                ViewportEditorHistoryErrorCode.NOTHING_TO_REDO,
                "no viewport edit is available to redo",
            )
        self._consume()
        following = self._redo.pop()
        self._undo.append(self._layout)
        self._layout = following
        return self.snapshot
