"""K5-owned deterministic media-session boundary.

Runtime-specific RTSP/GStreamer mechanics stay behind this contract. Observable
state intentionally retains no source URI, credentials, address, frame, or media.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol

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
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: str = "1"
    state: MediaSessionState
    generation: int


class MediaSession:
    """Deterministic lifecycle wrapper around one selected media runtime."""

    def __init__(self, runtime: MediaRuntime) -> None:
        self._runtime = runtime
        self._state = MediaSessionState.CREATED
        self._generation = 0

    @property
    def snapshot(self) -> MediaSessionSnapshot:
        return MediaSessionSnapshot(state=self._state, generation=self._generation)

    async def start(self, source_uri: str) -> MediaSessionSnapshot:
        if not source_uri.strip():
            raise MediaSessionError(
                MediaSessionErrorCode.INVALID_SOURCE,
                "media source is empty",
            )
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
        except Exception:
            self._state = MediaSessionState.FAILED
            raise MediaSessionError(
                MediaSessionErrorCode.RUNTIME_FAILURE,
                "media runtime failed to start",
            ) from None

        self._generation += 1
        self._state = MediaSessionState.RUNNING
        return self.snapshot

    async def stop(self) -> MediaSessionSnapshot:
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
        except Exception:
            self._state = MediaSessionState.FAILED
            raise MediaSessionError(
                MediaSessionErrorCode.RUNTIME_FAILURE,
                "media runtime failed to stop",
            ) from None

        self._state = MediaSessionState.STOPPED
        return self.snapshot

    async def close(self) -> MediaSessionSnapshot:
        if self._state == MediaSessionState.CLOSED:
            return self.snapshot
        if self._state == MediaSessionState.RUNNING:
            await self.stop()
        if self._state == MediaSessionState.FAILED:
            raise MediaSessionError(
                MediaSessionErrorCode.INVALID_STATE,
                "failed session requires explicit runtime cleanup",
            )

        try:
            await self._runtime.close()
        except Exception:
            self._state = MediaSessionState.FAILED
            raise MediaSessionError(
                MediaSessionErrorCode.RUNTIME_FAILURE,
                "media runtime failed to close",
            ) from None

        self._state = MediaSessionState.CLOSED
        return self.snapshot
