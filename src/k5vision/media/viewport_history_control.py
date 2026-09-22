"""Transactional undo/redo control above accepted viewport relayout boundaries."""

from __future__ import annotations

import enum
import typing

from pydantic import BaseModel, ConfigDict

from k5vision.media.viewport_editor import ViewportEdit, apply_viewport_edit
from k5vision.media.viewport_editor_history import (
    BoundedViewportEditorHistory,
    ViewportEditorHistoryError,
)
from k5vision.media.viewport_geometry import ViewportLayout


@typing.runtime_checkable
class ViewportRelayoutBoundary(typing.Protocol):
    async def relayout(self, layout: ViewportLayout) -> object: ...


class ViewportHistoryControlErrorCode(enum.StrEnum):
    INVALID_CONFIGURATION = "invalid_configuration"
    HISTORY_FAILURE = "history_failure"
    RELAYOUT_FAILURE = "relayout_failure"


class ViewportHistoryControlError(RuntimeError):
    """Sanitized history-control failure without source or native identity."""

    def __init__(self, code: ViewportHistoryControlErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class ViewportHistoryControlSnapshot(BaseModel):
    """Source-free retained state for one bounded history controller."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: typing.Literal["1"] = "1"
    layout: ViewportLayout
    undo_depth: int
    redo_depth: int
    operations: int


class TransactionalViewportHistoryControl:
    """Commit viewport history only after the visible relayout boundary accepts it."""

    def __init__(
        self,
        layout: ViewportLayout,
        *,
        max_history: int = 128,
        max_operations: int = 100_000,
    ) -> None:
        try:
            self._history = BoundedViewportEditorHistory(
                layout,
                max_history=max_history,
                max_operations=max_operations,
            )
        except ViewportEditorHistoryError as exc:
            raise ViewportHistoryControlError(
                ViewportHistoryControlErrorCode.INVALID_CONFIGURATION,
                "viewport history control configuration is invalid",
            ) from exc

    @property
    def snapshot(self) -> ViewportHistoryControlSnapshot:
        state = self._history.snapshot
        return ViewportHistoryControlSnapshot(
            layout=state.layout,
            undo_depth=state.undo_depth,
            redo_depth=state.redo_depth,
            operations=state.operations,
        )

    def _ensure_operation_available(self) -> None:
        try:
            self._history.ensure_operation_available()
        except ViewportEditorHistoryError:
            raise ViewportHistoryControlError(
                ViewportHistoryControlErrorCode.HISTORY_FAILURE,
                "viewport history operation limit reached",
            ) from None

    async def _relayout(
        self,
        application: ViewportRelayoutBoundary,
        candidate: ViewportLayout,
    ) -> None:
        if not isinstance(application, ViewportRelayoutBoundary):
            raise ViewportHistoryControlError(
                ViewportHistoryControlErrorCode.INVALID_CONFIGURATION,
                "viewport relayout boundary is invalid",
            )
        try:
            await application.relayout(candidate)
        except Exception:
            raise ViewportHistoryControlError(
                ViewportHistoryControlErrorCode.RELAYOUT_FAILURE,
                "viewport relayout was rejected",
            ) from None

    def rebase(self, layout: ViewportLayout) -> ViewportHistoryControlSnapshot:
        """Accept an explicit replace/relayout as a new history baseline."""
        try:
            self._history.rebase(layout)
        except ViewportEditorHistoryError:
            raise ViewportHistoryControlError(
                ViewportHistoryControlErrorCode.HISTORY_FAILURE,
                "viewport history could not rebase",
            ) from None
        return self.snapshot

    async def apply(
        self,
        application: ViewportRelayoutBoundary,
        edit: ViewportEdit,
    ) -> ViewportHistoryControlSnapshot:
        try:
            candidate = apply_viewport_edit(self._history.snapshot.layout, edit)
        except Exception:
            raise ViewportHistoryControlError(
                ViewportHistoryControlErrorCode.HISTORY_FAILURE,
                "viewport edit is invalid",
            ) from None
        self._ensure_operation_available()
        await self._relayout(application, candidate)
        try:
            accepted = self._history.apply(edit)
        except ViewportEditorHistoryError:
            raise ViewportHistoryControlError(
                ViewportHistoryControlErrorCode.HISTORY_FAILURE,
                "viewport history could not accept edit",
            ) from None
        if accepted.layout != candidate:
            raise ViewportHistoryControlError(
                ViewportHistoryControlErrorCode.HISTORY_FAILURE,
                "viewport history state mismatch",
            )
        return self.snapshot

    async def undo(
        self,
        application: ViewportRelayoutBoundary,
    ) -> ViewportHistoryControlSnapshot:
        try:
            candidate = self._history.undo_candidate
        except ViewportEditorHistoryError:
            raise ViewportHistoryControlError(
                ViewportHistoryControlErrorCode.HISTORY_FAILURE,
                "no viewport edit is available to undo",
            ) from None
        self._ensure_operation_available()
        await self._relayout(application, candidate)
        try:
            accepted = self._history.undo()
        except ViewportEditorHistoryError:
            raise ViewportHistoryControlError(
                ViewportHistoryControlErrorCode.HISTORY_FAILURE,
                "viewport history could not commit undo",
            ) from None
        if accepted.layout != candidate:
            raise ViewportHistoryControlError(
                ViewportHistoryControlErrorCode.HISTORY_FAILURE,
                "viewport undo state mismatch",
            )
        return self.snapshot

    async def redo(
        self,
        application: ViewportRelayoutBoundary,
    ) -> ViewportHistoryControlSnapshot:
        try:
            candidate = self._history.redo_candidate
        except ViewportEditorHistoryError:
            raise ViewportHistoryControlError(
                ViewportHistoryControlErrorCode.HISTORY_FAILURE,
                "no viewport edit is available to redo",
            ) from None
        self._ensure_operation_available()
        await self._relayout(application, candidate)
        try:
            accepted = self._history.redo()
        except ViewportEditorHistoryError:
            raise ViewportHistoryControlError(
                ViewportHistoryControlErrorCode.HISTORY_FAILURE,
                "viewport history could not commit redo",
            ) from None
        if accepted.layout != candidate:
            raise ViewportHistoryControlError(
                ViewportHistoryControlErrorCode.HISTORY_FAILURE,
                "viewport redo state mismatch",
            )
        return self.snapshot
