"""K5-owned deterministic media-session boundary.

Runtime-specific RTSP/GStreamer mechanics stay behind this contract. Observable
state intentionally retains no source URI, credentials, address, frame, or media.
"""

from __future__ import annotations

import asyncio
from enum import StrEnum
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict


class MediaSessionState(StrEnum):
    CREATED = "created"
    OPENING = "opening"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"
    CLOSED = "closed"


class MediaSessionErrorCode(StrEnum):
    INVALID_STATE = "invalid_state"
    INVALID_SOURCE = "invalid_source"
    RUNTIME_FAILURE = "runtime_failure"


class MediaSessionError(RuntimeError):
    """Sanitized error that never carries source material."""

    def __init__(self, code: MediaSessionErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class MediaRuntime(Protocol):
    async def start(self, source_uri: str) -> None: ...

    async def stop(self) -> None: ...

    async def close(self) -> None: ...


class MediaSessionSnapshot(BaseModel):
    """Versioned source-free observable session state."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1"] = "1"
    state: MediaSessionState
    generation: int


class MediaSession:
    """Serialized deterministic lifecycle wrapper around one media runtime."""

    def __init__(self, runtime: MediaRuntime) -> None:
        self._runtime = runtime
        self._state = MediaSessionState.CREATED
        self._generation = 0
        self._lock = asyncio.Lock()

    @property
    def snapshot(self) -> MediaSessionSnapshot:
        return MediaSessionSnapshot(state=self._state, generation=self._generation)

    async def start(self, source_uri: str) -> MediaSessionSnapshot:
        if not source_uri.strip():
            raise MediaSessionError(
                MediaSessionErrorCode.INVALID_SOURCE,
                "media source is empty",
            )

        async with self._lock:
            if self._state == MediaSessionState.RUNNING:
                return self.snapshot
            if self._state not in {MediaSessionState.CREATED, MediaSessionState.STOPPED}:
                raise MediaSessionError(
                    MediaSessionErrorCode.INVALID_STATE,
                    "session cannot start from current state",
                )

            self._state = MediaSessionState.OPENING
            try:
                await self._runtime.start(source_uri)
            except asyncio.CancelledError:
                self._state = MediaSessionState.FAILED
                raise
            except Exception:
                self._state = MediaSessionState.FAILED
                raise MediaSessionError(
                    MediaSessionErrorCode.RUNTIME_FAILURE,
                    "media runtime failed to start",
                ) from None

            self._generation += 1
            self._state = MediaSessionState.RUNNING
            return self.snapshot

    async def _stop_locked(self) -> MediaSessionSnapshot:
        if self._state in {MediaSessionState.CREATED, MediaSessionState.STOPPED}:
            self._state = MediaSessionState.STOPPED
            return self.snapshot
        if self._state != MediaSessionState.RUNNING:
            raise MediaSessionError(
                MediaSessionErrorCode.INVALID_STATE,
                "session cannot stop from current state",
            )

        self._state = MediaSessionState.STOPPING
        try:
            await self._runtime.stop()
        except asyncio.CancelledError:
            self._state = MediaSessionState.FAILED
            raise
        except Exception:
            self._state = MediaSessionState.FAILED
            raise MediaSessionError(
                MediaSessionErrorCode.RUNTIME_FAILURE,
                "media runtime failed to stop",
            ) from None

        self._state = MediaSessionState.STOPPED
        return self.snapshot

    async def stop(self) -> MediaSessionSnapshot:
        async with self._lock:
            return await self._stop_locked()

    async def recover(self) -> MediaSessionSnapshot:
        """Clean a failed runtime and return the session to a restartable stopped state."""
        async with self._lock:
            if self._state != MediaSessionState.FAILED:
                raise MediaSessionError(
                    MediaSessionErrorCode.INVALID_STATE,
                    "session recovery requires failed state",
                )
            try:
                await self._runtime.close()
            except asyncio.CancelledError:
                self._state = MediaSessionState.FAILED
                raise
            except Exception:
                self._state = MediaSessionState.FAILED
                raise MediaSessionError(
                    MediaSessionErrorCode.RUNTIME_FAILURE,
                    "media runtime cleanup failed",
                ) from None

            self._state = MediaSessionState.STOPPED
            return self.snapshot

    async def close(self) -> MediaSessionSnapshot:
        async with self._lock:
            if self._state == MediaSessionState.CLOSED:
                return self.snapshot
            if self._state == MediaSessionState.RUNNING:
                await self._stop_locked()
            if self._state == MediaSessionState.FAILED:
                raise MediaSessionError(
                    MediaSessionErrorCode.INVALID_STATE,
                    "failed session requires recovery before close",
                )

            try:
                await self._runtime.close()
            except asyncio.CancelledError:
                self._state = MediaSessionState.FAILED
                raise
            except Exception:
                self._state = MediaSessionState.FAILED
                raise MediaSessionError(
                    MediaSessionErrorCode.RUNTIME_FAILURE,
                    "media runtime failed to close",
                ) from None

            self._state = MediaSessionState.CLOSED
            return self.snapshot
