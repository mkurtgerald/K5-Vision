"""Bounded source-safe live-view consumer lifecycle.

This module intentionally manages only ephemeral consumer leases over an accepted
MediaSession. It stores no source URI, credentials, address, frame, clip, or media
payload in observable state or retained evidence.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from enum import StrEnum
from typing import Literal, TypeVar
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from k5vision.media.session import MediaSession, MediaSessionError, MediaSessionState

_T = TypeVar("_T")


class LiveViewState(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    FAILED = "failed"
    CLOSED = "closed"


class LiveViewErrorCode(StrEnum):
    CAPACITY = "capacity"
    INVALID_LEASE = "invalid_lease"
    INVALID_STATE = "invalid_state"
    OPERATION_TIMEOUT = "operation_timeout"
    SESSION_FAILURE = "session_failure"


class LiveViewError(RuntimeError):
    """Sanitized live-view failure that never embeds protected source data."""

    def __init__(self, code: LiveViewErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class LiveViewLease(BaseModel):
    """Opaque lease returned to one ephemeral live-view consumer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1"] = "1"
    lease_id: UUID
    generation: int = Field(ge=1)


class LiveViewSnapshot(BaseModel):
    """Source-free observable state for the shared live-view boundary."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1"] = "1"
    state: LiveViewState
    active_consumers: int = Field(ge=0)
    max_consumers: int = Field(ge=1, le=64)
    generation: int = Field(ge=0)


class LiveViewBoundary:
    """Serialize bounded consumers onto one project-owned media session.

    Multiple consumers share one underlying MediaSession start. The final release
    stops the runtime, preventing consumer fan-out from multiplying camera/runtime
    connections. Source material is passed through only for the first start and is
    never copied into lease or snapshot state.
    """

    def __init__(
        self,
        session: MediaSession,
        *,
        max_consumers: int = 8,
        operation_timeout_seconds: float = 10.0,
    ) -> None:
        if not 1 <= max_consumers <= 64:
            raise ValueError("max_consumers must be between 1 and 64")
        if operation_timeout_seconds <= 0:
            raise ValueError("operation_timeout_seconds must be positive")
        self._session = session
        self._max_consumers = max_consumers
        self._operation_timeout_seconds = operation_timeout_seconds
        self._leases: set[UUID] = set()
        self._state = LiveViewState.IDLE
        self._lock = asyncio.Lock()

    @property
    def snapshot(self) -> LiveViewSnapshot:
        session_snapshot = self._session.snapshot
        return LiveViewSnapshot(
            state=self._state,
            active_consumers=len(self._leases),
            max_consumers=self._max_consumers,
            generation=session_snapshot.generation,
        )

    async def _bounded(self, operation: Awaitable[_T]) -> _T:
        try:
            return await asyncio.wait_for(
                operation,
                timeout=self._operation_timeout_seconds,
            )
        except asyncio.CancelledError:
            self._state = LiveViewState.FAILED
            raise
        except TimeoutError:
            self._state = LiveViewState.FAILED
            raise LiveViewError(
                LiveViewErrorCode.OPERATION_TIMEOUT,
                "live-view operation timed out",
            ) from None
        except MediaSessionError:
            self._state = LiveViewState.FAILED
            raise LiveViewError(
                LiveViewErrorCode.SESSION_FAILURE,
                "live-view media session failed",
            ) from None

    async def acquire(self, source_uri: str) -> LiveViewLease:
        """Acquire one consumer lease without multiplying runtime starts."""
        async with self._lock:
            if self._state == LiveViewState.CLOSED:
                raise LiveViewError(
                    LiveViewErrorCode.INVALID_STATE,
                    "live-view boundary is closed",
                )
            if self._state == LiveViewState.FAILED:
                raise LiveViewError(
                    LiveViewErrorCode.INVALID_STATE,
                    "live-view boundary requires recovery",
                )
            if len(self._leases) >= self._max_consumers:
                raise LiveViewError(
                    LiveViewErrorCode.CAPACITY,
                    "live-view consumer capacity reached",
                )

            if not self._leases:
                await self._bounded(self._session.start(source_uri))
                self._state = LiveViewState.RUNNING

            lease_id = uuid4()
            self._leases.add(lease_id)
            return LiveViewLease(
                lease_id=lease_id,
                generation=self._session.snapshot.generation,
            )

    async def release(self, lease_id: UUID) -> LiveViewSnapshot:
        """Release one lease; stop the runtime when the final consumer leaves."""
        async with self._lock:
            if lease_id not in self._leases:
                raise LiveViewError(
                    LiveViewErrorCode.INVALID_LEASE,
                    "live-view lease is not active",
                )

            self._leases.remove(lease_id)
            if self._leases:
                return self.snapshot

            try:
                await self._bounded(self._session.stop())
            except Exception:
                self._leases.clear()
                raise
            self._state = LiveViewState.IDLE
            return self.snapshot

    async def recover(self) -> LiveViewSnapshot:
        """Clean a failed session and return to an idle, restartable boundary."""
        async with self._lock:
            if self._state != LiveViewState.FAILED:
                raise LiveViewError(
                    LiveViewErrorCode.INVALID_STATE,
                    "live-view recovery requires failed state",
                )
            self._leases.clear()
            if self._session.snapshot.state == MediaSessionState.FAILED:
                await self._bounded(self._session.recover())
            self._state = LiveViewState.IDLE
            return self.snapshot

    async def close(self) -> LiveViewSnapshot:
        """Bounded cleanup for all consumers and the underlying session."""
        async with self._lock:
            if self._state == LiveViewState.CLOSED:
                return self.snapshot
            self._leases.clear()
            try:
                if self._session.snapshot.state == MediaSessionState.RUNNING:
                    await self._bounded(self._session.stop())
                elif self._session.snapshot.state == MediaSessionState.FAILED:
                    await self._bounded(self._session.recover())
                await self._bounded(self._session.close())
            except Exception:
                self._state = LiveViewState.FAILED
                raise
            self._state = LiveViewState.CLOSED
            return self.snapshot
