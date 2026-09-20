"""Bounded live control above the accepted visible operator session."""

from __future__ import annotations

import asyncio
import enum
import typing
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

from k5vision.media.mixed_presentation import MixedPresentationStream
from k5vision.media.viewport_geometry import ViewportLayout
from k5vision.media.windows_operator_application import (
    WindowsOperatorApplicationSnapshot,
    WindowsOperatorApplicationState,
)
from k5vision.media.windows_operator_session import (
    ApplicationFactory,
    BoundedWindowsOperatorSession,
    WindowsOperatorSessionError,
    WindowsOperatorSessionErrorCode,
    WindowsOperatorSessionSnapshot,
    WindowsOperatorSessionState,
)

_MAX_PENDING_CONTROLS = 64
_MAX_CONTROLS_PER_CYCLE = 16


class WindowsOperatorControlErrorCode(enum.StrEnum):
    INVALID_CONFIGURATION = "invalid_configuration"
    INVALID_STATE = "invalid_state"
    CONTROL_LIMIT = "control_limit"
    APPLICATION_FAILURE = "application_failure"


class WindowsOperatorControlError(RuntimeError):
    """Sanitized live-control failure without media or native identity."""

    def __init__(self, code: WindowsOperatorControlErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class WindowsOperatorControlSnapshot(BaseModel):
    """Aggregate privacy-safe observability for one controllable session."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: typing.Literal["1"] = "1"
    session: WindowsOperatorSessionSnapshot
    queued_controls: int = Field(ge=0, le=_MAX_PENDING_CONTROLS)
    processed_controls: int = Field(ge=0)
    replacements: int = Field(ge=0)
    stop_requests: int = Field(ge=0)


class _ControlKind(enum.StrEnum):
    REPLACE = "replace"
    STOP = "stop"


@dataclass(frozen=True, slots=True)
class _ControlRequest:
    kind: _ControlKind
    layout: ViewportLayout | None = None
    streams: tuple[MixedPresentationStream, ...] = ()


@typing.runtime_checkable
class _ControllableApplicationBoundary(typing.Protocol):
    @property
    def snapshot(self) -> WindowsOperatorApplicationSnapshot: ...

    async def open(self, width: int, height: int) -> WindowsOperatorApplicationSnapshot: ...

    async def start(
        self,
        layout: ViewportLayout,
        streams: Sequence[MixedPresentationStream],
    ) -> WindowsOperatorApplicationSnapshot: ...

    async def replace(
        self,
        layout: ViewportLayout,
        streams: Sequence[MixedPresentationStream],
    ) -> WindowsOperatorApplicationSnapshot: ...

    async def wait(self) -> WindowsOperatorApplicationSnapshot: ...

    async def stop(self) -> WindowsOperatorApplicationSnapshot: ...

    async def pump(
        self,
        *,
        max_messages: int = 64,
    ) -> WindowsOperatorApplicationSnapshot: ...

    async def close(self) -> WindowsOperatorApplicationSnapshot: ...


class BoundedWindowsOperatorControl(BoundedWindowsOperatorSession):
    """Run one visible session while accepting bounded replace/stop requests."""

    def __init__(
        self,
        *,
        application_factory: ApplicationFactory | None = None,
        max_cycles: int = 100_000,
        max_messages_per_cycle: int = 64,
        poll_interval_seconds: float = 0.01,
        cleanup_timeout_seconds: float = 5.0,
        max_pending_controls: int = 16,
        max_controls_per_cycle: int = 4,
    ) -> None:
        if not 1 <= max_pending_controls <= _MAX_PENDING_CONTROLS:
            raise WindowsOperatorControlError(
                WindowsOperatorControlErrorCode.INVALID_CONFIGURATION,
                "operator control queue bound is invalid",
            )
        if not 1 <= max_controls_per_cycle <= _MAX_CONTROLS_PER_CYCLE:
            raise WindowsOperatorControlError(
                WindowsOperatorControlErrorCode.INVALID_CONFIGURATION,
                "operator controls-per-cycle bound is invalid",
            )
        super().__init__(
            application_factory=application_factory,
            max_cycles=max_cycles,
            max_messages_per_cycle=max_messages_per_cycle,
            poll_interval_seconds=poll_interval_seconds,
            cleanup_timeout_seconds=cleanup_timeout_seconds,
        )
        self._max_pending_controls = max_pending_controls
        self._max_controls_per_cycle = max_controls_per_cycle
        self._controls: asyncio.Queue[_ControlRequest] = asyncio.Queue(
            maxsize=max_pending_controls
        )
        self._processed_controls = 0
        self._replacements = 0
        self._stop_requests = 0

    @property
    def control_snapshot(self) -> WindowsOperatorControlSnapshot:
        return WindowsOperatorControlSnapshot(
            session=super().snapshot,
            queued_controls=self._controls.qsize(),
            processed_controls=self._processed_controls,
            replacements=self._replacements,
            stop_requests=self._stop_requests,
        )

    def _enqueue(self, request: _ControlRequest) -> WindowsOperatorControlSnapshot:
        if self._state != WindowsOperatorSessionState.RUNNING or self._application is None:
            raise WindowsOperatorControlError(
                WindowsOperatorControlErrorCode.INVALID_STATE,
                "operator control requires a running session",
            )
        try:
            self._controls.put_nowait(request)
        except asyncio.QueueFull:
            raise WindowsOperatorControlError(
                WindowsOperatorControlErrorCode.CONTROL_LIMIT,
                "operator control queue limit reached",
            ) from None
        return self.control_snapshot

    def request_replace(
        self,
        layout: ViewportLayout,
        streams: Sequence[MixedPresentationStream],
    ) -> WindowsOperatorControlSnapshot:
        """Queue one same-shell arbitrary-layout replacement."""
        return self._enqueue(
            _ControlRequest(
                kind=_ControlKind.REPLACE,
                layout=layout,
                streams=tuple(streams),
            )
        )

    def request_stop(self) -> WindowsOperatorControlSnapshot:
        """Queue one explicit session stop request."""
        return self._enqueue(_ControlRequest(kind=_ControlKind.STOP))

    def _drain_controls(self) -> None:
        while True:
            try:
                self._controls.get_nowait()
            except asyncio.QueueEmpty:
                return

    async def _process_controls(
        self,
        application: _ControllableApplicationBoundary,
        wait_task: asyncio.Task[WindowsOperatorApplicationSnapshot] | None,
    ) -> tuple[
        asyncio.Task[WindowsOperatorApplicationSnapshot] | None,
        WindowsOperatorSessionState | None,
    ]:
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

            if request.layout is None:
                raise WindowsOperatorControlError(
                    WindowsOperatorControlErrorCode.APPLICATION_FAILURE,
                    "operator replacement request is invalid",
                )
            await self._cancel_wait_task(wait_task)
            self._application_snapshot = await application.replace(
                request.layout,
                request.streams,
            )
            self._processed_controls += 1
            self._replacements += 1
            wait_task = asyncio.create_task(application.wait())

        return wait_task, None

    async def run(
        self,
        *,
        width: int,
        height: int,
        layout: ViewportLayout,
        streams: Sequence[MixedPresentationStream],
    ) -> WindowsOperatorControlSnapshot:
        """Run one bounded session and serialize live control with its message pump."""
        async with self._lock:
            if self._state != WindowsOperatorSessionState.READY:
                raise WindowsOperatorSessionError(
                    WindowsOperatorSessionErrorCode.INVALID_STATE,
                    "operator session cannot run from current state",
                )
            try:
                application = self._assemble_application()
            except WindowsOperatorSessionError:
                self._state = WindowsOperatorSessionState.FAILED
                raise
            if not isinstance(application, _ControllableApplicationBoundary):
                self._state = WindowsOperatorSessionState.FAILED
                raise WindowsOperatorControlError(
                    WindowsOperatorControlErrorCode.APPLICATION_FAILURE,
                    "operator session application does not support live control",
                )
            self._application = application
            self._state = WindowsOperatorSessionState.RUNNING

        wait_task: asyncio.Task[WindowsOperatorApplicationSnapshot] | None = None
        terminal_state: WindowsOperatorSessionState | None = None
        try:
            self._application_snapshot = await application.open(width, height)
            self._application_snapshot = await application.start(layout, streams)
            wait_task = asyncio.create_task(application.wait())

            while self._cycles < self._max_cycles:
                wait_task, control_terminal = await self._process_controls(
                    application,
                    wait_task,
                )
                if control_terminal is not None:
                    terminal_state = control_terminal
                    break

                self._application_snapshot = await application.pump(
                    max_messages=self._max_messages_per_cycle,
                )
                self._cycles += 1

                if self._application_snapshot.state == WindowsOperatorApplicationState.CLOSED:
                    terminal_state = WindowsOperatorSessionState.USER_CLOSED
                    await self._cancel_wait_task(wait_task)
                    break

                if wait_task is not None and wait_task.done():
                    self._application_snapshot = await wait_task
                    if self._application_snapshot.state in {
                        WindowsOperatorApplicationState.COMPLETE,
                        WindowsOperatorApplicationState.STOPPED,
                    }:
                        terminal_state = WindowsOperatorSessionState.COMPLETE
                        break
                    raise WindowsOperatorSessionError(
                        WindowsOperatorSessionErrorCode.APPLICATION_FAILURE,
                        "operator session application reached an invalid terminal state",
                    )

                if self._poll_interval_seconds:
                    await asyncio.sleep(self._poll_interval_seconds)
                else:
                    await asyncio.sleep(0)

            if terminal_state is None:
                await self._cancel_wait_task(wait_task)
                if application.snapshot.state == WindowsOperatorApplicationState.RUNNING:
                    try:
                        self._application_snapshot = await application.stop()
                    except Exception:
                        pass
                raise WindowsOperatorSessionError(
                    WindowsOperatorSessionErrorCode.SESSION_LIMIT,
                    "operator session cycle limit reached",
                )
        except asyncio.CancelledError:
            await self._cancel_wait_task(wait_task)
            cleanup_failed = await self._close_application()
            self._state = (
                WindowsOperatorSessionState.FAILED
                if cleanup_failed
                else WindowsOperatorSessionState.CANCELLED
            )
            raise
        except (WindowsOperatorSessionError, WindowsOperatorControlError):
            await self._cancel_wait_task(wait_task)
            cleanup_failed = await self._close_application()
            self._state = WindowsOperatorSessionState.FAILED
            if cleanup_failed:
                raise WindowsOperatorSessionError(
                    WindowsOperatorSessionErrorCode.CLEANUP_FAILURE,
                    "operator session cleanup failed",
                ) from None
            raise
        except Exception:
            await self._cancel_wait_task(wait_task)
            cleanup_failed = await self._close_application()
            self._state = WindowsOperatorSessionState.FAILED
            code = (
                WindowsOperatorSessionErrorCode.CLEANUP_FAILURE
                if cleanup_failed
                else WindowsOperatorSessionErrorCode.APPLICATION_FAILURE
            )
            message = (
                "operator session cleanup failed"
                if cleanup_failed
                else "operator session application failed"
            )
            raise WindowsOperatorSessionError(code, message) from None
        finally:
            self._drain_controls()

        cleanup_failed = await self._close_application()
        if cleanup_failed:
            self._state = WindowsOperatorSessionState.FAILED
            raise WindowsOperatorSessionError(
                WindowsOperatorSessionErrorCode.CLEANUP_FAILURE,
                "operator session cleanup failed",
            )
        self._state = terminal_state
        return self.control_snapshot
