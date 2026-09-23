"""Bounded human-session pause/resume control for active operator playback.

The control registry is task-scoped by authenticated principal plus recording UUID.
It never retains source URIs, credentials, paths, or media payloads. Playback media
continues to flow through the accepted presentation delivery and playback pump.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from enum import StrEnum
from pathlib import Path
from typing import Iterator, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from k5vision.media.playback_pump import PlaybackPumpError, PlaybackPumpErrorCode, PlaybackPumpState
from k5vision.media.playback_schedule import PlaybackRate
from k5vision.media.presentation_playback import (
    BoundedPresentationPlaybackDelivery,
    PresentationPlaybackError,
    PresentationPlaybackErrorCode,
    PresentationPlaybackState,
)
from k5vision.media.recording_descriptor import RecordingStreamDescriptor
from k5vision.operator_playback import WindowsMixedOperatorPlaybackLauncher

_ControlKey = tuple[UUID, UUID]
_CONTROL_KEY: ContextVar[_ControlKey | None] = ContextVar("k5_operator_playback_control", default=None)


class OperatorPlaybackControlState(StrEnum):
    RUNNING = "running"
    PAUSED = "paused"


class OperatorPlaybackControlErrorCode(StrEnum):
    NOT_ACTIVE = "not_active"
    INVALID_STATE = "invalid_state"
    CONTROL_FAILURE = "control_failure"


class OperatorPlaybackControlError(RuntimeError):
    """Sanitized operator playback-control failure."""

    def __init__(self, code: OperatorPlaybackControlErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class OperatorPlaybackControlReceipt(BaseModel):
    """Source/path/payload-free playback control result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    recording_id: UUID
    state: OperatorPlaybackControlState


@contextmanager
def playback_control_scope(principal_id: UUID, recording_id: UUID) -> Iterator[None]:
    """Bind one active playback creation to its authenticated human principal."""
    if not isinstance(principal_id, UUID) or not isinstance(recording_id, UUID):
        raise TypeError("playback control scope requires UUID identifiers")
    token = _CONTROL_KEY.set((principal_id, recording_id))
    try:
        yield
    finally:
        _CONTROL_KEY.reset(token)


class BoundedControllablePresentationPlaybackDelivery(BoundedPresentationPlaybackDelivery):
    """Accepted presentation playback with bounded pause/resume control."""

    async def pause(self) -> OperatorPlaybackControlState:
        if self.snapshot.state is not PresentationPlaybackState.RUNNING:
            raise OperatorPlaybackControlError(
                OperatorPlaybackControlErrorCode.INVALID_STATE,
                "operator playback is not running",
            )
        try:
            snapshot = await self._pump.pause()
        except PlaybackPumpError as exc:
            code = (
                OperatorPlaybackControlErrorCode.INVALID_STATE
                if exc.code is PlaybackPumpErrorCode.INVALID_STATE
                else OperatorPlaybackControlErrorCode.CONTROL_FAILURE
            )
            raise OperatorPlaybackControlError(code, "operator playback pause failed") from None
        if snapshot.state is not PlaybackPumpState.PAUSED:
            raise OperatorPlaybackControlError(
                OperatorPlaybackControlErrorCode.CONTROL_FAILURE,
                "operator playback pause failed",
            )
        return OperatorPlaybackControlState.PAUSED

    async def resume(self) -> OperatorPlaybackControlState:
        if self.snapshot.state is not PresentationPlaybackState.RUNNING:
            raise OperatorPlaybackControlError(
                OperatorPlaybackControlErrorCode.INVALID_STATE,
                "operator playback is not active",
            )
        try:
            snapshot = await self._pump.resume()
        except PlaybackPumpError as exc:
            code = (
                OperatorPlaybackControlErrorCode.INVALID_STATE
                if exc.code is PlaybackPumpErrorCode.INVALID_STATE
                else OperatorPlaybackControlErrorCode.CONTROL_FAILURE
            )
            raise OperatorPlaybackControlError(code, "operator playback resume failed") from None
        if snapshot.state is not PlaybackPumpState.RUNNING:
            raise OperatorPlaybackControlError(
                OperatorPlaybackControlErrorCode.CONTROL_FAILURE,
                "operator playback resume failed",
            )
        return OperatorPlaybackControlState.RUNNING


class ControlledWindowsMixedOperatorPlaybackLauncher(WindowsMixedOperatorPlaybackLauncher):
    """Register only active, authenticated playback deliveries for bounded control."""

    def __init__(self, *, runtime_factory=None, live_delivery_factory=None) -> None:
        self._active_controls: dict[_ControlKey, BoundedControllablePresentationPlaybackDelivery] = {}
        super().__init__(
            runtime_factory=runtime_factory,
            live_delivery_factory=live_delivery_factory,
            playback_factory=self._create_controlled_delivery,
        )

    def _create_controlled_delivery(
        self,
        path: Path,
        descriptor: RecordingStreamDescriptor,
        start_ms: int,
        end_ms: int,
        rate: PlaybackRate,
    ) -> BoundedControllablePresentationPlaybackDelivery:
        delivery = BoundedControllablePresentationPlaybackDelivery(
            path,
            descriptor,
            start_ms,
            end_ms,
            rate,
        )
        key = _CONTROL_KEY.get()
        if key is not None:
            if key in self._active_controls:
                raise PresentationPlaybackError(
                    PresentationPlaybackErrorCode.INVALID_STATE,
                    "operator playback control is already active",
                )
            self._active_controls[key] = delivery
        return delivery

    async def run(self, *args, **kwargs):
        key = _CONTROL_KEY.get()
        try:
            return await super().run(*args, **kwargs)
        finally:
            if key is not None:
                self._active_controls.pop(key, None)

    def _active_delivery(
        self,
        principal_id: UUID,
        recording_id: UUID,
    ) -> BoundedControllablePresentationPlaybackDelivery:
        delivery = self._active_controls.get((principal_id, recording_id))
        if delivery is None:
            raise OperatorPlaybackControlError(
                OperatorPlaybackControlErrorCode.NOT_ACTIVE,
                "operator playback is not active",
            )
        return delivery

    async def pause(
        self,
        principal_id: UUID,
        recording_id: UUID,
    ) -> OperatorPlaybackControlReceipt:
        delivery = self._active_delivery(principal_id, recording_id)
        state = await delivery.pause()
        return OperatorPlaybackControlReceipt(recording_id=recording_id, state=state)

    async def resume(
        self,
        principal_id: UUID,
        recording_id: UUID,
    ) -> OperatorPlaybackControlReceipt:
        delivery = self._active_delivery(principal_id, recording_id)
        state = await delivery.resume()
        return OperatorPlaybackControlReceipt(recording_id=recording_id, state=state)
