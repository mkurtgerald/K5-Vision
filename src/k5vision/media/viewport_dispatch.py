"""Bounded transient dispatch from logical presentation slots to viewport consumers.

This module is renderer-neutral. It forwards each presentation frame directly to a
pre-registered async consumer and retains no frame payload after dispatch returns.
Only aggregate lifecycle and counters remain observable.
"""

from __future__ import annotations

import asyncio
import enum
import typing
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

from k5vision.media.presentation_frame import PresentationVideoFrame

_MAX_VIEWPORTS = 16
_MAX_SLOT = 63
_MAX_TOTAL_FRAMES = 1_000_000
_MAX_FRAME_BYTES = 256 * 1024 * 1024
_MAX_TOTAL_FRAME_BYTES = 16 * 1024 * 1024 * 1024
_MAX_SOURCE_SPAN_MS = 2_147_483_647

ViewportConsumer = Callable[[PresentationVideoFrame], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class ViewportBinding:
    """Bind one logical slot to a caller-owned renderer-neutral consumer."""

    slot: int
    consumer: ViewportConsumer

    def __post_init__(self) -> None:
        if not 0 <= self.slot <= _MAX_SLOT:
            raise ValueError("slot must be between 0 and 63")
        if not callable(self.consumer):
            raise TypeError("consumer must be callable")


class ViewportDispatchState(enum.StrEnum):
    OPEN = "open"
    FAILED = "failed"
    CLOSED = "closed"


class ViewportDispatchErrorCode(enum.StrEnum):
    INVALID_STATE = "invalid_state"
    INVALID_BINDINGS = "invalid_bindings"
    UNKNOWN_SLOT = "unknown_slot"
    INVALID_FRAME = "invalid_frame"
    FRAME_LIMIT = "frame_limit"
    FRAME_BYTES_LIMIT = "frame_bytes_limit"
    TOTAL_BYTES_LIMIT = "total_bytes_limit"
    CONSUMER_TIMEOUT = "consumer_timeout"
    CONSUMER_FAILURE = "consumer_failure"


class ViewportDispatchError(RuntimeError):
    """Sanitized viewport dispatch failure."""

    def __init__(self, code: ViewportDispatchErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class ViewportDispatchSnapshot(BaseModel):
    """Payload/source/path/identifier-free dispatch observability."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: typing.Literal["1"] = "1"
    state: ViewportDispatchState
    viewport_count: int = Field(ge=0, le=_MAX_VIEWPORTS)
    delivered_frames: int = Field(ge=0, le=_MAX_TOTAL_FRAMES)
    delivered_frame_bytes: int = Field(ge=0, le=_MAX_TOTAL_FRAME_BYTES)
    max_source_span_ms: int = Field(ge=0, le=_MAX_SOURCE_SPAN_MS)


class BoundedViewportDispatcher:
    """Dispatch presentation frames without retaining media payloads."""

    def __init__(
        self,
        bindings: Sequence[ViewportBinding],
        *,
        max_viewports: int = 16,
        max_total_frames: int = 100_000,
        max_frame_bytes: int = 64 * 1024 * 1024,
        max_total_frame_bytes: int = 4 * 1024 * 1024 * 1024,
        consumer_timeout_seconds: float = 0.5,
    ) -> None:
        if not 1 <= max_viewports <= _MAX_VIEWPORTS:
            raise ValueError("max_viewports must be between 1 and 16")
        if not 1 <= max_total_frames <= _MAX_TOTAL_FRAMES:
            raise ValueError("max_total_frames must be between 1 and 1000000")
        if not 1 <= max_frame_bytes <= _MAX_FRAME_BYTES:
            raise ValueError("max_frame_bytes must be between 1 and 268435456")
        if not 1 <= max_total_frame_bytes <= _MAX_TOTAL_FRAME_BYTES:
            raise ValueError("max_total_frame_bytes must be between 1 and 17179869184")
        if not 0 < consumer_timeout_seconds <= 10:
            raise ValueError("consumer_timeout_seconds must be between zero and 10")

        selected = tuple(bindings)
        if not 1 <= len(selected) <= max_viewports:
            raise ViewportDispatchError(
                ViewportDispatchErrorCode.INVALID_BINDINGS,
                "viewport binding count is outside the configured bound",
            )
        if any(not isinstance(binding, ViewportBinding) for binding in selected):
            raise ViewportDispatchError(
                ViewportDispatchErrorCode.INVALID_BINDINGS,
                "viewport binding set is invalid",
            )
        slots = [binding.slot for binding in selected]
        if len(set(slots)) != len(slots):
            raise ViewportDispatchError(
                ViewportDispatchErrorCode.INVALID_BINDINGS,
                "viewport binding slots must be unique",
            )

        self._consumers: dict[int, ViewportConsumer] = {
            binding.slot: binding.consumer for binding in selected
        }
        self._viewport_count = len(self._consumers)
        self._max_total_frames = max_total_frames
        self._max_frame_bytes = max_frame_bytes
        self._max_total_frame_bytes = max_total_frame_bytes
        self._consumer_timeout_seconds = consumer_timeout_seconds
        self._state = ViewportDispatchState.OPEN
        self._delivered_frames = 0
        self._delivered_frame_bytes = 0
        self._max_source_span_ms = 0
        self._lock = asyncio.Lock()

    @property
    def snapshot(self) -> ViewportDispatchSnapshot:
        return ViewportDispatchSnapshot(
            state=self._state,
            viewport_count=self._viewport_count,
            delivered_frames=self._delivered_frames,
            delivered_frame_bytes=self._delivered_frame_bytes,
            max_source_span_ms=self._max_source_span_ms,
        )

    def _fail(self) -> None:
        self._state = ViewportDispatchState.FAILED
        self._consumers.clear()

    async def dispatch(self, slot: int, frame: PresentationVideoFrame) -> None:
        """Synchronously hand one transient frame to its registered viewport consumer."""
        async with self._lock:
            if self._state != ViewportDispatchState.OPEN:
                raise ViewportDispatchError(
                    ViewportDispatchErrorCode.INVALID_STATE,
                    "viewport dispatcher is not open",
                )
            consumer = self._consumers.get(slot)
            if consumer is None:
                raise ViewportDispatchError(
                    ViewportDispatchErrorCode.UNKNOWN_SLOT,
                    "viewport slot is not registered",
                )
            if not isinstance(frame, PresentationVideoFrame):
                raise ViewportDispatchError(
                    ViewportDispatchErrorCode.INVALID_FRAME,
                    "viewport dispatch frame is invalid",
                )

            frame_bytes = len(frame.payload)
            if self._delivered_frames >= self._max_total_frames:
                self._fail()
                raise ViewportDispatchError(
                    ViewportDispatchErrorCode.FRAME_LIMIT,
                    "viewport aggregate frame limit exceeded",
                )
            if frame_bytes > self._max_frame_bytes:
                self._fail()
                raise ViewportDispatchError(
                    ViewportDispatchErrorCode.FRAME_BYTES_LIMIT,
                    "viewport frame-byte limit exceeded",
                )
            if self._delivered_frame_bytes + frame_bytes > self._max_total_frame_bytes:
                self._fail()
                raise ViewportDispatchError(
                    ViewportDispatchErrorCode.TOTAL_BYTES_LIMIT,
                    "viewport aggregate frame-byte limit exceeded",
                )

            try:
                await asyncio.wait_for(
                    consumer(frame),
                    timeout=self._consumer_timeout_seconds,
                )
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                self._fail()
                raise ViewportDispatchError(
                    ViewportDispatchErrorCode.CONSUMER_TIMEOUT,
                    "viewport consumer timed out",
                ) from None
            except Exception:
                self._fail()
                raise ViewportDispatchError(
                    ViewportDispatchErrorCode.CONSUMER_FAILURE,
                    "viewport consumer failed",
                ) from None

            self._delivered_frames += 1
            self._delivered_frame_bytes += frame_bytes
            self._max_source_span_ms = max(
                self._max_source_span_ms,
                frame.source_elapsed_ms,
            )

    async def close(self) -> ViewportDispatchSnapshot:
        """Release viewport consumer references without retaining frame payloads."""
        async with self._lock:
            self._consumers.clear()
            if self._state == ViewportDispatchState.OPEN:
                self._state = ViewportDispatchState.CLOSED
            return self.snapshot
