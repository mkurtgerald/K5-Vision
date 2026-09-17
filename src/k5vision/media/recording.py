"""Bounded K5-owned recording ingest boundary.

This module accepts validated RTP packets from the Stage-06 consumer boundary and
hands them to a storage sink without exposing payloads in observable state.
Containerization, retention policy, playback, export, and indexing remain outside
this stage.
"""

from __future__ import annotations

import asyncio
from enum import StrEnum
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from k5vision.media.rtp_delivery import is_rtp_v2


class RecordingSink(Protocol):
    """Project-owned persistence handoff implemented by a concrete storage layer."""

    async def open(self) -> None: ...

    async def write(self, packet: memoryview) -> None: ...

    async def finalize(self) -> None: ...

    async def abort(self) -> None: ...


class RecordingState(StrEnum):
    CREATED = "created"
    RECORDING = "recording"
    FINALIZED = "finalized"
    ABORTED = "aborted"
    FAILED = "failed"


class RecordingErrorCode(StrEnum):
    INVALID_STATE = "invalid_state"
    INVALID_PACKET = "invalid_packet"
    LIMIT_EXCEEDED = "limit_exceeded"
    SINK_FAILURE = "sink_failure"
    TIMEOUT = "timeout"


class RecordingError(RuntimeError):
    """Sanitized recording failure that never contains source or payload details."""

    def __init__(self, code: RecordingErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class RecordingSnapshot(BaseModel):
    """Source-free recording observability."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"
    state: RecordingState
    packets_written: int = Field(ge=0)
    bytes_written: int = Field(ge=0)


class BoundedRtpRecorder:
    """Serialize and bound RTP writes to one recording sink."""

    def __init__(
        self,
        sink: RecordingSink,
        *,
        max_packets: int = 4096,
        max_bytes: int = 64 * 1024 * 1024,
        operation_timeout_seconds: float = 2.0,
    ) -> None:
        if not 1 <= max_packets <= 1_000_000:
            raise ValueError("max_packets must be between 1 and 1000000")
        if not 1 <= max_bytes <= 8 * 1024 * 1024 * 1024:
            raise ValueError("max_bytes must be between 1 and 8589934592")
        if not 0 < operation_timeout_seconds <= 30:
            raise ValueError("operation_timeout_seconds must be between zero and 30")
        self._sink = sink
        self._max_packets = max_packets
        self._max_bytes = max_bytes
        self._timeout = operation_timeout_seconds
        self._state = RecordingState.CREATED
        self._packets_written = 0
        self._bytes_written = 0
        self._lock = asyncio.Lock()

    @property
    def snapshot(self) -> RecordingSnapshot:
        return RecordingSnapshot(
            state=self._state,
            packets_written=self._packets_written,
            bytes_written=self._bytes_written,
        )

    async def _call(self, operation, *, message: str) -> None:
        try:
            await asyncio.wait_for(operation(), timeout=self._timeout)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            raise RecordingError(RecordingErrorCode.TIMEOUT, message) from None
        except Exception:
            raise RecordingError(RecordingErrorCode.SINK_FAILURE, message) from None

    async def _abort_sink_best_effort(self) -> None:
        try:
            await asyncio.wait_for(self._sink.abort(), timeout=self._timeout)
        except BaseException:
            pass

    async def start(self) -> RecordingSnapshot:
        async with self._lock:
            if self._state == RecordingState.RECORDING:
                return self.snapshot
            if self._state != RecordingState.CREATED:
                raise RecordingError(
                    RecordingErrorCode.INVALID_STATE,
                    "recording cannot start from current state",
                )
            try:
                await self._call(self._sink.open, message="recording sink failed to open")
            except asyncio.CancelledError:
                self._state = RecordingState.ABORTED
                await self._abort_sink_best_effort()
                raise
            except RecordingError:
                self._state = RecordingState.FAILED
                await self._abort_sink_best_effort()
                raise
            self._state = RecordingState.RECORDING
            return self.snapshot

    async def consume(self, packet: memoryview) -> RecordingSnapshot:
        async with self._lock:
            if self._state != RecordingState.RECORDING:
                raise RecordingError(
                    RecordingErrorCode.INVALID_STATE,
                    "recording is not accepting packets",
                )
            if not is_rtp_v2(packet):
                raise RecordingError(
                    RecordingErrorCode.INVALID_PACKET,
                    "recording packet is not valid RTP v2",
                )
            packet_bytes = len(packet)
            if (
                self._packets_written + 1 > self._max_packets
                or self._bytes_written + packet_bytes > self._max_bytes
            ):
                self._state = RecordingState.ABORTED
                await self._abort_sink_best_effort()
                raise RecordingError(
                    RecordingErrorCode.LIMIT_EXCEEDED,
                    "recording ingest limit exceeded",
                )
            try:
                await self._call(
                    lambda: self._sink.write(packet),
                    message="recording sink write failed",
                )
            except asyncio.CancelledError:
                self._state = RecordingState.ABORTED
                await self._abort_sink_best_effort()
                raise
            except RecordingError:
                self._state = RecordingState.FAILED
                await self._abort_sink_best_effort()
                raise
            self._packets_written += 1
            self._bytes_written += packet_bytes
            return self.snapshot

    async def finalize(self) -> RecordingSnapshot:
        async with self._lock:
            if self._state == RecordingState.FINALIZED:
                return self.snapshot
            if self._state != RecordingState.RECORDING:
                raise RecordingError(
                    RecordingErrorCode.INVALID_STATE,
                    "recording cannot finalize from current state",
                )
            try:
                await self._call(
                    self._sink.finalize,
                    message="recording sink failed to finalize",
                )
            except asyncio.CancelledError:
                self._state = RecordingState.ABORTED
                await self._abort_sink_best_effort()
                raise
            except RecordingError:
                self._state = RecordingState.FAILED
                await self._abort_sink_best_effort()
                raise
            self._state = RecordingState.FINALIZED
            return self.snapshot

    async def abort(self) -> RecordingSnapshot:
        async with self._lock:
            if self._state == RecordingState.ABORTED:
                return self.snapshot
            if self._state == RecordingState.FINALIZED:
                raise RecordingError(
                    RecordingErrorCode.INVALID_STATE,
                    "finalized recording cannot be aborted",
                )
            await self._abort_sink_best_effort()
            self._state = RecordingState.ABORTED
            return self.snapshot
