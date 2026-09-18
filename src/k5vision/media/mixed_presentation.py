"""Bounded renderer-agnostic coordination for mixed live and playback presentation.

The coordinator composes already-accepted live and playback presentation deliveries
without selecting a renderer or UI toolkit. Source URIs and playback storage details
remain execution-only inside child boundaries; retained state is aggregate metadata.
"""

from __future__ import annotations

import asyncio
import enum
import typing
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from k5vision.media.live_presentation import LivePresentationError, LivePresentationSnapshot
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


class LivePresentationRunner(Protocol):
    async def run(
        self,
        source_uri: str,
        consumer: Callable[[PresentationVideoFrame], Awaitable[None]],
    ) -> LivePresentationSnapshot: ...


class PlaybackPresentationRunner(Protocol):
    async def run(
        self,
        consumer: Callable[[PresentationVideoFrame], Awaitable[None]],
    ) -> PresentationPlaybackSnapshot: ...


@dataclass(frozen=True, slots=True)
class MixedLiveStream:
    """Execution-only live source binding for one logical presentation slot."""

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


@dataclass(frozen=True, slots=True)
class MixedPlaybackStream:
    """Execution-only playback binding for one logical presentation slot."""

    slot: int
    delivery: PlaybackPresentationRunner

    def __post_init__(self) -> None:
        if not 0 <= self.slot <= _MAX_SLOT:
            raise ValueError("slot must be between 0 and 63")
        if not callable(getattr(self.delivery, "run", None)):
            raise TypeError("delivery must implement the playback presentation boundary")


type MixedPresentationStream = MixedLiveStream | MixedPlaybackStream
MixedPresentationFrameConsumer = Callable[[int, PresentationVideoFrame], Awaitable[None]]


class MixedPresentationState(enum.StrEnum):
    CREATED = "created"
    RUNNING = "running"
    COMPLETE = "complete"
    CANCELLED = "cancelled"
    FAILED = "failed"


class MixedPresentationErrorCode(enum.StrEnum):
    INVALID_STATE = "invalid_state"
    INVALID_STREAM_SET = "invalid_stream_set"
    STREAM_FAILURE = "stream_failure"
    FRAME_LIMIT = "frame_limit"
    BYTE_LIMIT = "byte_limit"
    CONSUMER_TIMEOUT = "consumer_timeout"
    CONSUMER_FAILURE = "consumer_failure"


class MixedPresentationError(RuntimeError):
    """Sanitized mixed-presentation coordination failure."""

    def __init__(self, code: MixedPresentationErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class MixedPresentationSnapshot(BaseModel):
    """Source/path/identifier-free aggregate mixed-presentation observability."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: typing.Literal["1"] = "1"
    state: MixedPresentationState
    stream_count: int = Field(ge=0, le=_MAX_STREAMS)
    live_streams: int = Field(ge=0, le=_MAX_STREAMS)
    playback_streams: int = Field(ge=0, le=_MAX_STREAMS)
    completed_streams: int = Field(ge=0, le=_MAX_STREAMS)
    delivered_frames: int = Field(ge=0, le=_MAX_TOTAL_FRAMES)
    delivered_frame_bytes: int = Field(ge=0, le=_MAX_TOTAL_FRAME_BYTES)
    max_source_span_ms: int = Field(ge=0, le=_MAX_SOURCE_SPAN_MS)


class BoundedMixedPresentation:
    """Coordinate a bounded mixed set of live and playback presentation streams."""

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
        self._state = MixedPresentationState.CREATED
        self._stream_count = 0
        self._live_streams = 0
        self._playback_streams = 0
        self._completed_streams = 0
        self._delivered_frames = 0
        self._delivered_frame_bytes = 0
        self._max_source_span_ms = 0
        self._consumer_lock = asyncio.Lock()

    @property
    def snapshot(self) -> MixedPresentationSnapshot:
        return MixedPresentationSnapshot(
            state=self._state,
            stream_count=self._stream_count,
            live_streams=self._live_streams,
            playback_streams=self._playback_streams,
            completed_streams=self._completed_streams,
            delivered_frames=self._delivered_frames,
            delivered_frame_bytes=self._delivered_frame_bytes,
            max_source_span_ms=self._max_source_span_ms,
        )

    def _validate_streams(
        self,
        streams: Sequence[MixedPresentationStream],
    ) -> tuple[MixedPresentationStream, ...]:
        selected = tuple(streams)
        if not 2 <= len(selected) <= self._max_streams:
            raise MixedPresentationError(
                MixedPresentationErrorCode.INVALID_STREAM_SET,
                "mixed presentation stream count is outside the configured bound",
            )
        if any(
            not isinstance(stream, (MixedLiveStream, MixedPlaybackStream))
            for stream in selected
        ):
            raise MixedPresentationError(
                MixedPresentationErrorCode.INVALID_STREAM_SET,
                "mixed presentation stream set is invalid",
            )
        slots = [stream.slot for stream in selected]
        if len(set(slots)) != len(slots):
            raise MixedPresentationError(
                MixedPresentationErrorCode.INVALID_STREAM_SET,
                "mixed presentation stream slots must be unique",
            )
        live_count = sum(isinstance(stream, MixedLiveStream) for stream in selected)
        playback_count = len(selected) - live_count
        if live_count == 0 or playback_count == 0:
            raise MixedPresentationError(
                MixedPresentationErrorCode.INVALID_STREAM_SET,
                "mixed presentation requires live and playback streams",
            )
        return selected

    async def _deliver_frame(
        self,
        slot: int,
        frame: PresentationVideoFrame,
        consumer: MixedPresentationFrameConsumer,
    ) -> None:
        if not isinstance(frame, PresentationVideoFrame):
            raise MixedPresentationError(
                MixedPresentationErrorCode.STREAM_FAILURE,
                "mixed presentation stream emitted an invalid presentation frame",
            )
        frame_bytes = len(frame.payload)
        async with self._consumer_lock:
            if self._delivered_frames >= self._max_total_frames:
                raise MixedPresentationError(
                    MixedPresentationErrorCode.FRAME_LIMIT,
                    "mixed presentation aggregate frame limit exceeded",
                )
            if self._delivered_frame_bytes + frame_bytes > self._max_total_frame_bytes:
                raise MixedPresentationError(
                    MixedPresentationErrorCode.BYTE_LIMIT,
                    "mixed presentation aggregate frame-byte limit exceeded",
                )
            try:
                await asyncio.wait_for(
                    consumer(slot, frame),
                    timeout=self._consumer_timeout_seconds,
                )
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                raise MixedPresentationError(
                    MixedPresentationErrorCode.CONSUMER_TIMEOUT,
                    "mixed presentation frame consumer timed out",
                ) from None
            except Exception:
                raise MixedPresentationError(
                    MixedPresentationErrorCode.CONSUMER_FAILURE,
                    "mixed presentation frame consumer failed",
                ) from None
            self._delivered_frames += 1
            self._delivered_frame_bytes += frame_bytes
            self._max_source_span_ms = max(
                self._max_source_span_ms,
                frame.source_elapsed_ms,
            )

    async def _run_live(
        self,
        stream: MixedLiveStream,
        consumer: MixedPresentationFrameConsumer,
    ) -> None:
        boundary_error: MixedPresentationError | None = None

        async def frame_consumer(frame: PresentationVideoFrame) -> None:
            nonlocal boundary_error
            try:
                await self._deliver_frame(stream.slot, frame, consumer)
            except MixedPresentationError as exc:
                boundary_error = exc
                raise

        try:
            snapshot = await stream.delivery.run(stream.source_uri, frame_consumer)
        except asyncio.CancelledError:
            raise
        except LivePresentationError:
            if boundary_error is not None:
                raise boundary_error from None
            raise MixedPresentationError(
                MixedPresentationErrorCode.STREAM_FAILURE,
                "mixed live presentation stream failed",
            ) from None
        except Exception:
            if boundary_error is not None:
                raise boundary_error from None
            raise MixedPresentationError(
                MixedPresentationErrorCode.STREAM_FAILURE,
                "mixed live presentation stream failed",
            ) from None

        async with self._consumer_lock:
            self._completed_streams += 1
            self._max_source_span_ms = max(self._max_source_span_ms, snapshot.source_span_ms)

    async def _run_playback(
        self,
        stream: MixedPlaybackStream,
        consumer: MixedPresentationFrameConsumer,
    ) -> None:
        boundary_error: MixedPresentationError | None = None

        async def frame_consumer(frame: PresentationVideoFrame) -> None:
            nonlocal boundary_error
            try:
                await self._deliver_frame(stream.slot, frame, consumer)
            except MixedPresentationError as exc:
                boundary_error = exc
                raise

        try:
            snapshot = await stream.delivery.run(frame_consumer)
        except asyncio.CancelledError:
            raise
        except PresentationPlaybackError:
            if boundary_error is not None:
                raise boundary_error from None
            raise MixedPresentationError(
                MixedPresentationErrorCode.STREAM_FAILURE,
                "mixed playback presentation stream failed",
            ) from None
        except Exception:
            if boundary_error is not None:
                raise boundary_error from None
            raise MixedPresentationError(
                MixedPresentationErrorCode.STREAM_FAILURE,
                "mixed playback presentation stream failed",
            ) from None

        async with self._consumer_lock:
            self._completed_streams += 1
            self._max_source_span_ms = max(self._max_source_span_ms, snapshot.source_span_ms)

    @staticmethod
    async def _cancel_tasks(tasks: Sequence[asyncio.Task[None]]) -> None:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def run(
        self,
        streams: Sequence[MixedPresentationStream],
        consumer: MixedPresentationFrameConsumer,
    ) -> MixedPresentationSnapshot:
        """Run one bounded mixed live/playback presentation."""
        if self._state != MixedPresentationState.CREATED:
            raise MixedPresentationError(
                MixedPresentationErrorCode.INVALID_STATE,
                "mixed presentation cannot be reused",
            )
        if not callable(consumer):
            raise TypeError("consumer must be callable")

        selected = self._validate_streams(streams)
        self._stream_count = len(selected)
        self._live_streams = sum(isinstance(stream, MixedLiveStream) for stream in selected)
        self._playback_streams = self._stream_count - self._live_streams
        self._state = MixedPresentationState.RUNNING
        tasks: list[asyncio.Task[None]] = []
        for stream in selected:
            if isinstance(stream, MixedLiveStream):
                coroutine = self._run_live(stream, consumer)
            else:
                coroutine = self._run_playback(stream, consumer)
            tasks.append(
                asyncio.create_task(
                    coroutine,
                    name=f"k5-mixed-presentation-slot-{stream.slot}",
                )
            )

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            await self._cancel_tasks(tasks)
            self._state = MixedPresentationState.CANCELLED
            raise
        except MixedPresentationError:
            await self._cancel_tasks(tasks)
            self._state = MixedPresentationState.FAILED
            raise
        except Exception:
            await self._cancel_tasks(tasks)
            self._state = MixedPresentationState.FAILED
            raise MixedPresentationError(
                MixedPresentationErrorCode.STREAM_FAILURE,
                "mixed presentation failed",
            ) from None

        self._state = MixedPresentationState.COMPLETE
        return self.snapshot
