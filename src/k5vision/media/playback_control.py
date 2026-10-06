"""Bounded pause/resume control for paced playback.

The control owns no media, source, recording, device, or credential data. It exposes a
virtual monotonic clock plus an interruptible sleeper so the accepted playback pump can
freeze elapsed playback time while paused and resume without a catch-up burst.
"""

from __future__ import annotations

import asyncio
import enum
import time
import typing
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar

from pydantic import BaseModel, ConfigDict, Field

Clock = Callable[[], float]
Sleeper = Callable[[float], Awaitable[None]]


class PlaybackControlState(enum.StrEnum):
    RUNNING = "running"
    PAUSED = "paused"


class PlaybackControlErrorCode(enum.StrEnum):
    CLOCK_FAILURE = "clock_failure"
    SLEEP_FAILURE = "sleep_failure"


class PlaybackControlError(RuntimeError):
    """Sanitized pause-control failure."""

    def __init__(self, code: PlaybackControlErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class PlaybackControlSnapshot(BaseModel):
    """Source-free pause/resume observability."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: typing.Literal["1"] = "1"
    state: PlaybackControlState
    pause_count: int = Field(ge=0, le=1_000_000)
    resume_count: int = Field(ge=0, le=1_000_000)
    paused_total_ms: int = Field(ge=0, le=2_147_483_647)


class PlaybackPauseControl:
    """Freeze the playback clock while paused and resume deterministic pacing."""

    def __init__(
        self,
        *,
        clock: Clock = time.monotonic,
        sleep: Sleeper = asyncio.sleep,
    ) -> None:
        if not callable(clock):
            raise TypeError("clock must be callable")
        if not callable(sleep):
            raise TypeError("sleep must be callable")
        self._clock = clock
        self._sleep = sleep
        self._state = PlaybackControlState.RUNNING
        self._pause_count = 0
        self._resume_count = 0
        self._paused_total_seconds = 0.0
        self._paused_at: float | None = None
        self._running = asyncio.Event()
        self._running.set()
        self._paused = asyncio.Event()
        self._lock = asyncio.Lock()
        self._work_deadline: ContextVar[tuple[asyncio.Task[typing.Any], asyncio.Timeout] | None] = (
            ContextVar("playback_work_deadline", default=None)
        )

    def _raw_now(self) -> float:
        try:
            return float(self._clock())
        except Exception:
            raise PlaybackControlError(
                PlaybackControlErrorCode.CLOCK_FAILURE,
                "playback control clock failed",
            ) from None

    @property
    def snapshot(self) -> PlaybackControlSnapshot:
        return PlaybackControlSnapshot(
            state=self._state,
            pause_count=self._pause_count,
            resume_count=self._resume_count,
            paused_total_ms=min(
                2_147_483_647,
                max(0, round(self._paused_total_seconds * 1000)),
            ),
        )

    def monotonic(self) -> float:
        """Return virtual playback time with paused wall time removed."""
        raw_now = self._raw_now()
        effective_now = self._paused_at if self._paused_at is not None else raw_now
        return effective_now - self._paused_total_seconds

    async def pause(self) -> PlaybackControlSnapshot:
        async with self._lock:
            if self._state is PlaybackControlState.PAUSED:
                return self.snapshot
            paused_at = self._raw_now()
            self._paused_at = paused_at
            self._state = PlaybackControlState.PAUSED
            self._pause_count += 1
            self._running.clear()
            self._paused.set()
            return self.snapshot

    async def resume(self) -> PlaybackControlSnapshot:
        async with self._lock:
            if self._state is PlaybackControlState.RUNNING:
                return self.snapshot
            resumed_at = self._raw_now()
            assert self._paused_at is not None
            self._paused_total_seconds += max(0.0, resumed_at - self._paused_at)
            self._paused_at = None
            self._state = PlaybackControlState.RUNNING
            self._resume_count += 1
            self._paused.clear()
            self._running.set()
            return self.snapshot

    async def wait_until_running(self) -> None:
        """Admit work only while running; exempt this task's explicit pause wait."""
        if self._state is PlaybackControlState.RUNNING:
            return
        bound = self._work_deadline.get()
        # Context is inherited by decoder/consumer child tasks. Such a child must
        # never suspend the deadline bounding its parent's active work.
        deadline = bound[1] if bound is not None and bound[0] is asyncio.current_task() else None
        remaining = None
        if deadline is not None:
            when = deadline.when()
            if deadline.expired() or (
                when is not None and when <= asyncio.get_running_loop().time()
            ):
                raise TimeoutError
            if when is not None:
                remaining = when - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError
                deadline.reschedule(None)
        try:
            # Resume can be superseded by Pause before an awakened waiter runs.
            while self._state is PlaybackControlState.PAUSED:
                await self._running.wait()
        finally:
            if deadline is not None and remaining is not None and not deadline.expired():
                deadline.reschedule(asyncio.get_running_loop().time() + remaining)

    @asynccontextmanager
    async def work_timeout(self, seconds: float) -> AsyncIterator[None]:
        """Bound callback work while excluding only its explicit pause-admission waits."""
        owner = asyncio.current_task()
        if owner is None:
            raise RuntimeError("playback work deadline requires a task")
        async with asyncio.timeout(seconds) as deadline:
            token = self._work_deadline.set((owner, deadline))
            try:
                yield
            finally:
                self._work_deadline.reset(token)

    async def sleep(self, delay: float) -> None:
        """Sleep for virtual playback time and stop consuming delay while paused."""
        if delay <= 0:
            await self.wait_until_running()
            return
        target = self.monotonic() + delay
        while True:
            await self.wait_until_running()
            remaining = target - self.monotonic()
            if remaining <= 0:
                return

            delay_task = asyncio.create_task(self._sleep(remaining))
            pause_task = asyncio.create_task(self._paused.wait())
            try:
                done, pending = await asyncio.wait(
                    {delay_task, pause_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                if delay_task in done:
                    try:
                        await delay_task
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        raise PlaybackControlError(
                            PlaybackControlErrorCode.SLEEP_FAILURE,
                            "playback control sleep failed",
                        ) from None
                if pause_task in done:
                    await pause_task
            except asyncio.CancelledError:
                delay_task.cancel()
                pause_task.cancel()
                await asyncio.gather(delay_task, pause_task, return_exceptions=True)
                raise
