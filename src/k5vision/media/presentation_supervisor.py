"""Bounded operator-style replacement control above the accepted presentation host.

The supervisor adds deterministic present/replace/wait/stop/close transitions without
selecting a renderer or UI toolkit. Caller stream plans remain execution-only and are
handed directly to the accepted host; no source, path, identifier, or payload data is
retained in supervisor state, snapshots, or errors.
"""

from __future__ import annotations

import asyncio
import enum
import typing
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field

from k5vision.media.mixed_presentation import MixedPresentationStream
from k5vision.media.presentation_host import PresentationHostSnapshot, PresentationHostState

_MAX_REPLACEMENTS = 1023
_MAX_GENERATIONS = 1024
_MAX_STREAMS = 16
_MAX_VIEWPORTS = 16
_MAX_TOTAL_FRAMES = 1_024_000_000
_MAX_TOTAL_FRAME_BYTES = 16 * 1024 * 1024 * 1024 * _MAX_GENERATIONS
_MAX_SOURCE_SPAN_MS = 2_147_483_647


@typing.runtime_checkable
class PresentationHostBoundary(typing.Protocol):
    """Structural Stage-33 host contract consumed by the supervisor."""

    @property
    def snapshot(self) -> PresentationHostSnapshot: ...

    async def start(
        self,
        streams: Sequence[MixedPresentationStream],
    ) -> PresentationHostSnapshot: ...

    async def wait(self) -> PresentationHostSnapshot: ...

    async def stop(self) -> PresentationHostSnapshot: ...

    async def close(self) -> PresentationHostSnapshot: ...


class PresentationSupervisorState(enum.StrEnum):
    READY = "ready"
    RUNNING = "running"
    COMPLETE = "complete"
    STOPPED = "stopped"
    FAILED = "failed"
    CLOSED = "closed"


class PresentationSupervisorErrorCode(enum.StrEnum):
    INVALID_CONFIGURATION = "invalid_configuration"
    INVALID_STATE = "invalid_state"
    REPLACEMENT_LIMIT = "replacement_limit"
    START_FAILURE = "start_failure"
    REPLACEMENT_FAILURE = "replacement_failure"
    EXECUTION_FAILURE = "execution_failure"
    CONTROL_FAILURE = "control_failure"
    CLEANUP_FAILURE = "cleanup_failure"


class PresentationSupervisorError(RuntimeError):
    """Sanitized presentation-supervisor failure."""

    def __init__(self, code: PresentationSupervisorErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class PresentationSupervisorSnapshot(BaseModel):
    """Source/path/identifier/payload-free supervisor observability."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: typing.Literal["1"] = "1"
    state: PresentationSupervisorState
    generation: int = Field(ge=0, le=_MAX_GENERATIONS)
    replacements: int = Field(ge=0, le=_MAX_REPLACEMENTS)
    replacement_limit: int = Field(ge=1, le=_MAX_REPLACEMENTS)
    completed_generations: int = Field(ge=0, le=_MAX_GENERATIONS)
    stopped_generations: int = Field(ge=0, le=_MAX_GENERATIONS)
    failures: int = Field(ge=0)
    stream_count: int = Field(ge=0, le=_MAX_STREAMS)
    live_streams: int = Field(ge=0, le=_MAX_STREAMS)
    playback_streams: int = Field(ge=0, le=_MAX_STREAMS)
    viewport_count: int = Field(ge=0, le=_MAX_VIEWPORTS)
    delivered_frames: int = Field(ge=0, le=_MAX_TOTAL_FRAMES)
    delivered_frame_bytes: int = Field(ge=0, le=_MAX_TOTAL_FRAME_BYTES)
    max_source_span_ms: int = Field(ge=0, le=_MAX_SOURCE_SPAN_MS)


_HOST_STATE_MAP: dict[PresentationHostState, PresentationSupervisorState] = {
    PresentationHostState.READY: PresentationSupervisorState.READY,
    PresentationHostState.RUNNING: PresentationSupervisorState.RUNNING,
    PresentationHostState.COMPLETE: PresentationSupervisorState.COMPLETE,
    PresentationHostState.STOPPED: PresentationSupervisorState.STOPPED,
    PresentationHostState.FAILED: PresentationSupervisorState.FAILED,
    PresentationHostState.CLOSED: PresentationSupervisorState.CLOSED,
}


class BoundedPresentationSupervisor:
    """Serialize bounded operator presentation and replacement transitions."""

    def __init__(
        self,
        host: PresentationHostBoundary,
        *,
        max_replacements: int = 127,
        transition_timeout_seconds: float = 10.0,
    ) -> None:
        if not isinstance(host, PresentationHostBoundary):
            raise PresentationSupervisorError(
                PresentationSupervisorErrorCode.INVALID_CONFIGURATION,
                "presentation supervisor host is invalid",
            )
        if not 1 <= max_replacements <= _MAX_REPLACEMENTS:
            raise PresentationSupervisorError(
                PresentationSupervisorErrorCode.INVALID_CONFIGURATION,
                "presentation supervisor replacement bound is invalid",
            )
        if not 0.1 <= transition_timeout_seconds <= 30.0:
            raise PresentationSupervisorError(
                PresentationSupervisorErrorCode.INVALID_CONFIGURATION,
                "presentation supervisor transition timeout is invalid",
            )

        self._host = host
        self._max_replacements = max_replacements
        self._transition_timeout_seconds = transition_timeout_seconds
        self._state = PresentationSupervisorState.READY
        self._replacements = 0
        self._lock = asyncio.Lock()

    @property
    def snapshot(self) -> PresentationSupervisorSnapshot:
        child = self._host.snapshot
        return PresentationSupervisorSnapshot(
            state=self._state,
            generation=child.generation,
            replacements=self._replacements,
            replacement_limit=self._max_replacements,
            completed_generations=child.completed_generations,
            stopped_generations=child.stopped_generations,
            failures=child.failures,
            stream_count=child.stream_count,
            live_streams=child.live_streams,
            playback_streams=child.playback_streams,
            viewport_count=child.viewport_count,
            delivered_frames=child.delivered_frames,
            delivered_frame_bytes=child.delivered_frame_bytes,
            max_source_span_ms=child.max_source_span_ms,
        )

    def _sync_state(self, child: PresentationHostSnapshot) -> None:
        self._state = _HOST_STATE_MAP[child.state]

    def _fail(self) -> None:
        self._state = PresentationSupervisorState.FAILED

    async def present(
        self,
        streams: Sequence[MixedPresentationStream],
    ) -> PresentationSupervisorSnapshot:
        """Start the first operator presentation and retain no stream plan."""
        async with self._lock:
            if self._state != PresentationSupervisorState.READY:
                raise PresentationSupervisorError(
                    PresentationSupervisorErrorCode.INVALID_STATE,
                    "presentation supervisor cannot present from current state",
                )
            try:
                child = await asyncio.wait_for(
                    self._host.start(tuple(streams)),
                    timeout=self._transition_timeout_seconds,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                self._fail()
                raise PresentationSupervisorError(
                    PresentationSupervisorErrorCode.START_FAILURE,
                    "presentation supervisor failed to start presentation",
                ) from None
            if child.state != PresentationHostState.RUNNING:
                self._fail()
                raise PresentationSupervisorError(
                    PresentationSupervisorErrorCode.START_FAILURE,
                    "presentation supervisor failed to start presentation",
                )
            self._state = PresentationSupervisorState.RUNNING
            return self.snapshot

    async def replace(
        self,
        streams: Sequence[MixedPresentationStream],
    ) -> PresentationSupervisorSnapshot:
        """Stop an active generation when needed and start a fresh replacement."""
        async with self._lock:
            if self._state in {
                PresentationSupervisorState.READY,
                PresentationSupervisorState.CLOSED,
            }:
                raise PresentationSupervisorError(
                    PresentationSupervisorErrorCode.INVALID_STATE,
                    "presentation supervisor cannot replace from current state",
                )
            if self._replacements >= self._max_replacements:
                raise PresentationSupervisorError(
                    PresentationSupervisorErrorCode.REPLACEMENT_LIMIT,
                    "presentation supervisor replacement limit reached",
                )

            if self._host.snapshot.state == PresentationHostState.RUNNING:
                try:
                    stopped = await asyncio.wait_for(
                        self._host.stop(),
                        timeout=self._transition_timeout_seconds,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self._fail()
                    raise PresentationSupervisorError(
                        PresentationSupervisorErrorCode.REPLACEMENT_FAILURE,
                        "presentation supervisor could not stop active presentation",
                    ) from None
                if stopped.state not in {
                    PresentationHostState.STOPPED,
                    PresentationHostState.COMPLETE,
                }:
                    self._fail()
                    raise PresentationSupervisorError(
                        PresentationSupervisorErrorCode.REPLACEMENT_FAILURE,
                        "presentation supervisor could not stop active presentation",
                    )

            try:
                child = await asyncio.wait_for(
                    self._host.start(tuple(streams)),
                    timeout=self._transition_timeout_seconds,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                self._fail()
                raise PresentationSupervisorError(
                    PresentationSupervisorErrorCode.REPLACEMENT_FAILURE,
                    "presentation supervisor failed to start replacement",
                ) from None
            if child.state != PresentationHostState.RUNNING:
                self._fail()
                raise PresentationSupervisorError(
                    PresentationSupervisorErrorCode.REPLACEMENT_FAILURE,
                    "presentation supervisor failed to start replacement",
                )

            self._replacements += 1
            self._state = PresentationSupervisorState.RUNNING
            return self.snapshot

    async def wait(self) -> PresentationSupervisorSnapshot:
        """Wait for the active generation under serialized operator control."""
        async with self._lock:
            if self._state != PresentationSupervisorState.RUNNING:
                raise PresentationSupervisorError(
                    PresentationSupervisorErrorCode.INVALID_STATE,
                    "presentation supervisor has no running presentation",
                )
            try:
                child = await self._host.wait()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._fail()
                raise PresentationSupervisorError(
                    PresentationSupervisorErrorCode.EXECUTION_FAILURE,
                    "presentation supervisor presentation failed",
                ) from None
            if child.state not in {
                PresentationHostState.COMPLETE,
                PresentationHostState.STOPPED,
            }:
                self._fail()
                raise PresentationSupervisorError(
                    PresentationSupervisorErrorCode.EXECUTION_FAILURE,
                    "presentation supervisor presentation failed",
                )
            self._sync_state(child)
            return self.snapshot

    async def stop(self) -> PresentationSupervisorSnapshot:
        """Boundedly stop the active operator presentation."""
        async with self._lock:
            if self._state == PresentationSupervisorState.READY:
                raise PresentationSupervisorError(
                    PresentationSupervisorErrorCode.INVALID_STATE,
                    "presentation supervisor has no presentation to stop",
                )
            if self._state == PresentationSupervisorState.CLOSED:
                return self.snapshot
            if self._state != PresentationSupervisorState.RUNNING:
                return self.snapshot
            try:
                child = await asyncio.wait_for(
                    self._host.stop(),
                    timeout=self._transition_timeout_seconds,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                self._fail()
                raise PresentationSupervisorError(
                    PresentationSupervisorErrorCode.CONTROL_FAILURE,
                    "presentation supervisor stop failed",
                ) from None
            if child.state not in {
                PresentationHostState.STOPPED,
                PresentationHostState.COMPLETE,
            }:
                self._fail()
                raise PresentationSupervisorError(
                    PresentationSupervisorErrorCode.CONTROL_FAILURE,
                    "presentation supervisor stop failed",
                )
            self._sync_state(child)
            return self.snapshot

    async def close(self) -> PresentationSupervisorSnapshot:
        """Close the supervisor and accepted host exactly once."""
        async with self._lock:
            if self._state == PresentationSupervisorState.CLOSED:
                return self.snapshot
            try:
                child = await asyncio.wait_for(
                    self._host.close(),
                    timeout=self._transition_timeout_seconds,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                self._fail()
                raise PresentationSupervisorError(
                    PresentationSupervisorErrorCode.CLEANUP_FAILURE,
                    "presentation supervisor cleanup failed",
                ) from None
            if child.state != PresentationHostState.CLOSED:
                self._fail()
                raise PresentationSupervisorError(
                    PresentationSupervisorErrorCode.CLEANUP_FAILURE,
                    "presentation supervisor cleanup failed",
                )
            self._state = PresentationSupervisorState.CLOSED
            return self.snapshot
