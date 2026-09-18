"""Bounded re-entrant host for accepted operator presentation runtimes.

The host adds process-lifetime re-entry above the accepted Stage-32 runtime without
selecting a renderer or UI toolkit. Caller-supplied stream plans remain execution-only:
they are handed to a fresh child runtime for each generation and are never copied into
host state, snapshots, or errors.
"""

from __future__ import annotations

import asyncio
import enum
import typing
from collections.abc import Callable, Sequence

from pydantic import BaseModel, ConfigDict, Field

from k5vision.media.mixed_presentation import MixedPresentationStream
from k5vision.media.presentation_runtime import (
    PresentationRuntimeError,
    PresentationRuntimeSnapshot,
    PresentationRuntimeState,
)

_MAX_GENERATIONS = 1024
_MAX_STREAMS = 16
_MAX_VIEWPORTS = 16
_MAX_TOTAL_FRAMES = 1_024_000_000
_MAX_TOTAL_FRAME_BYTES = 16 * 1024 * 1024 * 1024 * _MAX_GENERATIONS
_MAX_SOURCE_SPAN_MS = 2_147_483_647


@typing.runtime_checkable
class PresentationRuntimeBoundary(typing.Protocol):
    """Structural contract consumed by the process-lifetime host."""

    @property
    def snapshot(self) -> PresentationRuntimeSnapshot: ...

    async def start(
        self,
        streams: Sequence[MixedPresentationStream],
    ) -> PresentationRuntimeSnapshot: ...

    async def wait(self) -> PresentationRuntimeSnapshot: ...

    async def stop(self) -> PresentationRuntimeSnapshot: ...

    async def close(self) -> PresentationRuntimeSnapshot: ...


PresentationRuntimeFactory = Callable[[], PresentationRuntimeBoundary]


class PresentationHostState(enum.StrEnum):
    READY = "ready"
    RUNNING = "running"
    COMPLETE = "complete"
    STOPPED = "stopped"
    FAILED = "failed"
    CLOSED = "closed"


class PresentationHostErrorCode(enum.StrEnum):
    INVALID_CONFIGURATION = "invalid_configuration"
    INVALID_STATE = "invalid_state"
    GENERATION_LIMIT = "generation_limit"
    ASSEMBLY_FAILURE = "assembly_failure"
    START_FAILURE = "start_failure"
    EXECUTION_FAILURE = "execution_failure"
    CONTROL_FAILURE = "control_failure"
    CLEANUP_FAILURE = "cleanup_failure"


class PresentationHostError(RuntimeError):
    """Sanitized process-lifetime presentation-host failure."""

    def __init__(self, code: PresentationHostErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class PresentationHostSnapshot(BaseModel):
    """Source/path/identifier/payload-free host observability."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: typing.Literal["1"] = "1"
    state: PresentationHostState
    generation: int = Field(ge=0, le=_MAX_GENERATIONS)
    generation_limit: int = Field(ge=1, le=_MAX_GENERATIONS)
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


class BoundedPresentationHost:
    """Run fresh bounded presentation runtimes across a finite process lifetime."""

    def __init__(
        self,
        runtime_factory: PresentationRuntimeFactory,
        *,
        max_generations: int = 128,
        transition_timeout_seconds: float = 5.0,
    ) -> None:
        if not callable(runtime_factory):
            raise PresentationHostError(
                PresentationHostErrorCode.INVALID_CONFIGURATION,
                "presentation host runtime factory is invalid",
            )
        if not 1 <= max_generations <= _MAX_GENERATIONS:
            raise PresentationHostError(
                PresentationHostErrorCode.INVALID_CONFIGURATION,
                "presentation host generation bound is invalid",
            )
        if not 0.1 <= transition_timeout_seconds <= 30.0:
            raise PresentationHostError(
                PresentationHostErrorCode.INVALID_CONFIGURATION,
                "presentation host transition timeout is invalid",
            )

        self._runtime_factory = runtime_factory
        self._max_generations = max_generations
        self._transition_timeout_seconds = transition_timeout_seconds
        self._state = PresentationHostState.READY
        self._generation = 0
        self._completed_generations = 0
        self._stopped_generations = 0
        self._failures = 0
        self._runtime: PresentationRuntimeBoundary | None = None
        self._accounted_generation = 0
        self._delivered_frames = 0
        self._delivered_frame_bytes = 0
        self._max_source_span_ms = 0
        self._lock = asyncio.Lock()

    def _child_snapshot(self) -> PresentationRuntimeSnapshot | None:
        if self._runtime is None:
            return None
        return self._runtime.snapshot

    def _preview_totals(self) -> tuple[int, int, int]:
        frames = self._delivered_frames
        frame_bytes = self._delivered_frame_bytes
        max_span = self._max_source_span_ms
        child = self._child_snapshot()
        if child is not None and self._accounted_generation != self._generation:
            frames += child.delivered_frames
            frame_bytes += child.delivered_frame_bytes
            max_span = max(max_span, child.max_source_span_ms)
        return frames, frame_bytes, max_span

    @property
    def snapshot(self) -> PresentationHostSnapshot:
        child = self._child_snapshot()
        frames, frame_bytes, max_span = self._preview_totals()
        return PresentationHostSnapshot(
            state=self._state,
            generation=self._generation,
            generation_limit=self._max_generations,
            completed_generations=self._completed_generations,
            stopped_generations=self._stopped_generations,
            failures=self._failures,
            stream_count=0 if child is None else child.stream_count,
            live_streams=0 if child is None else child.live_streams,
            playback_streams=0 if child is None else child.playback_streams,
            viewport_count=0 if child is None else child.viewport_count,
            delivered_frames=frames,
            delivered_frame_bytes=frame_bytes,
            max_source_span_ms=max_span,
        )

    def _account_current(self) -> None:
        if self._runtime is None or self._accounted_generation == self._generation:
            return
        child = self._runtime.snapshot
        self._delivered_frames += child.delivered_frames
        self._delivered_frame_bytes += child.delivered_frame_bytes
        self._max_source_span_ms = max(self._max_source_span_ms, child.max_source_span_ms)
        if self._state == PresentationHostState.COMPLETE:
            self._completed_generations += 1
        elif self._state == PresentationHostState.STOPPED:
            self._stopped_generations += 1
        self._accounted_generation = self._generation

    def _fail(self) -> None:
        self._state = PresentationHostState.FAILED
        self._failures += 1

    async def _close_previous(self) -> None:
        if self._runtime is None:
            return
        self._account_current()
        try:
            await asyncio.wait_for(
                self._runtime.close(),
                timeout=self._transition_timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            self._fail()
            raise PresentationHostError(
                PresentationHostErrorCode.CLEANUP_FAILURE,
                "presentation host could not release the prior generation",
            ) from None
        self._runtime = None

    def _assemble_runtime(self) -> PresentationRuntimeBoundary:
        try:
            runtime = self._runtime_factory()
        except Exception:
            self._fail()
            raise PresentationHostError(
                PresentationHostErrorCode.ASSEMBLY_FAILURE,
                "presentation host runtime assembly failed",
            ) from None
        if not isinstance(runtime, PresentationRuntimeBoundary):
            self._fail()
            raise PresentationHostError(
                PresentationHostErrorCode.ASSEMBLY_FAILURE,
                "presentation host runtime assembly failed",
            )
        return runtime

    async def start(
        self,
        streams: Sequence[MixedPresentationStream],
    ) -> PresentationHostSnapshot:
        """Start one fresh generation and retain no caller stream plan."""
        async with self._lock:
            if self._state in {PresentationHostState.RUNNING, PresentationHostState.CLOSED}:
                raise PresentationHostError(
                    PresentationHostErrorCode.INVALID_STATE,
                    "presentation host cannot start from current state",
                )
            if self._generation >= self._max_generations:
                raise PresentationHostError(
                    PresentationHostErrorCode.GENERATION_LIMIT,
                    "presentation host generation limit reached",
                )

            await self._close_previous()
            runtime = self._assemble_runtime()
            self._generation += 1
            self._runtime = runtime
            try:
                await runtime.start(tuple(streams))
            except asyncio.CancelledError:
                self._fail()
                raise
            except PresentationRuntimeError:
                self._fail()
                raise PresentationHostError(
                    PresentationHostErrorCode.START_FAILURE,
                    "presentation host generation failed to start",
                ) from None
            except Exception:
                self._fail()
                raise PresentationHostError(
                    PresentationHostErrorCode.START_FAILURE,
                    "presentation host generation failed to start",
                ) from None

            self._state = PresentationHostState.RUNNING
            return self.snapshot

    async def wait(self) -> PresentationHostSnapshot:
        """Wait for the active generation to terminate."""
        async with self._lock:
            if self._state != PresentationHostState.RUNNING or self._runtime is None:
                raise PresentationHostError(
                    PresentationHostErrorCode.INVALID_STATE,
                    "presentation host has no running generation",
                )
            runtime = self._runtime

        try:
            child = await runtime.wait()
        except asyncio.CancelledError:
            raise
        except Exception:
            async with self._lock:
                if runtime is self._runtime:
                    self._fail()
                    self._account_current()
            raise PresentationHostError(
                PresentationHostErrorCode.EXECUTION_FAILURE,
                "presentation host generation failed",
            ) from None

        async with self._lock:
            if runtime is not self._runtime:
                raise PresentationHostError(
                    PresentationHostErrorCode.INVALID_STATE,
                    "presentation host generation changed during wait",
                )
            if self._state == PresentationHostState.CLOSED:
                return self.snapshot
            if child.state == PresentationRuntimeState.COMPLETE:
                self._state = PresentationHostState.COMPLETE
            elif child.state == PresentationRuntimeState.STOPPED:
                self._state = PresentationHostState.STOPPED
            else:
                self._fail()
                self._account_current()
                raise PresentationHostError(
                    PresentationHostErrorCode.EXECUTION_FAILURE,
                    "presentation host generation failed",
                )
            self._account_current()
            return self.snapshot

    async def stop(self) -> PresentationHostSnapshot:
        """Stop the active generation while preserving bounded aggregate counters."""
        async with self._lock:
            if self._state == PresentationHostState.READY:
                raise PresentationHostError(
                    PresentationHostErrorCode.INVALID_STATE,
                    "presentation host has no generation to stop",
                )
            if self._state == PresentationHostState.CLOSED:
                return self.snapshot
            if self._state != PresentationHostState.RUNNING:
                return self.snapshot
            runtime = self._runtime
            if runtime is None:
                self._fail()
                raise PresentationHostError(
                    PresentationHostErrorCode.CONTROL_FAILURE,
                    "presentation host active runtime is unavailable",
                )

        try:
            child = await asyncio.wait_for(
                runtime.stop(),
                timeout=self._transition_timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            async with self._lock:
                if runtime is self._runtime:
                    self._fail()
                    self._account_current()
            raise PresentationHostError(
                PresentationHostErrorCode.CONTROL_FAILURE,
                "presentation host stop failed",
            ) from None

        async with self._lock:
            if runtime is self._runtime:
                if child.state not in {
                    PresentationRuntimeState.STOPPED,
                    PresentationRuntimeState.COMPLETE,
                }:
                    self._fail()
                    self._account_current()
                    raise PresentationHostError(
                        PresentationHostErrorCode.CONTROL_FAILURE,
                        "presentation host stop failed",
                    )
                self._state = (
                    PresentationHostState.COMPLETE
                    if child.state == PresentationRuntimeState.COMPLETE
                    else PresentationHostState.STOPPED
                )
                self._account_current()
            return self.snapshot

    async def close(self) -> PresentationHostSnapshot:
        """Close the host and deterministically release the current generation."""
        async with self._lock:
            if self._state == PresentationHostState.CLOSED:
                return self.snapshot
            runtime = self._runtime
            running = self._state == PresentationHostState.RUNNING

        if runtime is not None and running:
            try:
                child = await asyncio.wait_for(
                    runtime.stop(),
                    timeout=self._transition_timeout_seconds,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                async with self._lock:
                    if runtime is self._runtime:
                        self._fail()
                        self._account_current()
                raise PresentationHostError(
                    PresentationHostErrorCode.CLEANUP_FAILURE,
                    "presentation host cleanup failed",
                ) from None
            async with self._lock:
                if runtime is self._runtime:
                    self._state = (
                        PresentationHostState.COMPLETE
                        if child.state == PresentationRuntimeState.COMPLETE
                        else PresentationHostState.STOPPED
                    )
                    self._account_current()

        if runtime is not None:
            async with self._lock:
                if runtime is self._runtime:
                    self._account_current()
            try:
                await asyncio.wait_for(
                    runtime.close(),
                    timeout=self._transition_timeout_seconds,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                async with self._lock:
                    if runtime is self._runtime:
                        self._fail()
                raise PresentationHostError(
                    PresentationHostErrorCode.CLEANUP_FAILURE,
                    "presentation host cleanup failed",
                ) from None

        async with self._lock:
            self._runtime = None
            self._state = PresentationHostState.CLOSED
            return self.snapshot
