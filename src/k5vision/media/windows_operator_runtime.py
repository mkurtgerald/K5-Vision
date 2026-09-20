"""Bounded lifecycle joining accepted operator media and Windows viewport runtimes."""

from __future__ import annotations

import asyncio
import enum
import typing
from collections.abc import Callable, Sequence

from pydantic import BaseModel, ConfigDict, Field

from k5vision.media.mixed_presentation import MixedPresentationStream
from k5vision.media.presentation_runtime import (
    BoundedPresentationRuntime,
    PresentationRuntimeSnapshot,
    PresentationRuntimeState,
)
from k5vision.media.viewport_dispatch import ViewportBinding
from k5vision.media.viewport_geometry import ViewportLayout
from k5vision.media.windows_viewport_runtime import (
    BoundedWindowsViewportRuntime,
    WindowsViewportRuntimeSnapshot,
)

_MAX_VIEWPORTS = 16
_MAX_STREAMS = 16
_MAX_TOTAL_FRAMES = 1_000_000
_MAX_PRESENTATIONS = 1_000_000


class WindowsOperatorRuntimeState(enum.StrEnum):
    READY = "ready"
    RUNNING = "running"
    COMPLETE = "complete"
    STOPPED = "stopped"
    FAILED = "failed"
    CLOSED = "closed"


class WindowsOperatorRuntimeErrorCode(enum.StrEnum):
    INVALID_CONFIGURATION = "invalid_configuration"
    INVALID_STATE = "invalid_state"
    WINDOWS_OPEN_FAILURE = "windows_open_failure"
    ASSEMBLY_FAILURE = "assembly_failure"
    START_FAILURE = "start_failure"
    EXECUTION_FAILURE = "execution_failure"
    STOP_FAILURE = "stop_failure"
    CLEANUP_FAILURE = "cleanup_failure"


class WindowsOperatorRuntimeError(RuntimeError):
    """Sanitized operator-runtime failure."""

    def __init__(self, code: WindowsOperatorRuntimeErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class WindowsOperatorRuntimeSnapshot(BaseModel):
    """Source/media/path/native-identity-free operator observability."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: typing.Literal["1"] = "1"
    state: WindowsOperatorRuntimeState
    viewport_count: int = Field(ge=0, le=_MAX_VIEWPORTS)
    open_surface_count: int = Field(ge=0, le=_MAX_VIEWPORTS)
    stream_count: int = Field(ge=0, le=_MAX_STREAMS)
    delivered_frames: int = Field(ge=0, le=_MAX_TOTAL_FRAMES)
    presentations: int = Field(ge=0, le=_MAX_PRESENTATIONS)


@typing.runtime_checkable
class _WindowsRuntimeBoundary(typing.Protocol):
    @property
    def bindings(self) -> tuple[ViewportBinding, ...]: ...

    async def open(self) -> WindowsViewportRuntimeSnapshot: ...

    async def close(self) -> WindowsViewportRuntimeSnapshot: ...


@typing.runtime_checkable
class _PresentationRuntimeBoundary(typing.Protocol):
    async def start(
        self,
        streams: Sequence[MixedPresentationStream],
    ) -> PresentationRuntimeSnapshot: ...

    async def wait(self) -> PresentationRuntimeSnapshot: ...

    async def stop(self) -> PresentationRuntimeSnapshot: ...

    async def close(self) -> PresentationRuntimeSnapshot: ...


WindowsRuntimeFactory = Callable[[ViewportLayout], _WindowsRuntimeBoundary]
PresentationRuntimeFactory = Callable[
    [Sequence[ViewportBinding]],
    _PresentationRuntimeBoundary,
]


class BoundedWindowsOperatorRuntime:
    """Run one exact mixed stream plan through one arbitrary Windows viewport layout."""

    def __init__(
        self,
        layout: ViewportLayout,
        *,
        windows_runtime_factory: WindowsRuntimeFactory | None = None,
        presentation_runtime_factory: PresentationRuntimeFactory | None = None,
    ) -> None:
        if not isinstance(layout, ViewportLayout) or not 2 <= len(layout.placements) <= _MAX_VIEWPORTS:
            raise WindowsOperatorRuntimeError(
                WindowsOperatorRuntimeErrorCode.INVALID_CONFIGURATION,
                "operator runtime viewport layout is invalid",
            )
        self._layout = layout
        self._windows_runtime_factory = windows_runtime_factory or BoundedWindowsViewportRuntime
        self._presentation_runtime_factory = (
            presentation_runtime_factory or BoundedPresentationRuntime
        )
        self._state = WindowsOperatorRuntimeState.READY
        self._windows_runtime: _WindowsRuntimeBoundary | None = None
        self._presentation_runtime: _PresentationRuntimeBoundary | None = None
        self._windows_snapshot: WindowsViewportRuntimeSnapshot | None = None
        self._presentation_snapshot: PresentationRuntimeSnapshot | None = None
        self._lock = asyncio.Lock()

    @property
    def snapshot(self) -> WindowsOperatorRuntimeSnapshot:
        windows = self._windows_snapshot
        presentation = self._presentation_snapshot
        return WindowsOperatorRuntimeSnapshot(
            state=self._state,
            viewport_count=len(self._layout.placements),
            open_surface_count=0 if windows is None else windows.open_surface_count,
            stream_count=0 if presentation is None else presentation.stream_count,
            delivered_frames=0 if presentation is None else presentation.delivered_frames,
            presentations=0 if windows is None else windows.presentations,
        )

    async def _cleanup(self) -> bool:
        failed = False
        presentation = self._presentation_runtime
        self._presentation_runtime = None
        if presentation is not None:
            try:
                self._presentation_snapshot = await presentation.close()
            except asyncio.CancelledError:
                failed = True
            except Exception:
                failed = True

        windows = self._windows_runtime
        self._windows_runtime = None
        if windows is not None:
            try:
                self._windows_snapshot = await windows.close()
            except asyncio.CancelledError:
                failed = True
            except Exception:
                failed = True
        return failed

    async def _fail_closed(self) -> None:
        await self._cleanup()
        self._state = WindowsOperatorRuntimeState.FAILED

    async def start(
        self,
        streams: Sequence[MixedPresentationStream],
    ) -> WindowsOperatorRuntimeSnapshot:
        """Open Windows resources, assemble accepted media runtime, then start one plan."""
        async with self._lock:
            if self._state != WindowsOperatorRuntimeState.READY:
                raise WindowsOperatorRuntimeError(
                    WindowsOperatorRuntimeErrorCode.INVALID_STATE,
                    "operator runtime cannot start from current state",
                )

            try:
                windows = self._windows_runtime_factory(self._layout)
                if not isinstance(windows, _WindowsRuntimeBoundary):
                    raise TypeError("invalid Windows runtime boundary")
                self._windows_runtime = windows
                self._windows_snapshot = await windows.open()
            except asyncio.CancelledError:
                await self._fail_closed()
                raise
            except Exception:
                await self._fail_closed()
                raise WindowsOperatorRuntimeError(
                    WindowsOperatorRuntimeErrorCode.WINDOWS_OPEN_FAILURE,
                    "operator runtime Windows open failed",
                ) from None

            try:
                presentation = self._presentation_runtime_factory(windows.bindings)
                if not isinstance(presentation, _PresentationRuntimeBoundary):
                    raise TypeError("invalid presentation runtime boundary")
                self._presentation_runtime = presentation
            except Exception:
                await self._fail_closed()
                raise WindowsOperatorRuntimeError(
                    WindowsOperatorRuntimeErrorCode.ASSEMBLY_FAILURE,
                    "operator runtime assembly failed",
                ) from None

            try:
                self._presentation_snapshot = await presentation.start(streams)
            except asyncio.CancelledError:
                await self._fail_closed()
                raise
            except Exception:
                await self._fail_closed()
                raise WindowsOperatorRuntimeError(
                    WindowsOperatorRuntimeErrorCode.START_FAILURE,
                    "operator runtime start failed",
                ) from None

            self._state = WindowsOperatorRuntimeState.RUNNING
            return self.snapshot

    async def wait(self) -> WindowsOperatorRuntimeSnapshot:
        """Wait for the media runtime and preserve Windows resources until close."""
        async with self._lock:
            if self._state != WindowsOperatorRuntimeState.RUNNING:
                raise WindowsOperatorRuntimeError(
                    WindowsOperatorRuntimeErrorCode.INVALID_STATE,
                    "operator runtime is not running",
                )
            presentation = self._presentation_runtime
            if presentation is None:
                await self._fail_closed()
                raise WindowsOperatorRuntimeError(
                    WindowsOperatorRuntimeErrorCode.EXECUTION_FAILURE,
                    "operator runtime media boundary is unavailable",
                )
            try:
                self._presentation_snapshot = await presentation.wait()
            except asyncio.CancelledError:
                await self._fail_closed()
                raise
            except Exception:
                await self._fail_closed()
                raise WindowsOperatorRuntimeError(
                    WindowsOperatorRuntimeErrorCode.EXECUTION_FAILURE,
                    "operator runtime execution failed",
                ) from None

            if self._presentation_snapshot.state == PresentationRuntimeState.COMPLETE:
                self._state = WindowsOperatorRuntimeState.COMPLETE
            elif self._presentation_snapshot.state == PresentationRuntimeState.STOPPED:
                self._state = WindowsOperatorRuntimeState.STOPPED
            elif self._presentation_snapshot.state == PresentationRuntimeState.FAILED:
                await self._fail_closed()
                raise WindowsOperatorRuntimeError(
                    WindowsOperatorRuntimeErrorCode.EXECUTION_FAILURE,
                    "operator runtime child failed",
                )
            return self.snapshot

    async def stop(self) -> WindowsOperatorRuntimeSnapshot:
        """Stop media execution and release both media and Windows resources."""
        async with self._lock:
            if self._state != WindowsOperatorRuntimeState.RUNNING:
                raise WindowsOperatorRuntimeError(
                    WindowsOperatorRuntimeErrorCode.INVALID_STATE,
                    "operator runtime cannot stop from current state",
                )
            presentation = self._presentation_runtime
            if presentation is None:
                await self._fail_closed()
                raise WindowsOperatorRuntimeError(
                    WindowsOperatorRuntimeErrorCode.STOP_FAILURE,
                    "operator runtime media boundary is unavailable",
                )
            try:
                self._presentation_snapshot = await presentation.stop()
            except asyncio.CancelledError:
                await self._fail_closed()
                raise
            except Exception:
                await self._fail_closed()
                raise WindowsOperatorRuntimeError(
                    WindowsOperatorRuntimeErrorCode.STOP_FAILURE,
                    "operator runtime stop failed",
                ) from None

            failed = await self._cleanup()
            if failed:
                self._state = WindowsOperatorRuntimeState.FAILED
                raise WindowsOperatorRuntimeError(
                    WindowsOperatorRuntimeErrorCode.CLEANUP_FAILURE,
                    "operator runtime cleanup failed",
                )
            self._state = WindowsOperatorRuntimeState.STOPPED
            return self.snapshot

    async def close(self) -> WindowsOperatorRuntimeSnapshot:
        """Release both accepted child runtimes deterministically."""
        async with self._lock:
            if self._state == WindowsOperatorRuntimeState.CLOSED:
                return self.snapshot
            failed = await self._cleanup()
            if failed:
                self._state = WindowsOperatorRuntimeState.FAILED
                raise WindowsOperatorRuntimeError(
                    WindowsOperatorRuntimeErrorCode.CLEANUP_FAILURE,
                    "operator runtime cleanup failed",
                )
            self._state = WindowsOperatorRuntimeState.CLOSED
            return self.snapshot
