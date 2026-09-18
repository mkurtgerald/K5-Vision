"""Bounded renderer-agnostic coordination for multiple playback presentations.

The coordinator composes accepted Stage-24 recording-to-presentation deliveries
without selecting a renderer or UI toolkit. Recording paths and identifiers remain
inside child execution; retained observability is aggregate counters/lifecycle only.
"""

from __future__ import annotations

import asyncio
import enum
import typing
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from k5vision.media.presentation_frame import PresentationVideoFrame
from k5vision.media.presentation_playback import (
    PresentationPlaybackError,
    PresentationPlaybackSnapshot,
)

_MAX_STREAMS = 16
_MAX_SLOT = 63
_MAX_TOTAL_FRAMES = 1_000_000
_MAX_TOTAL_FRAME_BYTES = 16 * 1024 * 1024 * 1024
_MAX_SOURCE_SPAN_MS = 2_147_483_647


class PlaybackPresentationRunner(Protocol):
    async def run(
        self,
        consumer: Callable[[PresentationVideoFrame], Awaitable[None]],
    ) -> PresentationPlaybackSnapshot: ...


@dataclass(frozen=True, slots=True)
class MultiViewPlaybackStream:
    """Logical playback slot bound to an accepted presentation delivery."""

    slot: int
    delivery: PlaybackPresentationRunner

    def __post_init__(self) -> None:
        if not 0 <= self.slot <= _MAX_SLOT:
            raise ValueError("slot must be between 0 and 63")
        if not callable(getattr(self.delivery, "run", None)):
            raise TypeError("delivery must implement the playback presentation boundary")


MultiViewPlaybackFrameConsumer = Callable[[int, PresentationVideoFrame], Awaitable[None]]


class MultiViewPlaybackState(enum.StrEnum):
    CREATED = "created"
    RUNNING = "running"
    COMPLETE = "complete"
    CANCELLED = "cancelled"
    FAILED = "failed"


class MultiViewPlaybackErrorCode(enum.StrEnum):
    INVALID_STATE = "invalid_state"
    INVALID_STREAM_SET = "invalid_stream_set"
    STREAM_FAILURE = "stream_failure"
    FRAME_LIMIT = "frame_limit"
    BYTE_LIMIT = "byte_limit"
    CONSUMER_TIMEOUT = "consumer_timeout"
    CONSUMER_FAILURE = "consumer_failure"


class MultiViewPlaybackError(RuntimeError):
    """Sanitized multi-view playback coordination failure."""

    def __init__(self, code: MultiViewPlaybackErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class MultiViewPlaybackSnapshot(BaseModel):
    """Source/path/identifier-free aggregate playback observability."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: typing.Literal["1"] = "1"
    state: MultiViewPlaybackState
    stream_count: int = Field(ge=0, le=_MAX_STREAMS)
    completed_streams: int = Field(ge=0, le=_MAX_STREAMS)
    delivered_frames: int = Field(ge=0, le=_MAX_TOTAL_FRAMES)
    delivered_frame_bytes: int = Field(ge=0, le=_MAX_TOTAL_FRAME_BYTES)
    max_source_span_ms: int = Field(ge=0, le=_MAX_SOURCE_SPAN_MS)


class BoundedMultiViewPlaybackPresentation:
    """Coordinate bounded concurrent playback deliveries across logical view slots."""

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
        self._state = MultiViewPlaybackState.CREATED
        self._stream_count = 0
        self._completed_streams = 0
        self._delivered_frames = 0
        self._delivered_frame_bytes = 0
        self._max_source_span_ms = 0
        self._consumer_lock = asyncio.Lock()

    @property
    def snapshot(self) -> MultiViewPlaybackSnapshot:
        return MultiViewPlaybackSnapshot(
            state=self._state,
            stream_count=self._stream_count,
            completed_streams=self._completed_streams,
            delivered_frames=self._delivered_frames,
            delivered_frame_bytes=self._delivered_frame_bytes,
            max_source_span_ms=self._max_source_span_ms,
        )

    def _validate_streams(
        self,
        streams: Sequence[MultiViewPlaybackStream],
    ) -> tuple[MultiViewPlaybackStream, ...]:
        selected = tuple(streams)
        if not 2 <= len(selected) <= self._max_streams:
            raise MultiViewPlaybackError(
                MultiViewPlaybackErrorCode.INVALID_STREAM_SET,
                "multi-view playback stream count is outside the configured bound",
            )
        if any(not isinstance(stream, MultiViewPlaybackStream) for stream in selected):
            raise MultiViewPlaybackError(
                MultiViewPlaybackErrorCode.INVALID_STREAM_SET,
                "multi-view playback stream set is invalid",
            )
        slots = [stream.slot for stream in selected]
        if len(set(slots)) != len(slots):
            raise MultiViewPlaybackError(
                MultiViewPlaybackErrorCode.INVALID_STREAM_SET,
                "multi-view playback slots must be unique",
            )
        return selected

    async def _deliver_frame(
        self,
        slot: int,
        frame: PresentationVideoFrame,
        consumer: MultiViewPlaybackFrameConsumer,
    ) -> None:
        if not isinstance(frame, PresentationVideoFrame):
            raise MultiViewPlaybackError(
                MultiViewPlaybackErrorCode.STREAM_FAILURE,
                "multi-view playback emitted an invalid presentation frame",
            )
        frame_bytes = len(frame.payload)
        async with self._consumer_lock:
            if self._delivered_frames >= self._max_total_frames:
                raise MultiViewPlaybackError(
                    MultiViewPlaybackErrorCode.FRAME_LIMIT,
                    "multi-view playback aggregate frame limit exceeded",
                )
            if self._delivered_frame_bytes + frame_bytes > self._max_total_frame_bytes:
                raise MultiViewPlaybackError(
                    MultiViewPlaybackErrorCode.BYTE_LIMIT,
                    "multi-view playback aggregate frame-byte limit exceeded",
                )
            try:
                await asyncio.wait_for(
                    consumer(slot, frame),
                    timeout=self._consumer_timeout_seconds,
                )
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                raise MultiViewPlaybackError(
                    MultiViewPlaybackErrorCode.CONSUMER_TIMEOUT,
                    "multi-view playback frame consumer timed out",
                ) from None
            except Exception:
                raise MultiViewPlaybackError(
                    MultiViewPlaybackErrorCode.CONSUMER_FAILURE,
                    "multi-view playback frame consumer failed",
                ) from None
            self._delivered_frames += 1
            self._delivered_frame_bytes += frame_bytes
            self._max_source_span_ms = max(
                self._max_source_span_ms,
                frame.source_elapsed_ms,
            )

    async def _run_stream(
        self,
        stream: MultiViewPlaybackStream,
        consumer: MultiViewPlaybackFrameConsumer,
    ) -> None:
        boundary_error: MultiViewPlaybackError | None = None

        async def frame_consumer(frame: PresentationVideoFrame) -> None:
            nonlocal boundary_error
            try:
                await self._deliver_frame(stream.slot, frame, consumer)
            except MultiViewPlaybackError as exc:
                boundary_error = exc
                raise

        try:
            snapshot = await stream.delivery.run(frame_consumer)
        except asyncio.CancelledError:
            raise
        except PresentationPlaybackError:
            if boundary_error is not None:
                raise boundary_error from None
            raise MultiViewPlaybackError(
                MultiViewPlaybackErrorCode.STREAM_FAILURE,
                "multi-view playback presentation stream failed",
            ) from None
        except Exception:
            if boundary_error is not None:
                raise boundary_error from None
            raise MultiViewPlaybackError(
                MultiViewPlaybackErrorCode.STREAM_FAILURE,
                "multi-view playback presentation stream failed",
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
        streams: Sequence[MultiViewPlaybackStream],
        consumer: MultiViewPlaybackFrameConsumer,
    ) -> MultiViewPlaybackSnapshot:
        """Run one bounded concurrent multi-view playback delivery."""
        if self._state != MultiViewPlaybackState.CREATED:
            raise MultiViewPlaybackError(
                MultiViewPlaybackErrorCode.INVALID_STATE,
                "multi-view playback presentation cannot be reused",
            )
        if not callable(consumer):
            raise TypeError("consumer must be callable")

        selected = self._validate_streams(streams)
        self._stream_count = len(selected)
        self._state = MultiViewPlaybackState.RUNNING
        tasks = [
            asyncio.create_task(
                self._run_stream(stream, consumer),
                name=f"k5-multiview-playback-slot-{stream.slot}",
            )
            for stream in selected
        ]

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            await self._cancel_tasks(tasks)
            self._state = MultiViewPlaybackState.CANCELLED
            raise
        except MultiViewPlaybackError:
            await self._cancel_tasks(tasks)
            self._state = MultiViewPlaybackState.FAILED
            raise
        except Exception:
            await self._cancel_tasks(tasks)
            self._state = MultiViewPlaybackState.FAILED
            raise MultiViewPlaybackError(
                MultiViewPlaybackErrorCode.STREAM_FAILURE,
                "multi-view playback presentation failed",
            ) from None

        self._state = MultiViewPlaybackState.COMPLETE
        return self.snapshot
