"""Bounded assembly of the accepted operator presentation runtime.

This module is the project-owned construction boundary between caller-supplied logical
viewport consumers/stream bindings and the accepted Stage-28 through Stage-31 media
stack. Source and storage identities remain execution-only in the selected stream
objects and are never copied into runtime state, snapshots, or errors.
"""

from __future__ import annotations

import enum
import typing
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field

from k5vision.media.mixed_presentation import (
    BoundedMixedPresentation,
    MixedLiveStream,
    MixedPlaybackStream,
    MixedPresentationStream,
)
from k5vision.media.presentation_controller import (
    BoundedPresentationController,
    PresentationControllerError,
    PresentationControllerErrorCode,
    PresentationControllerState,
)
from k5vision.media.presentation_session import BoundedPresentationSession
from k5vision.media.viewport_dispatch import (
    BoundedViewportDispatcher,
    ViewportBinding,
    ViewportDispatchError,
)

_MAX_STREAMS = 16
_MAX_VIEWPORTS = 16
_MAX_TOTAL_FRAMES = 1_000_000
_MAX_FRAME_BYTES = 256 * 1024 * 1024
_MAX_TOTAL_FRAME_BYTES = 16 * 1024 * 1024 * 1024
_MAX_SOURCE_SPAN_MS = 2_147_483_647


class PresentationRuntimeState(enum.StrEnum):
    CREATED = "created"
    RUNNING = "running"
    COMPLETE = "complete"
    STOPPED = "stopped"
    FAILED = "failed"
    CLOSED = "closed"


class PresentationRuntimeErrorCode(enum.StrEnum):
    INVALID_CONFIGURATION = "invalid_configuration"
    INVALID_PLAN = "invalid_plan"
    INVALID_STATE = "invalid_state"
    CONTROL_FAILURE = "control_failure"


class PresentationRuntimeError(RuntimeError):
    """Sanitized presentation-runtime failure."""

    def __init__(self, code: PresentationRuntimeErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class PresentationRuntimeSnapshot(BaseModel):
    """Source/path/identifier/payload-free runtime observability."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: typing.Literal["1"] = "1"
    state: PresentationRuntimeState
    stream_count: int = Field(ge=0, le=_MAX_STREAMS)
    live_streams: int = Field(ge=0, le=_MAX_STREAMS)
    playback_streams: int = Field(ge=0, le=_MAX_STREAMS)
    viewport_count: int = Field(ge=0, le=_MAX_VIEWPORTS)
    completed_streams: int = Field(ge=0, le=_MAX_STREAMS)
    delivered_frames: int = Field(ge=0, le=_MAX_TOTAL_FRAMES)
    delivered_frame_bytes: int = Field(ge=0, le=_MAX_TOTAL_FRAME_BYTES)
    max_source_span_ms: int = Field(ge=0, le=_MAX_SOURCE_SPAN_MS)


_CONTROLLER_STATE_MAP: dict[PresentationControllerState, PresentationRuntimeState] = {
    PresentationControllerState.CREATED: PresentationRuntimeState.CREATED,
    PresentationControllerState.RUNNING: PresentationRuntimeState.RUNNING,
    PresentationControllerState.COMPLETE: PresentationRuntimeState.COMPLETE,
    PresentationControllerState.STOPPED: PresentationRuntimeState.STOPPED,
    PresentationControllerState.FAILED: PresentationRuntimeState.FAILED,
    PresentationControllerState.CLOSED: PresentationRuntimeState.CLOSED,
}


class BoundedPresentationRuntime:
    """Assemble and operate one accepted bounded presentation runtime."""

    def __init__(
        self,
        bindings: Sequence[ViewportBinding],
        *,
        max_streams: int = 16,
        max_viewports: int = 16,
        max_total_frames: int = 100_000,
        max_frame_bytes: int = 64 * 1024 * 1024,
        max_total_frame_bytes: int = 4 * 1024 * 1024 * 1024,
        consumer_timeout_seconds: float = 0.5,
        stop_timeout_seconds: float = 5.0,
        allow_all_live: bool = False,
    ) -> None:
        if not 2 <= max_streams <= _MAX_STREAMS:
            raise PresentationRuntimeError(
                PresentationRuntimeErrorCode.INVALID_CONFIGURATION,
                "presentation runtime max_streams must be between 2 and 16",
            )
        if not 2 <= max_viewports <= _MAX_VIEWPORTS:
            raise PresentationRuntimeError(
                PresentationRuntimeErrorCode.INVALID_CONFIGURATION,
                "presentation runtime max_viewports must be between 2 and 16",
            )
        if max_viewports > max_streams:
            raise PresentationRuntimeError(
                PresentationRuntimeErrorCode.INVALID_CONFIGURATION,
                "presentation runtime viewport bound exceeds stream bound",
            )
        if not 1 <= max_total_frames <= _MAX_TOTAL_FRAMES:
            raise PresentationRuntimeError(
                PresentationRuntimeErrorCode.INVALID_CONFIGURATION,
                "presentation runtime frame bound is invalid",
            )
        if not 1 <= max_frame_bytes <= _MAX_FRAME_BYTES:
            raise PresentationRuntimeError(
                PresentationRuntimeErrorCode.INVALID_CONFIGURATION,
                "presentation runtime frame-byte bound is invalid",
            )
        if not 1 <= max_total_frame_bytes <= _MAX_TOTAL_FRAME_BYTES:
            raise PresentationRuntimeError(
                PresentationRuntimeErrorCode.INVALID_CONFIGURATION,
                "presentation runtime aggregate-byte bound is invalid",
            )
        if not 0 < consumer_timeout_seconds <= 10:
            raise PresentationRuntimeError(
                PresentationRuntimeErrorCode.INVALID_CONFIGURATION,
                "presentation runtime consumer timeout is invalid",
            )
        if not 0.1 <= stop_timeout_seconds <= 30.0:
            raise PresentationRuntimeError(
                PresentationRuntimeErrorCode.INVALID_CONFIGURATION,
                "presentation runtime stop timeout is invalid",
            )
        if not isinstance(allow_all_live, bool):
            raise PresentationRuntimeError(
                PresentationRuntimeErrorCode.INVALID_CONFIGURATION,
                "presentation runtime live-only capability is invalid",
            )

        selected_bindings = tuple(bindings)
        if not 2 <= len(selected_bindings) <= max_viewports:
            raise PresentationRuntimeError(
                PresentationRuntimeErrorCode.INVALID_CONFIGURATION,
                "presentation runtime viewport binding count is outside the configured bound",
            )
        if any(not isinstance(binding, ViewportBinding) for binding in selected_bindings):
            raise PresentationRuntimeError(
                PresentationRuntimeErrorCode.INVALID_CONFIGURATION,
                "presentation runtime viewport bindings are invalid",
            )
        slots = tuple(binding.slot for binding in selected_bindings)
        if len(set(slots)) != len(slots):
            raise PresentationRuntimeError(
                PresentationRuntimeErrorCode.INVALID_CONFIGURATION,
                "presentation runtime viewport slots must be unique",
            )

        try:
            dispatcher = BoundedViewportDispatcher(
                selected_bindings,
                max_viewports=max_viewports,
                max_total_frames=max_total_frames,
                max_frame_bytes=max_frame_bytes,
                max_total_frame_bytes=max_total_frame_bytes,
                consumer_timeout_seconds=consumer_timeout_seconds,
            )
            coordinator = BoundedMixedPresentation(
                max_streams=max_streams,
                max_total_frames=max_total_frames,
                max_total_frame_bytes=max_total_frame_bytes,
                consumer_timeout_seconds=consumer_timeout_seconds,
                allow_all_live=allow_all_live,
            )
            session = BoundedPresentationSession(coordinator, dispatcher)
            controller = BoundedPresentationController(
                session,
                stop_timeout_seconds=stop_timeout_seconds,
            )
        except (TypeError, ValueError, ViewportDispatchError):
            raise PresentationRuntimeError(
                PresentationRuntimeErrorCode.INVALID_CONFIGURATION,
                "presentation runtime assembly failed",
            ) from None

        self._controller = controller
        self._viewport_slots = frozenset(slots)
        self._max_streams = max_streams
        self._allow_all_live = allow_all_live

    @property
    def snapshot(self) -> PresentationRuntimeSnapshot:
        child = self._controller.snapshot
        return PresentationRuntimeSnapshot(
            state=_CONTROLLER_STATE_MAP[child.state],
            stream_count=child.stream_count,
            live_streams=child.live_streams,
            playback_streams=child.playback_streams,
            viewport_count=child.viewport_count,
            completed_streams=child.completed_streams,
            delivered_frames=child.delivered_frames,
            delivered_frame_bytes=child.delivered_frame_bytes,
            max_source_span_ms=child.max_source_span_ms,
        )

    def _validate_plan(
        self,
        streams: Sequence[MixedPresentationStream],
    ) -> tuple[MixedPresentationStream, ...]:
        selected = tuple(streams)
        if not 2 <= len(selected) <= self._max_streams:
            raise PresentationRuntimeError(
                PresentationRuntimeErrorCode.INVALID_PLAN,
                "presentation runtime stream count is outside the configured bound",
            )
        if any(
            not isinstance(stream, (MixedLiveStream, MixedPlaybackStream)) for stream in selected
        ):
            raise PresentationRuntimeError(
                PresentationRuntimeErrorCode.INVALID_PLAN,
                "presentation runtime stream plan is invalid",
            )
        slots = tuple(stream.slot for stream in selected)
        if len(set(slots)) != len(slots) or frozenset(slots) != self._viewport_slots:
            raise PresentationRuntimeError(
                PresentationRuntimeErrorCode.INVALID_PLAN,
                "presentation runtime stream and viewport slots must match exactly",
            )
        live_count = sum(isinstance(stream, MixedLiveStream) for stream in selected)
        playback_count = len(selected) - live_count
        if live_count == 0 or (playback_count == 0 and not self._allow_all_live):
            raise PresentationRuntimeError(
                PresentationRuntimeErrorCode.INVALID_PLAN,
                "presentation runtime requires live and playback streams",
            )
        return selected

    @staticmethod
    def _control_error(exc: PresentationControllerError) -> PresentationRuntimeError:
        if exc.code == PresentationControllerErrorCode.INVALID_STATE:
            return PresentationRuntimeError(
                PresentationRuntimeErrorCode.INVALID_STATE,
                "presentation runtime cannot perform operation from current state",
            )
        return PresentationRuntimeError(
            PresentationRuntimeErrorCode.CONTROL_FAILURE,
            "presentation runtime control operation failed",
        )

    async def start(
        self,
        streams: Sequence[MixedPresentationStream],
    ) -> PresentationRuntimeSnapshot:
        """Validate one exact slot plan, start it, and retain no stream identity."""
        if self.snapshot.state != PresentationRuntimeState.CREATED:
            raise PresentationRuntimeError(
                PresentationRuntimeErrorCode.INVALID_STATE,
                "presentation runtime cannot start from current state",
            )
        selected = self._validate_plan(streams)
        try:
            await self._controller.start(selected)
        except PresentationControllerError as exc:
            raise self._control_error(exc) from None
        return self.snapshot

    async def wait(self) -> PresentationRuntimeSnapshot:
        """Wait for the currently controlled presentation to reach a terminal state."""
        try:
            await self._controller.wait()
        except PresentationControllerError as exc:
            raise self._control_error(exc) from None
        return self.snapshot

    async def stop(self) -> PresentationRuntimeSnapshot:
        """Boundedly stop the running presentation and release viewport consumers."""
        try:
            await self._controller.stop()
        except PresentationControllerError as exc:
            raise self._control_error(exc) from None
        return self.snapshot

    async def close(self) -> PresentationRuntimeSnapshot:
        """Close the runtime and deterministically release accepted child boundaries."""
        try:
            await self._controller.close()
        except PresentationControllerError as exc:
            raise self._control_error(exc) from None
        return self.snapshot
