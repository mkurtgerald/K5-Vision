"""Bounded application-session orchestration above the accepted Win32 shell."""

from __future__ import annotations

import asyncio
import enum
import typing
from collections.abc import Callable, Sequence

from pydantic import BaseModel, ConfigDict, Field

from k5vision.media.mixed_presentation import MixedPresentationStream
from k5vision.media.viewport_geometry import ViewportLayout
from k5vision.media.windows_operator_application import (
    BoundedWindowsOperatorApplication,
    WindowsOperatorApplicationSnapshot,
    WindowsOperatorApplicationState,
)

_MAX_SESSION_CYCLES = 1_000_000
_MAX_PUMP_MESSAGES = 256


class WindowsOperatorSessionState(enum.StrEnum):
    READY = "ready"
    RUNNING = "running"
    COMPLETE = "complete"
    USER_CLOSED = "user_closed"
    CANCELLED = "cancelled"
    FAILED = "failed"
    CLOSED = "closed"


class WindowsOperatorSessionErrorCode(enum.StrEnum):
    INVALID_CONFIGURATION = "invalid_configuration"
    INVALID_STATE = "invalid_state"
    SESSION_LIMIT = "session_limit"
    APPLICATION_FAILURE = "application_failure"
    CLEANUP_FAILURE = "cleanup_failure"


class WindowsOperatorSessionError(RuntimeError):
    """Sanitized session failure with no source or native identity."""

    def __init__(
        self,
        code: WindowsOperatorSessionErrorCode,
        message: str,
    ) -> None:
        super().__init__(message)
        self.code = code


class WindowsOperatorSessionSnapshot(BaseModel):
    """Privacy-safe aggregate session observability."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: typing.Literal["1"] = "1"
    state: WindowsOperatorSessionState
    cycles: int = Field(ge=0, le=_MAX_SESSION_CYCLES)
    shell_open: bool
    generation: int = Field(ge=0)
    viewport_count: int = Field(ge=0)
    open_surface_count: int = Field(ge=0)
    delivered_frames: int = Field(ge=0)
    presentations: int = Field(ge=0)
    pumped_messages: int = Field(ge=0)


@typing.runtime_checkable
class _ApplicationBoundary(typing.Protocol):
    @property
    def snapshot(self) -> WindowsOperatorApplicationSnapshot: ...

    async def open(self, width: int, height: int) -> WindowsOperatorApplicationSnapshot: ...

    async def start(
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


ApplicationFactory = Callable[[], _ApplicationBoundary]


def _default_application_factory() -> BoundedWindowsOperatorApplication:
    return BoundedWindowsOperatorApplication()


class BoundedWindowsOperatorSession:
    """Run one bounded visible operator application session to terminal cleanup."""

    def __init__(
        self,
        *,
        application_factory: ApplicationFactory | None = None,
        max_cycles: int = 100_000,
        max_messages_per_cycle: int = 64,
        poll_interval_seconds: float = 0.01,
        cleanup_timeout_seconds: float = 5.0,
    ) -> None:
        selected_factory = application_factory or _default_application_factory
        if not callable(selected_factory):
            raise WindowsOperatorSessionError(
                WindowsOperatorSessionErrorCode.INVALID_CONFIGURATION,
                "operator session application factory is invalid",
            )
        if not 1 <= max_cycles <= _MAX_SESSION_CYCLES:
            raise WindowsOperatorSessionError(
                WindowsOperatorSessionErrorCode.INVALID_CONFIGURATION,
                "operator session cycle bound is invalid",
            )
        if not 1 <= max_messages_per_cycle <= _MAX_PUMP_MESSAGES:
            raise WindowsOperatorSessionError(
                WindowsOperatorSessionErrorCode.INVALID_CONFIGURATION,
                "operator session message batch is invalid",
            )
        if not 0 <= poll_interval_seconds <= 1.0:
            raise WindowsOperatorSessionError(
                WindowsOperatorSessionErrorCode.INVALID_CONFIGURATION,
                "operator session poll interval is invalid",
            )
        if not 0.1 <= cleanup_timeout_seconds <= 30.0:
            raise WindowsOperatorSessionError(
                WindowsOperatorSessionErrorCode.INVALID_CONFIGURATION,
                "operator session cleanup timeout is invalid",
            )
        self._application_factory = selected_factory
        self._max_cycles = max_cycles
        self._max_messages_per_cycle = max_messages_per_cycle
        self._poll_interval_seconds = poll_interval_seconds
        self._cleanup_timeout_seconds = cleanup_timeout_seconds
        self._state = WindowsOperatorSessionState.READY
        self._cycles = 0
        self._application: _ApplicationBoundary | None = None
        self._application_snapshot: WindowsOperatorApplicationSnapshot | None = None
        self._lock = asyncio.Lock()

    @property
    def snapshot(self) -> WindowsOperatorSessionSnapshot:
        application = (
            self._application.snapshot
            if self._application is not None
            else self._application_snapshot
        )
        return WindowsOperatorSessionSnapshot(
            state=self._state,
            cycles=self._cycles,
            shell_open=False if application is None else application.shell_open,
            generation=0 if application is None else application.generation,
            viewport_count=0 if application is None else application.viewport_count,
            open_surface_count=(0 if application is None else application.open_surface_count),
            delivered_frames=0 if application is None else application.delivered_frames,
            presentations=0 if application is None else application.presentations,
            pumped_messages=0 if application is None else application.pumped_messages,
        )

    def _assemble_application(self) -> _ApplicationBoundary:
        try:
            application = self._application_factory()
        except Exception:
            raise WindowsOperatorSessionError(
                WindowsOperatorSessionErrorCode.APPLICATION_FAILURE,
                "operator session application assembly failed",
            ) from None
        if not isinstance(application, _ApplicationBoundary):
            raise WindowsOperatorSessionError(
                WindowsOperatorSessionErrorCode.APPLICATION_FAILURE,
                "operator session application assembly failed",
            )
        return application

    async def _cancel_wait_task(
        self,
        wait_task: asyncio.Task[WindowsOperatorApplicationSnapshot] | None,
    ) -> None:
        if wait_task is None or wait_task.done():
            return
        wait_task.cancel()
        try:
            await wait_task
        except BaseException:
            pass

    async def _close_application(self) -> bool:
        application = self._application
        self._application = None
        if application is None:
            return False
        try:
            self._application_snapshot = await asyncio.wait_for(
                application.close(),
                timeout=self._cleanup_timeout_seconds,
            )
        except BaseException:
            return True
        return False

    async def run(
        self,
        *,
        width: int,
        height: int,
        layout: ViewportLayout,
        streams: Sequence[MixedPresentationStream],
    ) -> WindowsOperatorSessionSnapshot:
        """Run one bounded shell/host session and always release it before returning."""
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
            self._application = application
            self._state = WindowsOperatorSessionState.RUNNING

        wait_task: asyncio.Task[WindowsOperatorApplicationSnapshot] | None = None
        terminal_state: WindowsOperatorSessionState | None = None
        try:
            self._application_snapshot = await application.open(width, height)
            self._application_snapshot = await application.start(layout, streams)
            wait_task = asyncio.create_task(application.wait())

            while self._cycles < self._max_cycles:
                self._application_snapshot = await application.pump(
                    max_messages=self._max_messages_per_cycle,
                )
                self._cycles += 1

                if self._application_snapshot.state == WindowsOperatorApplicationState.CLOSED:
                    terminal_state = WindowsOperatorSessionState.USER_CLOSED
                    await self._cancel_wait_task(wait_task)
                    break

                if wait_task.done():
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
        except WindowsOperatorSessionError:
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

        cleanup_failed = await self._close_application()
        if cleanup_failed:
            self._state = WindowsOperatorSessionState.FAILED
            raise WindowsOperatorSessionError(
                WindowsOperatorSessionErrorCode.CLEANUP_FAILURE,
                "operator session cleanup failed",
            )
        self._state = terminal_state
        return self.snapshot

    async def close(self) -> WindowsOperatorSessionSnapshot:
        """Idempotently release any owned application outside an active run."""
        async with self._lock:
            if self._state == WindowsOperatorSessionState.CLOSED:
                return self.snapshot
            if self._state == WindowsOperatorSessionState.RUNNING:
                raise WindowsOperatorSessionError(
                    WindowsOperatorSessionErrorCode.INVALID_STATE,
                    "operator session cannot close during active run",
                )
            cleanup_failed = await self._close_application()
            if cleanup_failed:
                self._state = WindowsOperatorSessionState.FAILED
                raise WindowsOperatorSessionError(
                    WindowsOperatorSessionErrorCode.CLEANUP_FAILURE,
                    "operator session cleanup failed",
                )
            self._state = WindowsOperatorSessionState.CLOSED
            return self.snapshot
