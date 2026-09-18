"""Bounded external controller for one accepted operator presentation session.

The controller provides the first project-owned start/wait/stop/close control surface over
the accepted Stage-30 session without selecting a renderer or UI toolkit. Source and
storage identities remain execution-only inside the child stream objects and are never
copied into controller state or errors.
"""

from __future__ import annotations

import asyncio
import enum
import typing
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field

from k5vision.media.mixed_presentation import MixedPresentationStream
from k5vision.media.presentation_session import (
    BoundedPresentationSession,
    PresentationSessionError,
)

_MAX_STREAMS = 16
_MAX_VIEWPORTS = 16
_MAX_TOTAL_FRAMES = 1_000_000
_MAX_TOTAL_FRAME_BYTES = 16 * 1024 * 1024 * 1024
_MAX_SOURCE_SPAN_MS = 2_147_483_647


class PresentationControllerState(enum.StrEnum):
    CREATED = "created"
    RUNNING = "running"
    COMPLETE = "complete"
    STOPPED = "stopped"
    FAILED = "failed"
    CLOSED = "closed"


class PresentationControllerErrorCode(enum.StrEnum):
    INVALID_STATE = "invalid_state"
    SESSION_FAILURE = "session_failure"
    STOP_TIMEOUT = "stop_timeout"
    CLEANUP_FAILURE = "cleanup_failure"


class PresentationControllerError(RuntimeError):
    """Sanitized operator presentation-controller failure."""

    def __init__(self, code: PresentationControllerErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class PresentationControllerSnapshot(BaseModel):
    """Source/path/identifier/payload-free controller observability."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: typing.Literal["1"] = "1"
    state: PresentationControllerState
    stream_count: int = Field(ge=0, le=_MAX_STREAMS)
    live_streams: int = Field(ge=0, le=_MAX_STREAMS)
    playback_streams: int = Field(ge=0, le=_MAX_STREAMS)
    viewport_count: int = Field(ge=0, le=_MAX_VIEWPORTS)
    completed_streams: int = Field(ge=0, le=_MAX_STREAMS)
    delivered_frames: int = Field(ge=0, le=_MAX_TOTAL_FRAMES)
    delivered_frame_bytes: int = Field(ge=0, le=_MAX_TOTAL_FRAME_BYTES)
    max_source_span_ms: int = Field(ge=0, le=_MAX_SOURCE_SPAN_MS)


class BoundedPresentationController:
    """Externally control exactly one bounded presentation session."""

    def __init__(
        self,
        session: BoundedPresentationSession,
        *,
        stop_timeout_seconds: float = 5.0,
    ) -> None:
        if not isinstance(session, BoundedPresentationSession):
            raise TypeError("session must be a bounded presentation session")
        if not 0.1 <= stop_timeout_seconds <= 30.0:
            raise ValueError("stop_timeout_seconds must be between 0.1 and 30.0")

        self._session = session
        self._stop_timeout_seconds = stop_timeout_seconds
        self._state = PresentationControllerState.CREATED
        self._task: asyncio.Task[object] | None = None
        self._lock = asyncio.Lock()

    @property
    def snapshot(self) -> PresentationControllerSnapshot:
        child = self._session.snapshot
        return PresentationControllerSnapshot(
            state=self._state,
            stream_count=child.stream_count,
            live_streams=child.live_streams,
            playback_streams=child.playback_streams,
            viewport_count=child.viewport_count,
            completed_streams=child.completed_streams,
            delivered_frames=child.delivered_frames,
            delivered_frame_bytes=child.delivered_frame_bytes,
            max_source_span_ms=child.max_source_span_ms,
        )

    async def start(
        self,
        streams: Sequence[MixedPresentationStream],
    ) -> PresentationControllerSnapshot:
        """Start one presentation asynchronously and return immediately."""
        async with self._lock:
            if self._state != PresentationControllerState.CREATED:
                raise PresentationControllerError(
                    PresentationControllerErrorCode.INVALID_STATE,
                    "presentation controller cannot start from current state",
                )
            selected = tuple(streams)
            self._state = PresentationControllerState.RUNNING
            self._task = asyncio.create_task(self._session.run(selected))
            return self.snapshot

    async def wait(self) -> PresentationControllerSnapshot:
        """Wait for the controlled presentation without propagating waiter cancellation."""
        async with self._lock:
            if self._state == PresentationControllerState.CREATED:
                raise PresentationControllerError(
                    PresentationControllerErrorCode.INVALID_STATE,
                    "presentation controller has not started",
                )
            task = self._task
            if task is None:
                return self.snapshot

        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if not task.cancelled():
                raise
            async with self._lock:
                if self._state == PresentationControllerState.RUNNING:
                    self._state = PresentationControllerState.STOPPED
                self._task = None
                return self.snapshot
        except PresentationSessionError:
            async with self._lock:
                self._state = PresentationControllerState.FAILED
                self._task = None
            raise PresentationControllerError(
                PresentationControllerErrorCode.SESSION_FAILURE,
                "presentation controller session failed",
            ) from None
        except Exception:
            async with self._lock:
                self._state = PresentationControllerState.FAILED
                self._task = None
            raise PresentationControllerError(
                PresentationControllerErrorCode.SESSION_FAILURE,
                "presentation controller session failed",
            ) from None

        async with self._lock:
            if self._state == PresentationControllerState.RUNNING:
                self._state = PresentationControllerState.COMPLETE
            self._task = None
            return self.snapshot

    async def stop(self) -> PresentationControllerSnapshot:
        """Cancel a running presentation and bound session cleanup."""
        async with self._lock:
            if self._state == PresentationControllerState.CREATED:
                raise PresentationControllerError(
                    PresentationControllerErrorCode.INVALID_STATE,
                    "presentation controller is not running",
                )
            if self._state != PresentationControllerState.RUNNING:
                return self.snapshot
            task = self._task
            if task is None:
                self._state = PresentationControllerState.FAILED
                raise PresentationControllerError(
                    PresentationControllerErrorCode.SESSION_FAILURE,
                    "presentation controller task is unavailable",
                )
            task.cancel()

        try:
            await asyncio.wait_for(asyncio.shield(task), self._stop_timeout_seconds)
        except asyncio.CancelledError:
            pass
        except TimeoutError:
            async with self._lock:
                self._state = PresentationControllerState.FAILED
            raise PresentationControllerError(
                PresentationControllerErrorCode.STOP_TIMEOUT,
                "presentation controller stop timed out",
            ) from None
        except Exception:
            async with self._lock:
                self._state = PresentationControllerState.FAILED
            raise PresentationControllerError(
                PresentationControllerErrorCode.SESSION_FAILURE,
                "presentation controller session failed while stopping",
            ) from None

        try:
            await asyncio.wait_for(self._session.close(), self._stop_timeout_seconds)
        except asyncio.CancelledError:
            raise
        except Exception:
            async with self._lock:
                self._state = PresentationControllerState.FAILED
                self._task = None
            raise PresentationControllerError(
                PresentationControllerErrorCode.CLEANUP_FAILURE,
                "presentation controller cleanup failed",
            ) from None

        async with self._lock:
            self._state = PresentationControllerState.STOPPED
            self._task = None
            return self.snapshot

    async def close(self) -> PresentationControllerSnapshot:
        """Release the controlled session, stopping it first when necessary."""
        async with self._lock:
            if self._state == PresentationControllerState.CLOSED:
                return self.snapshot
            running = self._state == PresentationControllerState.RUNNING

        if running:
            await self.stop()

        try:
            await asyncio.wait_for(self._session.close(), self._stop_timeout_seconds)
        except asyncio.CancelledError:
            raise
        except Exception:
            async with self._lock:
                self._state = PresentationControllerState.FAILED
                self._task = None
            raise PresentationControllerError(
                PresentationControllerErrorCode.CLEANUP_FAILURE,
                "presentation controller cleanup failed",
            ) from None

        async with self._lock:
            self._state = PresentationControllerState.CLOSED
            self._task = None
            return self.snapshot
