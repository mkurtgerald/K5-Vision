"""Bounded re-entrant host above the accepted Windows operator runtime."""

from __future__ import annotations

import asyncio
import enum
import typing
from collections.abc import Callable, Sequence

from pydantic import BaseModel, ConfigDict, Field

from k5vision.media.mixed_presentation import MixedPresentationStream
from k5vision.media.viewport_geometry import ViewportLayout
from k5vision.media.windows_operator_runtime import (
    BoundedWindowsOperatorRuntime,
    WindowsOperatorRuntimeSnapshot,
    WindowsOperatorRuntimeState,
)

_MAX_GENERATIONS = 1024
_MAX_VIEWPORTS = 16
_MAX_STREAMS = 16
_MAX_TOTAL_FRAMES = 1_024_000_000
_MAX_PRESENTATIONS = 1_024_000_000


class WindowsOperatorHostState(enum.StrEnum):
    READY = "ready"
    RUNNING = "running"
    COMPLETE = "complete"
    STOPPED = "stopped"
    FAILED = "failed"
    CLOSED = "closed"


class WindowsOperatorHostErrorCode(enum.StrEnum):
    INVALID_CONFIGURATION = "invalid_configuration"
    INVALID_STATE = "invalid_state"
    GENERATION_LIMIT = "generation_limit"
    ASSEMBLY_FAILURE = "assembly_failure"
    START_FAILURE = "start_failure"
    EXECUTION_FAILURE = "execution_failure"
    CONTROL_FAILURE = "control_failure"
    CLEANUP_FAILURE = "cleanup_failure"


class WindowsOperatorHostError(RuntimeError):
    """Sanitized process-lifetime operator-host failure."""

    def __init__(self, code: WindowsOperatorHostErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class WindowsOperatorHostSnapshot(BaseModel):
    """Source/media/path/native-identity-free host observability."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: typing.Literal["1"] = "1"
    state: WindowsOperatorHostState
    generation: int = Field(ge=0, le=_MAX_GENERATIONS)
    generation_limit: int = Field(ge=1, le=_MAX_GENERATIONS)
    completed_generations: int = Field(ge=0, le=_MAX_GENERATIONS)
    stopped_generations: int = Field(ge=0, le=_MAX_GENERATIONS)
    failures: int = Field(ge=0)
    viewport_count: int = Field(ge=0, le=_MAX_VIEWPORTS)
    open_surface_count: int = Field(ge=0, le=_MAX_VIEWPORTS)
    stream_count: int = Field(ge=0, le=_MAX_STREAMS)
    delivered_frames: int = Field(ge=0, le=_MAX_TOTAL_FRAMES)
    presentations: int = Field(ge=0, le=_MAX_PRESENTATIONS)


@typing.runtime_checkable
class _OperatorRuntimeBoundary(typing.Protocol):
    @property
    def snapshot(self) -> WindowsOperatorRuntimeSnapshot: ...

    async def start(
        self,
        streams: Sequence[MixedPresentationStream],
    ) -> WindowsOperatorRuntimeSnapshot: ...

    async def wait(self) -> WindowsOperatorRuntimeSnapshot: ...

    async def stop(self) -> WindowsOperatorRuntimeSnapshot: ...

    async def close(self) -> WindowsOperatorRuntimeSnapshot: ...


@typing.runtime_checkable
class _RelayoutRuntimeBoundary(typing.Protocol):
    async def relayout(self, layout: ViewportLayout) -> WindowsOperatorRuntimeSnapshot: ...


OperatorRuntimeFactory = Callable[[ViewportLayout], _OperatorRuntimeBoundary]


class BoundedWindowsOperatorHost:
    """Run and replace finite Windows operator generations without process restart."""

    def __init__(
        self,
        *,
        runtime_factory: OperatorRuntimeFactory | None = None,
        max_generations: int = 128,
        transition_timeout_seconds: float = 5.0,
    ) -> None:
        selected_factory = runtime_factory or BoundedWindowsOperatorRuntime
        if not callable(selected_factory):
            raise WindowsOperatorHostError(
                WindowsOperatorHostErrorCode.INVALID_CONFIGURATION,
                "operator host runtime factory is invalid",
            )
        if not 1 <= max_generations <= _MAX_GENERATIONS:
            raise WindowsOperatorHostError(
                WindowsOperatorHostErrorCode.INVALID_CONFIGURATION,
                "operator host generation bound is invalid",
            )
        if not 0.1 <= transition_timeout_seconds <= 30.0:
            raise WindowsOperatorHostError(
                WindowsOperatorHostErrorCode.INVALID_CONFIGURATION,
                "operator host transition timeout is invalid",
            )

        self._runtime_factory = selected_factory
        self._max_generations = max_generations
        self._transition_timeout_seconds = transition_timeout_seconds
        self._state = WindowsOperatorHostState.READY
        self._generation = 0
        self._completed_generations = 0
        self._stopped_generations = 0
        self._failures = 0
        self._runtime: _OperatorRuntimeBoundary | None = None
        self._accounted_generation = 0
        self._delivered_frames = 0
        self._presentations = 0
        self._lock = asyncio.Lock()

    def _child_snapshot(self) -> WindowsOperatorRuntimeSnapshot | None:
        if self._runtime is None:
            return None
        return self._runtime.snapshot

    def _preview_totals(self) -> tuple[int, int]:
        frames = self._delivered_frames
        presentations = self._presentations
        child = self._child_snapshot()
        if child is not None and self._accounted_generation != self._generation:
            frames += child.delivered_frames
            presentations += child.presentations
        return frames, presentations

    @property
    def snapshot(self) -> WindowsOperatorHostSnapshot:
        child = self._child_snapshot()
        frames, presentations = self._preview_totals()
        return WindowsOperatorHostSnapshot(
            state=self._state,
            generation=self._generation,
            generation_limit=self._max_generations,
            completed_generations=self._completed_generations,
            stopped_generations=self._stopped_generations,
            failures=self._failures,
            viewport_count=0 if child is None else child.viewport_count,
            open_surface_count=0 if child is None else child.open_surface_count,
            stream_count=0 if child is None else child.stream_count,
            delivered_frames=frames,
            presentations=presentations,
        )

    @staticmethod
    def _validate_layout(layout: ViewportLayout) -> None:
        if (
            not isinstance(layout, ViewportLayout)
            or not 2 <= len(layout.placements) <= _MAX_VIEWPORTS
        ):
            raise WindowsOperatorHostError(
                WindowsOperatorHostErrorCode.INVALID_CONFIGURATION,
                "operator host viewport layout is invalid",
            )

    def _fail(self) -> None:
        self._state = WindowsOperatorHostState.FAILED
        self._failures += 1

    def _account_current(self) -> None:
        if self._runtime is None or self._accounted_generation == self._generation:
            return
        child = self._runtime.snapshot
        self._delivered_frames += child.delivered_frames
        self._presentations += child.presentations
        if self._state == WindowsOperatorHostState.COMPLETE:
            self._completed_generations += 1
        elif self._state == WindowsOperatorHostState.STOPPED:
            self._stopped_generations += 1
        self._accounted_generation = self._generation

    def _assemble_runtime(self, layout: ViewportLayout) -> _OperatorRuntimeBoundary:
        try:
            runtime = self._runtime_factory(layout)
        except Exception:
            self._fail()
            raise WindowsOperatorHostError(
                WindowsOperatorHostErrorCode.ASSEMBLY_FAILURE,
                "operator host runtime assembly failed",
            ) from None
        if not isinstance(runtime, _OperatorRuntimeBoundary):
            self._fail()
            raise WindowsOperatorHostError(
                WindowsOperatorHostErrorCode.ASSEMBLY_FAILURE,
                "operator host runtime assembly failed",
            )
        return runtime

    async def _close_runtime_after_failure(self, runtime: _OperatorRuntimeBoundary) -> None:
        try:
            await asyncio.wait_for(
                runtime.close(),
                timeout=self._transition_timeout_seconds,
            )
        except BaseException:
            pass
        if runtime is self._runtime:
            self._runtime = None

    async def _release_current(self) -> None:
        runtime = self._runtime
        if runtime is None:
            return

        if self._state == WindowsOperatorHostState.RUNNING:
            try:
                child = await asyncio.wait_for(
                    runtime.stop(),
                    timeout=self._transition_timeout_seconds,
                )
            except asyncio.CancelledError:
                await self._close_runtime_after_failure(runtime)
                self._fail()
                raise
            except Exception:
                await self._close_runtime_after_failure(runtime)
                self._fail()
                raise WindowsOperatorHostError(
                    WindowsOperatorHostErrorCode.CLEANUP_FAILURE,
                    "operator host could not stop the prior generation",
                ) from None
            if child.state == WindowsOperatorRuntimeState.COMPLETE:
                self._state = WindowsOperatorHostState.COMPLETE
            elif child.state == WindowsOperatorRuntimeState.STOPPED:
                self._state = WindowsOperatorHostState.STOPPED
            else:
                await self._close_runtime_after_failure(runtime)
                self._fail()
                raise WindowsOperatorHostError(
                    WindowsOperatorHostErrorCode.CLEANUP_FAILURE,
                    "operator host prior generation did not stop safely",
                )

        self._account_current()
        try:
            await asyncio.wait_for(
                runtime.close(),
                timeout=self._transition_timeout_seconds,
            )
        except asyncio.CancelledError:
            self._runtime = None
            self._fail()
            raise
        except Exception:
            self._runtime = None
            self._fail()
            raise WindowsOperatorHostError(
                WindowsOperatorHostErrorCode.CLEANUP_FAILURE,
                "operator host could not release the prior generation",
            ) from None
        self._runtime = None

    async def _start_generation(
        self,
        layout: ViewportLayout,
        streams: Sequence[MixedPresentationStream],
    ) -> WindowsOperatorHostSnapshot:
        if self._generation >= self._max_generations:
            raise WindowsOperatorHostError(
                WindowsOperatorHostErrorCode.GENERATION_LIMIT,
                "operator host generation limit reached",
            )
        runtime = self._assemble_runtime(layout)
        self._generation += 1
        self._runtime = runtime
        try:
            await runtime.start(tuple(streams))
        except asyncio.CancelledError:
            await self._close_runtime_after_failure(runtime)
            self._fail()
            raise
        except Exception:
            await self._close_runtime_after_failure(runtime)
            self._fail()
            raise WindowsOperatorHostError(
                WindowsOperatorHostErrorCode.START_FAILURE,
                "operator host generation failed to start",
            ) from None
        self._state = WindowsOperatorHostState.RUNNING
        return self.snapshot

    async def start(
        self,
        layout: ViewportLayout,
        streams: Sequence[MixedPresentationStream],
    ) -> WindowsOperatorHostSnapshot:
        """Start the first operator generation."""
        self._validate_layout(layout)
        async with self._lock:
            if self._state != WindowsOperatorHostState.READY:
                raise WindowsOperatorHostError(
                    WindowsOperatorHostErrorCode.INVALID_STATE,
                    "operator host cannot start from current state",
                )
            return await self._start_generation(layout, streams)

    async def replace(
        self,
        layout: ViewportLayout,
        streams: Sequence[MixedPresentationStream],
    ) -> WindowsOperatorHostSnapshot:
        """Release the current generation before opening its replacement."""
        self._validate_layout(layout)
        async with self._lock:
            if self._state not in {
                WindowsOperatorHostState.RUNNING,
                WindowsOperatorHostState.COMPLETE,
                WindowsOperatorHostState.STOPPED,
            }:
                raise WindowsOperatorHostError(
                    WindowsOperatorHostErrorCode.INVALID_STATE,
                    "operator host cannot replace from current state",
                )
            if self._generation >= self._max_generations:
                raise WindowsOperatorHostError(
                    WindowsOperatorHostErrorCode.GENERATION_LIMIT,
                    "operator host generation limit reached",
                )
            await self._release_current()
            return await self._start_generation(layout, streams)

    async def relayout(self, layout: ViewportLayout) -> WindowsOperatorHostSnapshot:
        """Apply source-free geometry to the active generation without replacing it."""
        self._validate_layout(layout)
        async with self._lock:
            if self._state != WindowsOperatorHostState.RUNNING or self._runtime is None:
                raise WindowsOperatorHostError(
                    WindowsOperatorHostErrorCode.INVALID_STATE,
                    "operator host has no running generation to relayout",
                )
            runtime = self._runtime
            if not isinstance(runtime, _RelayoutRuntimeBoundary):
                await self._close_runtime_after_failure(runtime)
                self._fail()
                raise WindowsOperatorHostError(
                    WindowsOperatorHostErrorCode.CONTROL_FAILURE,
                    "operator host relayout boundary is unavailable",
                )
            try:
                child = await asyncio.wait_for(
                    runtime.relayout(layout),
                    timeout=self._transition_timeout_seconds,
                )
            except asyncio.CancelledError:
                await self._close_runtime_after_failure(runtime)
                self._fail()
                raise
            except Exception:
                await self._close_runtime_after_failure(runtime)
                self._fail()
                raise WindowsOperatorHostError(
                    WindowsOperatorHostErrorCode.CONTROL_FAILURE,
                    "operator host relayout failed",
                ) from None
            if child.state != WindowsOperatorRuntimeState.RUNNING:
                await self._close_runtime_after_failure(runtime)
                self._fail()
                raise WindowsOperatorHostError(
                    WindowsOperatorHostErrorCode.CONTROL_FAILURE,
                    "operator host relayout did not preserve running state",
                )
            return self.snapshot

    async def wait(self) -> WindowsOperatorHostSnapshot:
        """Wait for the active operator generation to terminate."""
        async with self._lock:
            if self._state != WindowsOperatorHostState.RUNNING or self._runtime is None:
                raise WindowsOperatorHostError(
                    WindowsOperatorHostErrorCode.INVALID_STATE,
                    "operator host has no running generation",
                )
            runtime = self._runtime

        try:
            child = await runtime.wait()
        except asyncio.CancelledError:
            raise
        except Exception:
            async with self._lock:
                if runtime is self._runtime:
                    await self._close_runtime_after_failure(runtime)
                    self._fail()
            raise WindowsOperatorHostError(
                WindowsOperatorHostErrorCode.EXECUTION_FAILURE,
                "operator host generation failed",
            ) from None

        async with self._lock:
            if runtime is not self._runtime:
                raise WindowsOperatorHostError(
                    WindowsOperatorHostErrorCode.INVALID_STATE,
                    "operator host generation changed during wait",
                )
            if child.state == WindowsOperatorRuntimeState.COMPLETE:
                self._state = WindowsOperatorHostState.COMPLETE
            elif child.state == WindowsOperatorRuntimeState.STOPPED:
                self._state = WindowsOperatorHostState.STOPPED
            else:
                await self._close_runtime_after_failure(runtime)
                self._fail()
                raise WindowsOperatorHostError(
                    WindowsOperatorHostErrorCode.EXECUTION_FAILURE,
                    "operator host generation failed",
                )
            self._account_current()
            return self.snapshot

    async def stop(self) -> WindowsOperatorHostSnapshot:
        """Stop the active generation; the child releases media and Windows resources."""
        async with self._lock:
            if self._state == WindowsOperatorHostState.CLOSED:
                return self.snapshot
            if self._state != WindowsOperatorHostState.RUNNING or self._runtime is None:
                raise WindowsOperatorHostError(
                    WindowsOperatorHostErrorCode.INVALID_STATE,
                    "operator host has no running generation",
                )
            runtime = self._runtime

        try:
            child = await asyncio.wait_for(
                runtime.stop(),
                timeout=self._transition_timeout_seconds,
            )
        except asyncio.CancelledError:
            async with self._lock:
                if runtime is self._runtime:
                    await self._close_runtime_after_failure(runtime)
                    self._fail()
            raise
        except Exception:
            async with self._lock:
                if runtime is self._runtime:
                    await self._close_runtime_after_failure(runtime)
                    self._fail()
            raise WindowsOperatorHostError(
                WindowsOperatorHostErrorCode.CONTROL_FAILURE,
                "operator host stop failed",
            ) from None

        async with self._lock:
            if runtime is not self._runtime:
                raise WindowsOperatorHostError(
                    WindowsOperatorHostErrorCode.INVALID_STATE,
                    "operator host generation changed during stop",
                )
            if child.state == WindowsOperatorRuntimeState.COMPLETE:
                self._state = WindowsOperatorHostState.COMPLETE
            elif child.state == WindowsOperatorRuntimeState.STOPPED:
                self._state = WindowsOperatorHostState.STOPPED
            else:
                await self._close_runtime_after_failure(runtime)
                self._fail()
                raise WindowsOperatorHostError(
                    WindowsOperatorHostErrorCode.CONTROL_FAILURE,
                    "operator host stop failed",
                )
            self._account_current()
            return self.snapshot

    async def close(self) -> WindowsOperatorHostSnapshot:
        """Close the host and deterministically release its current generation."""
        async with self._lock:
            if self._state == WindowsOperatorHostState.CLOSED:
                return self.snapshot
            if self._state == WindowsOperatorHostState.READY:
                self._state = WindowsOperatorHostState.CLOSED
                return self.snapshot
            await self._release_current()
            self._state = WindowsOperatorHostState.CLOSED
            return self.snapshot
