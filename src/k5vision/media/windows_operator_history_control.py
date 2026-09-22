"""Transactional viewport history for the accepted live Windows operator control path."""

from __future__ import annotations

import asyncio
import enum
import typing

from pydantic import BaseModel, ConfigDict, Field

from k5vision.media.viewport_editor import ViewportEditorError, apply_viewport_edit
from k5vision.media.viewport_geometry import ViewportLayout
from k5vision.media.viewport_history_control import (
    TransactionalViewportHistoryControl,
    ViewportHistoryControlError,
    ViewportHistoryControlErrorCode,
)
from k5vision.media.windows_operator_application import WindowsOperatorApplicationSnapshot
from k5vision.media.windows_operator_control import (
    BoundedWindowsOperatorControl,
    WindowsOperatorControlError,
    WindowsOperatorControlErrorCode,
    WindowsOperatorControlSnapshot,
    _ControllableApplicationBoundary,
    _ControlKind,
    _ControlRequest,
    _RelayoutApplicationBoundary,
)
from k5vision.media.windows_operator_session import WindowsOperatorSessionState

_MAX_VIEWPORT_HISTORY = 1024
_MAX_VIEWPORT_HISTORY_OPERATIONS = 1_000_000


class _HistoryControlKind(enum.StrEnum):
    UNDO = "undo"
    REDO = "redo"


class WindowsOperatorHistoryControlSnapshot(BaseModel):
    """Aggregate source-free history state for one live operator session."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: typing.Literal["1"] = "1"
    control: WindowsOperatorControlSnapshot
    undo_depth: int = Field(default=0, ge=0, le=_MAX_VIEWPORT_HISTORY)
    redo_depth: int = Field(default=0, ge=0, le=_MAX_VIEWPORT_HISTORY)
    history_operations: int = Field(default=0, ge=0, le=_MAX_VIEWPORT_HISTORY_OPERATIONS)
    undo_requests: int = Field(default=0, ge=0)
    redo_requests: int = Field(default=0, ge=0)
    history_rebases: int = Field(default=0, ge=0)


class BoundedHistoryWindowsOperatorControl(BoundedWindowsOperatorControl):
    """Add bounded undo/redo to the running accepted operator-control queue."""

    def __init__(
        self,
        *,
        max_viewport_history: int = 128,
        max_viewport_history_operations: int = 100_000,
        **kwargs: typing.Any,
    ) -> None:
        if (
            not 1 <= max_viewport_history <= _MAX_VIEWPORT_HISTORY
            or not 1
            <= max_viewport_history_operations
            <= _MAX_VIEWPORT_HISTORY_OPERATIONS
        ):
            raise WindowsOperatorControlError(
                WindowsOperatorControlErrorCode.INVALID_CONFIGURATION,
                "operator viewport history bounds are invalid",
            )
        super().__init__(**kwargs)
        self._max_viewport_history = max_viewport_history
        self._max_viewport_history_operations = max_viewport_history_operations
        self._viewport_history: TransactionalViewportHistoryControl | None = None
        self._undo_requests = 0
        self._redo_requests = 0
        self._history_rebases = 0

    def _ensure_history(self) -> TransactionalViewportHistoryControl:
        if self._viewport_history is None:
            if self._active_layout is None:
                raise WindowsOperatorControlError(
                    WindowsOperatorControlErrorCode.INVALID_STATE,
                    "operator viewport history requires an active layout",
                )
            self._viewport_history = TransactionalViewportHistoryControl(
                self._active_layout,
                max_history=self._max_viewport_history,
                max_operations=self._max_viewport_history_operations,
            )
        return self._viewport_history

    @property
    def history_control_snapshot(self) -> WindowsOperatorHistoryControlSnapshot:
        history = self._viewport_history
        return WindowsOperatorHistoryControlSnapshot(
            control=self.control_snapshot,
            undo_depth=0 if history is None else history.snapshot.undo_depth,
            redo_depth=0 if history is None else history.snapshot.redo_depth,
            history_operations=0 if history is None else history.snapshot.operations,
            undo_requests=self._undo_requests,
            redo_requests=self._redo_requests,
            history_rebases=self._history_rebases,
        )

    def request_undo(self) -> WindowsOperatorHistoryControlSnapshot:
        """Queue one bounded undo through the same serialized live-control queue."""
        self._enqueue(
            _ControlRequest(
                kind=typing.cast(typing.Any, _HistoryControlKind.UNDO),
            )
        )
        return self.history_control_snapshot

    def request_redo(self) -> WindowsOperatorHistoryControlSnapshot:
        """Queue one bounded redo through the same serialized live-control queue."""
        self._enqueue(
            _ControlRequest(
                kind=typing.cast(typing.Any, _HistoryControlKind.REDO),
            )
        )
        return self.history_control_snapshot

    @staticmethod
    def _raise_history_failure(exc: ViewportHistoryControlError) -> typing.NoReturn:
        if exc.code == ViewportHistoryControlErrorCode.RELAYOUT_FAILURE:
            raise WindowsOperatorControlError(
                WindowsOperatorControlErrorCode.APPLICATION_FAILURE,
                "operator viewport relayout was rejected",
            ) from None
        if exc.code == ViewportHistoryControlErrorCode.INVALID_CONFIGURATION:
            raise WindowsOperatorControlError(
                WindowsOperatorControlErrorCode.APPLICATION_FAILURE,
                "operator viewport history boundary is invalid",
            ) from None
        raise WindowsOperatorControlError(
            WindowsOperatorControlErrorCode.INVALID_EDIT,
            "operator viewport history request is unavailable",
        ) from None

    def _rebase_history(self, layout: ViewportLayout) -> None:
        history = self._ensure_history()
        try:
            history.rebase(layout)
        except ViewportHistoryControlError as exc:
            self._raise_history_failure(exc)
        if self._pointer_drag is not None:
            self._cancelled_interactions += 1
        self._pointer_drag = None
        self._active_layout = layout
        self._history_rebases += 1

    async def _process_controls(
        self,
        application: _ControllableApplicationBoundary,
        wait_task: asyncio.Task[WindowsOperatorApplicationSnapshot] | None,
    ) -> tuple[
        asyncio.Task[WindowsOperatorApplicationSnapshot] | None,
        WindowsOperatorSessionState | None,
    ]:
        history = self._ensure_history()
        for _ in range(self._max_controls_per_cycle):
            try:
                request = self._controls.get_nowait()
            except asyncio.QueueEmpty:
                break

            if request.kind == _ControlKind.STOP:
                await self._cancel_wait_task(wait_task)
                self._application_snapshot = await application.stop()
                self._processed_controls += 1
                self._stop_requests += 1
                return None, WindowsOperatorSessionState.COMPLETE

            if request.kind in {_HistoryControlKind.UNDO, _HistoryControlKind.REDO}:
                if not isinstance(application, _RelayoutApplicationBoundary):
                    raise WindowsOperatorControlError(
                        WindowsOperatorControlErrorCode.APPLICATION_FAILURE,
                        "operator application does not support source-free relayout",
                    )
                try:
                    accepted = (
                        await history.undo(application)
                        if request.kind == _HistoryControlKind.UNDO
                        else await history.redo(application)
                    )
                except ViewportHistoryControlError as exc:
                    self._raise_history_failure(exc)
                self._application_snapshot = application.snapshot
                self._active_layout = accepted.layout
                self._processed_controls += 1
                self._relayouts += 1
                if request.kind == _HistoryControlKind.UNDO:
                    self._undo_requests += 1
                else:
                    self._redo_requests += 1
                continue

            if request.kind == _ControlKind.EDIT:
                if request.edit is None or self._active_layout is None:
                    raise WindowsOperatorControlError(
                        WindowsOperatorControlErrorCode.INVALID_EDIT,
                        "operator viewport edit cannot be applied",
                    )
                if not isinstance(application, _RelayoutApplicationBoundary):
                    raise WindowsOperatorControlError(
                        WindowsOperatorControlErrorCode.APPLICATION_FAILURE,
                        "operator application does not support source-free relayout",
                    )
                try:
                    apply_viewport_edit(self._active_layout, request.edit)
                except ViewportEditorError:
                    raise WindowsOperatorControlError(
                        WindowsOperatorControlErrorCode.INVALID_EDIT,
                        "operator viewport edit is invalid",
                    ) from None
                try:
                    accepted = await history.apply(application, request.edit)
                except ViewportHistoryControlError as exc:
                    self._raise_history_failure(exc)
                self._application_snapshot = application.snapshot
                self._active_layout = accepted.layout
                self._processed_controls += 1
                self._relayouts += 1
                self._viewport_edits += 1
                if request.pointer_origin:
                    self._interaction_edits += 1
                continue

            if not isinstance(request.layout, ViewportLayout):
                raise WindowsOperatorControlError(
                    WindowsOperatorControlErrorCode.APPLICATION_FAILURE,
                    "operator layout request is invalid",
                )

            if request.kind == _ControlKind.RELAYOUT:
                if not isinstance(application, _RelayoutApplicationBoundary):
                    raise WindowsOperatorControlError(
                        WindowsOperatorControlErrorCode.APPLICATION_FAILURE,
                        "operator application does not support source-free relayout",
                    )
                self._application_snapshot = await application.relayout(request.layout)
                self._rebase_history(request.layout)
                self._processed_controls += 1
                self._relayouts += 1
                continue

            if request.kind != _ControlKind.REPLACE:
                raise WindowsOperatorControlError(
                    WindowsOperatorControlErrorCode.INVALID_CONFIGURATION,
                    "operator control request is invalid",
                )

            await self._cancel_wait_task(wait_task)
            self._application_snapshot = await application.replace(
                request.layout,
                request.streams,
            )
            self._rebase_history(request.layout)
            self._processed_controls += 1
            self._replacements += 1
            wait_task = asyncio.create_task(application.wait())

        return wait_task, None
