"""Bounded operator-facing presentation session over accepted media boundaries.

The session composes the accepted mixed live/playback coordinator with the accepted
renderer-neutral viewport dispatcher. Source and storage identities remain execution-
only in child inputs; the session retains aggregate lifecycle and counters only.
"""

from __future__ import annotations

import asyncio
import enum
import typing
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field

from k5vision.media.mixed_presentation import (
    BoundedMixedPresentation,
    MixedLiveStream,
    MixedPlaybackStream,
    MixedPresentationError,
    MixedPresentationStream,
)
from k5vision.media.viewport_dispatch import BoundedViewportDispatcher

_MAX_STREAMS = 16
_MAX_VIEWPORTS = 16
_MAX_TOTAL_FRAMES = 1_000_000
_MAX_TOTAL_FRAME_BYTES = 16 * 1024 * 1024 * 1024
_MAX_SOURCE_SPAN_MS = 2_147_483_647


class PresentationSessionState(enum.StrEnum):
    CREATED = "created"
    RUNNING = "running"
    COMPLETE = "complete"
    CANCELLED = "cancelled"
    FAILED = "failed"
    CLOSED = "closed"


class PresentationSessionErrorCode(enum.StrEnum):
    INVALID_STATE = "invalid_state"
    PRESENTATION_FAILURE = "presentation_failure"
    CLEANUP_FAILURE = "cleanup_failure"


class PresentationSessionError(RuntimeError):
    """Sanitized operator presentation-session failure."""

    def __init__(self, code: PresentationSessionErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class PresentationSessionSnapshot(BaseModel):
    """Source/path/identifier/payload-free operator-session observability."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: typing.Literal["1"] = "1"
    state: PresentationSessionState
    stream_count: int = Field(ge=0, le=_MAX_STREAMS)
    live_streams: int = Field(ge=0, le=_MAX_STREAMS)
    playback_streams: int = Field(ge=0, le=_MAX_STREAMS)
    viewport_count: int = Field(ge=0, le=_MAX_VIEWPORTS)
    completed_streams: int = Field(ge=0, le=_MAX_STREAMS)
    delivered_frames: int = Field(ge=0, le=_MAX_TOTAL_FRAMES)
    delivered_frame_bytes: int = Field(ge=0, le=_MAX_TOTAL_FRAME_BYTES)
    max_source_span_ms: int = Field(ge=0, le=_MAX_SOURCE_SPAN_MS)


class BoundedPresentationSession:
    """Run one bounded mixed presentation through a transient viewport dispatcher."""

    def __init__(
        self,
        coordinator: BoundedMixedPresentation,
        dispatcher: BoundedViewportDispatcher,
    ) -> None:
        if not isinstance(coordinator, BoundedMixedPresentation):
            raise TypeError("coordinator must be a bounded mixed presentation")
        if not isinstance(dispatcher, BoundedViewportDispatcher):
            raise TypeError("dispatcher must be a bounded viewport dispatcher")

        self._coordinator = coordinator
        self._dispatcher = dispatcher
        self._state = PresentationSessionState.CREATED
        self._stream_count = 0
        self._live_streams = 0
        self._playback_streams = 0
        self._viewport_count = dispatcher.snapshot.viewport_count
        self._completed_streams = 0
        self._delivered_frames = 0
        self._delivered_frame_bytes = 0
        self._max_source_span_ms = 0
        self._run_lock = asyncio.Lock()

    @property
    def snapshot(self) -> PresentationSessionSnapshot:
        return PresentationSessionSnapshot(
            state=self._state,
            stream_count=self._stream_count,
            live_streams=self._live_streams,
            playback_streams=self._playback_streams,
            viewport_count=self._viewport_count,
            completed_streams=self._completed_streams,
            delivered_frames=self._delivered_frames,
            delivered_frame_bytes=self._delivered_frame_bytes,
            max_source_span_ms=self._max_source_span_ms,
        )

    def _capture_aggregate_state(self) -> None:
        mixed = self._coordinator.snapshot
        viewport = self._dispatcher.snapshot
        self._stream_count = max(self._stream_count, mixed.stream_count)
        self._live_streams = max(self._live_streams, mixed.live_streams)
        self._playback_streams = max(self._playback_streams, mixed.playback_streams)
        self._viewport_count = viewport.viewport_count
        self._completed_streams = mixed.completed_streams
        self._delivered_frames = viewport.delivered_frames
        self._delivered_frame_bytes = viewport.delivered_frame_bytes
        self._max_source_span_ms = max(
            mixed.max_source_span_ms,
            viewport.max_source_span_ms,
        )

    async def _close_dispatcher(self) -> None:
        try:
            await self._dispatcher.close()
        except asyncio.CancelledError:
            raise
        except Exception:
            raise PresentationSessionError(
                PresentationSessionErrorCode.CLEANUP_FAILURE,
                "presentation session cleanup failed",
            ) from None
        finally:
            self._capture_aggregate_state()

    async def run(
        self,
        streams: Sequence[MixedPresentationStream],
    ) -> PresentationSessionSnapshot:
        """Run one operator presentation session and always release viewport bindings."""
        async with self._run_lock:
            if self._state != PresentationSessionState.CREATED:
                raise PresentationSessionError(
                    PresentationSessionErrorCode.INVALID_STATE,
                    "presentation session cannot run from current state",
                )

            selected = tuple(streams)
            self._stream_count = len(selected)
            self._live_streams = sum(isinstance(stream, MixedLiveStream) for stream in selected)
            self._playback_streams = sum(
                isinstance(stream, MixedPlaybackStream) for stream in selected
            )
            self._state = PresentationSessionState.RUNNING

            try:
                await self._coordinator.run(selected, self._dispatcher.dispatch)
            except asyncio.CancelledError:
                try:
                    await self._close_dispatcher()
                finally:
                    self._state = PresentationSessionState.CANCELLED
                raise
            except MixedPresentationError:
                try:
                    await self._close_dispatcher()
                except PresentationSessionError:
                    self._state = PresentationSessionState.FAILED
                    raise
                self._state = PresentationSessionState.FAILED
                raise PresentationSessionError(
                    PresentationSessionErrorCode.PRESENTATION_FAILURE,
                    "presentation session failed",
                ) from None
            except Exception:
                try:
                    await self._close_dispatcher()
                except PresentationSessionError:
                    self._state = PresentationSessionState.FAILED
                    raise
                self._state = PresentationSessionState.FAILED
                raise PresentationSessionError(
                    PresentationSessionErrorCode.PRESENTATION_FAILURE,
                    "presentation session failed",
                ) from None

            try:
                await self._close_dispatcher()
            except PresentationSessionError:
                self._state = PresentationSessionState.FAILED
                raise
            self._state = PresentationSessionState.COMPLETE
            return self.snapshot

    async def close(self) -> PresentationSessionSnapshot:
        """Close an unused session or reassert resource release for a terminal session."""
        async with self._run_lock:
            if self._state == PresentationSessionState.RUNNING:
                raise PresentationSessionError(
                    PresentationSessionErrorCode.INVALID_STATE,
                    "running presentation session cannot be closed externally",
                )
            await self._close_dispatcher()
            if self._state == PresentationSessionState.CREATED:
                self._state = PresentationSessionState.CLOSED
            return self.snapshot
