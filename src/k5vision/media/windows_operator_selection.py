"""Source-free viewport selection above the accepted capture-aware operator control."""

from __future__ import annotations

import asyncio
import typing
from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

from k5vision.media.mixed_presentation import MixedPresentationStream
from k5vision.media.viewport_stack import (
    ViewportStackAction,
    ViewportStackEdit,
    ViewportStackError,
    apply_viewport_stack,
)
from k5vision.media.windows_operator_application import WindowsOperatorApplicationSnapshot
from k5vision.media.windows_operator_control import (
    BoundedWindowsOperatorControl,
    WindowsOperatorControlError,
    WindowsOperatorControlErrorCode,
    WindowsOperatorControlSnapshot,
    _ControllableApplicationBoundary,
    _select_pointer_drag,
)
from k5vision.media.windows_operator_interaction import WindowsPointerEvent, WindowsPointerEventKind
from k5vision.media.windows_operator_session import WindowsOperatorSessionState


class WindowsOperatorSelectionSnapshot(BaseModel):
    """Privacy-safe selected logical-layout state for one operator session."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: typing.Literal["1"] = "1"
    control: WindowsOperatorControlSnapshot
    selected_logical_slot: int | None = Field(default=None, ge=0, le=4095)
    selection_changes: int = Field(default=0, ge=0)
    stack_changes: int = Field(default=0, ge=0)


@dataclass(slots=True)
class _SelectionCandidate:
    logical_slot: int | None
    start_x: int
    start_y: int
    moved: bool = False


class BoundedSelectableWindowsOperatorControl(BoundedWindowsOperatorControl):
    """Add deterministic viewport selection without introducing source identity."""

    def __init__(self, **kwargs: typing.Any) -> None:
        super().__init__(**kwargs)
        self._selected_logical_slot: int | None = None
        self._selection_changes = 0
        self._stack_changes = 0
        self._selection_candidate: _SelectionCandidate | None = None

    @property
    def selection_snapshot(self) -> WindowsOperatorSelectionSnapshot:
        return WindowsOperatorSelectionSnapshot(
            control=self.control_snapshot,
            selected_logical_slot=self._selected_logical_slot,
            selection_changes=self._selection_changes,
            stack_changes=self._stack_changes,
        )

    def _set_selection(self, logical_slot: int | None) -> None:
        if logical_slot == self._selected_logical_slot:
            return
        self._selected_logical_slot = logical_slot
        self._selection_changes += 1

    def _reconcile_selection(self) -> None:
        selected = self._selected_logical_slot
        if selected is None:
            return
        layout = self._active_layout
        if layout is None or selected not in {item.logical_slot for item in layout.placements}:
            self._set_selection(None)

    def request_stack(
        self,
        action: ViewportStackAction,
    ) -> WindowsOperatorSelectionSnapshot:
        """Queue one source-free z-order change for the selected logical viewport."""
        if not isinstance(action, ViewportStackAction):
            raise WindowsOperatorControlError(
                WindowsOperatorControlErrorCode.INVALID_CONFIGURATION,
                "operator viewport stack request is invalid",
            )
        selected = self._selected_logical_slot
        layout = self._active_layout
        if selected is None or layout is None:
            raise WindowsOperatorControlError(
                WindowsOperatorControlErrorCode.INVALID_EDIT,
                "operator viewport stack requires a selected viewport",
            )
        try:
            candidate = apply_viewport_stack(
                layout,
                ViewportStackEdit(logical_slot=selected, action=action),
            )
        except ViewportStackError:
            raise WindowsOperatorControlError(
                WindowsOperatorControlErrorCode.INVALID_EDIT,
                "operator viewport stack request is invalid",
            ) from None
        if candidate is layout:
            return self.selection_snapshot
        self.request_relayout(candidate)
        self._stack_changes += 1
        return self.selection_snapshot

    def _consume_pointer_events(self, events: tuple[WindowsPointerEvent, ...]) -> None:
        for event in events:
            if not isinstance(event, WindowsPointerEvent):
                super()._consume_pointer_events((event,))
                continue

            if event.kind == WindowsPointerEventKind.DOWN:
                hit = (
                    None
                    if self._active_layout is None
                    else _select_pointer_drag(self._active_layout, event.x, event.y)
                )
                self._selection_candidate = _SelectionCandidate(
                    logical_slot=None if hit is None else hit.logical_slot,
                    start_x=event.x,
                    start_y=event.y,
                )
                super()._consume_pointer_events((event,))
                continue

            candidate = self._selection_candidate
            if event.kind == WindowsPointerEventKind.MOVE:
                if candidate is not None and (
                    event.x != candidate.start_x or event.y != candidate.start_y
                ):
                    candidate.moved = True
                super()._consume_pointer_events((event,))
                continue

            if event.kind == WindowsPointerEventKind.CANCEL:
                self._selection_candidate = None
                super()._consume_pointer_events((event,))
                continue

            if event.kind == WindowsPointerEventKind.UP:
                self._selection_candidate = None
                super()._consume_pointer_events((event,))
                if (
                    candidate is not None
                    and not candidate.moved
                    and event.x == candidate.start_x
                    and event.y == candidate.start_y
                ):
                    self._set_selection(candidate.logical_slot)
                continue

            super()._consume_pointer_events((event,))

    async def _process_controls(
        self,
        application: _ControllableApplicationBoundary,
        wait_task: asyncio.Task[WindowsOperatorApplicationSnapshot] | None,
    ) -> tuple[
        asyncio.Task[WindowsOperatorApplicationSnapshot] | None,
        WindowsOperatorSessionState | None,
    ]:
        result = await super()._process_controls(application, wait_task)
        self._reconcile_selection()
        return result

    async def run(
        self,
        *,
        width: int,
        height: int,
        layout: typing.Any,
        streams: Sequence[MixedPresentationStream],
    ) -> WindowsOperatorSelectionSnapshot:
        """Run the accepted operator session and clear ephemeral click state on exit."""
        try:
            await super().run(width=width, height=height, layout=layout, streams=streams)
        finally:
            self._selection_candidate = None
        return self.selection_snapshot
