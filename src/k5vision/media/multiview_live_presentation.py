"""Bounded renderer-agnostic coordination for multiple live presentation streams.

The coordinator composes accepted Stage-25 live presentation deliveries without
selecting a renderer or UI toolkit. Source URIs remain execution-only inputs;
retained observability is aggregate counters/lifecycle metadata only.
"""

from __future__ import annotations

import asyncio
import enum
import typing
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from k5vision.media.live_presentation import (
    LivePresentationError,
    LivePresentationSnapshot,
)
from k5vision.media.presentation_frame import PresentationVideoFrame

_MAX_STREAMS = 16
_MAX_SLOT = 63
_MAX_TOTAL_FRAMES = 1_000_000
_MAX_TOTAL_FRAME_BYTES = 16 * 1024 * 1024 * 1024
_MAX_SOURCE_SPAN_MS = 600_000


class LivePresentationRunner(Protocol):
    async def run(
        self,
        source_uri: str,
        consumer: Callable[[PresentationVideoFrame], Awaitable[None]],
    ) -> LivePresentationSnapshot: ...


@dataclass(frozen=True, slots=True)
class MultiViewLiveStream:
    """Execution-only stream binding; it is never serialized into retained state."""

    slot: int
    source_uri: str
    delivery: LivePresentationRunner

    def __post_init__(self) -> None:
        if not 0 <= self.slot <= _MAX_SLOT:
            raise ValueError("slot must be between 0 and 63")
        if not isinstance(self.source_uri, str) or not self.source_uri.strip():
            raise ValueError("source_uri must be a non-empty string")
        if not callable(getattr(self.delivery, "run", None)):
            raise TypeError("delivery must implement the live presentation boundary")


MultiViewFrameConsumer = Callable[[int, PresentationVideoFrame], Awaitable[None]]


class MultiViewLiveState(enum.StrEnum):
    CREATED = "created"
    RUNNING = "running"
    COMPLETE = "complete"
    CANCELLED = "cancelled"
    FAILED = "failed"


class MultiViewLiveErrorCode(enum.StrEnum):
    INVALID_STATE = "invalid_state"
    INVALID_STREAM_SET = "invalid_stream_set"
    STREAM_FAILURE = "stream_failure"
    FRAME_LIMIT = "frame_limit"
    BYTE_LIMIT = "byte_limit"
    CONSUMER_TIMEOUT = "consumer_timeout"
    CONSUMER_FAILURE = "consumer_failure"


class MultiViewLiveError(RuntimeError):
    """Sanitized multi-view coordination failure."""

    def __init__(self, code: MultiViewLiveErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class MultiViewLiveSnapshot(BaseModel):
    """Source-free retained observability for one bounded multi-view run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: typing.Literal["1"] = "1"
    state: MultiViewLiveState
    stream_count: int = Field(ge=0, le=_MAX_STREAMS)
    completed_streams: int = Field(ge=0, le=_MAX_STREAMS)
    delivered_frames: int = Field(ge=0, le=_MAX_TOTAL_FRAMES)
    delivered_frame_bytes: int = Field(ge=0, le=_MAX_TOTAL_FRAME_BYTES)
    max_source_span_ms: int = Field(ge=0, le=_MAX_SOURCE_SPAN_MS)


class BoundedMultiViewLivePresentation:
    """Coordinate bounded live presentation deliveries across logical view slots."""

    def __init__(
        self,
        *,
        max_streams: int = 16,
        max_total_frames: int = 100_000,
        max_total_frame_bytes: int = 4 * 1024 * 1024 * 1024,
        consumer_timeout_seconds: float = 0.5,
    ) -> None:
        if not 2 <= max_streams <= _MAX_STREAMS:
            raise ValueError("max_streams must be between 2 and 16")
        if not 1 <= max_total_frames <= _MAX_TOTAL_FRAMES:
            raise ValueError("max_total_frames must be between 1 and 1000000")
        if not 1 <= max_total_frame_bytes <= _MAX_TOTAL_FRAME_BYTES:
            raise ValueError("max_total_frame_bytes must be between 1 and 17179869184")
        if not 0 < consumer_timeout_seconds <= 10:
            raise ValueError("consumer_timeout_seconds must be between zero and 10")

        self._max_streams = max_streams
        self._max_total_frames = max_total_frames
        self._max_total_frame_bytes = max_total_frame_bytes
        self._consumer_timeout_seconds = consumer_timeout_seconds
        self._state = MultiViewLiveState.CREATED
        self._stream_count = 0
        self._completed_streams = 0
        self._delivered_frames = 0
        self._delivered_frame_bytes = 0
        self._max_source_span_ms = 0
        self._consumer_lock = asyncio.Lock()

    @property
    def snapshot(self) -> MultiViewLiveSnapshot:
        return MultiViewLiveSnapshot(
            state=self._state,
            stream_count=self._stream_count,
            completed_streams=self._completed_streams,
            delivered_frames=self._delivered_frames,
            delivered_frame_bytes=self._delivered_frame_bytes,
            max_source_span_ms=self._max_source_span_ms,
        )

    def _validate_streams(
        self,
        streams: Sequence[MultiViewLiveStream],
    ) -> tuple[MultiViewLiveStream, ...]:
        selected = tuple(streams)
        if not 2 <= len(selected) <= self._max_streams:
            raise MultiViewLiveError(
                MultiViewLiveErrorCode.INVALID_STREAM_SET,
                "multi-view stream count is outside the configured bound",
            )
        if any(not isinstance(stream, MultiViewLiveStream) for stream in selected):
            raise MultiViewLiveError(
                MultiViewLiveErrorCode.INVALID_STREAM_SET,
                "multi-view stream set is invalid",
            )
        slots = [stream.slot for stream in selected]
        if len(set(slots)) != len(slots):
            raise MultiViewLiveError(
                MultiViewLiveErrorCode.INVALID_STREAM_SET,
                "multi-view stream slots must be unique",
            )
        return selected

    async def _deliver_frame(
        self,
        slot: int,
        frame: PresentationVideoFrame,
        consumer: MultiViewFrameConsumer,
    ) -> None:
        if not isinstance(frame, PresentationVideoFrame):
            raise MultiViewLiveError(
                MultiViewLiveErrorCode.STREAM_FAILURE,
                "multi-view stream emitted an invalid presentation frame",
            )
        frame_bytes = len(frame.payload)
        async with self._consumer_lock:
            if self._delivered_frames >= self._max_total_frames:
                raise MultiViewLiveError(
                    MultiViewLiveErrorCode.FRAME_LIMIT,
                    "multi-view aggregate frame limit exceeded",
                )
            if self._delivered_frame_bytes + frame_bytes > self._max_total_frame_bytes:
                raise MultiViewLiveError(
                    MultiViewLiveErrorCode.BYTE_LIMIT,
                    "multi-view aggregate frame-byte limit exceeded",
                )
            try:
                await asyncio.wait_for(
                    consumer(slot, frame),
                    timeout=self._consumer_timeout_seconds,
                )
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                raise MultiViewLiveError(
                    MultiViewLiveErrorCode.CONSUMER_TIMEOUT,
                    "multi-view frame consumer timed out",
                ) from None
            except Exception:
                raise MultiViewLiveError(
                    MultiViewLiveErrorCode.CONSUMER_FAILURE,
                    "multi-view frame consumer failed",
                ) from None
            self._delivered_frames += 1
            self._delivered_frame_bytes += frame_bytes
            self._max_source_span_ms = max(
                self._max_source_span_ms,
                frame.source_elapsed_ms,
            )

    async def _run_stream(
        self,
        stream: MultiViewLiveStream,
        consumer: MultiViewFrameConsumer,
    ) -> None:
        boundary_error: MultiViewLiveError | None = None

        async def frame_consumer(frame: PresentationVideoFrame) -> None:
            nonlocal boundary_error
            try:
                await self._deliver_frame(stream.slot, frame, consumer)
            except MultiViewLiveError as exc:
                boundary_error = exc
                raise

        try:
            snapshot = await stream.delivery.run(stream.source_uri, frame_consumer)
        except asyncio.CancelledError:
            raise
        except LivePresentationError:
            if boundary_error is not None:
                raise boundary_error from None
            raise MultiViewLiveError(
                MultiViewLiveErrorCode.STREAM_FAILURE,
                "multi-view live presentation stream failed",
            ) from None
        except Exception:
            if boundary_error is not None:
                raise boundary_error from None
            raise MultiViewLiveError(
                MultiViewLiveErrorCode.STREAM_FAILURE,
                "multi-view live presentation stream failed",
            ) from None

        async with self._consumer_lock:
            self._completed_streams += 1
            self._max_source_span_ms = max(
                self._max_source_span_ms,
                snapshot.source_span_ms,
            )

    @staticmethod
    async def _cancel_tasks(tasks: Sequence[asyncio.Task[None]]) -> None:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def run(
        self,
        streams: Sequence[MultiViewLiveStream],
        consumer: MultiViewFrameConsumer,
    ) -> MultiViewLiveSnapshot:
        """Run one bounded concurrent multi-view delivery."""
        if self._state != MultiViewLiveState.CREATED:
            raise MultiViewLiveError(
                MultiViewLiveErrorCode.INVALID_STATE,
                "multi-view live presentation cannot be reused",
            )
        if not callable(consumer):
            raise TypeError("consumer must be callable")

        selected = self._validate_streams(streams)
        self._stream_count = len(selected)
        self._state = MultiViewLiveState.RUNNING
        tasks = [
            asyncio.create_task(
                self._run_stream(stream, consumer),
                name=f"k5-multiview-slot-{stream.slot}",
            )
            for stream in selected
        ]

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            await self._cancel_tasks(tasks)
            self._state = MultiViewLiveState.CANCELLED
            raise
        except MultiViewLiveError:
            await self._cancel_tasks(tasks)
            self._state = MultiViewLiveState.FAILED
            raise
        except Exception:
            await self._cancel_tasks(tasks)
            self._state = MultiViewLiveState.FAILED
            raise MultiViewLiveError(
                MultiViewLiveErrorCode.STREAM_FAILURE,
                "multi-view live presentation failed",
            ) from None

        self._state = MultiViewLiveState.COMPLETE
        return self.snapshot
