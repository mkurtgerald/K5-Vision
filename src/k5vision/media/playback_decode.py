"""Project-owned decoder/frame-consumer boundary for paced playback.

A concrete decoder runtime is deliberately injected behind ``PlaybackDecoder``.
Decoded frame payloads are transient: retained state contains counters/timing only.
"""

from __future__ import annotations

import asyncio
import enum
import pathlib
import typing
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from k5vision.media.playback_pump import (
    BoundedPlaybackPump,
    PlaybackPumpError,
    PlaybackPumpErrorCode,
)
from k5vision.media.playback_schedule import PlaybackRate
from k5vision.media.recording_descriptor import RecordingStreamDescriptor

_MAX_FRAMES = 1_000_000
_MAX_FRAME_BYTES = 128 * 1024 * 1024
_MAX_FRAMES_PER_PACKET = 32
_MAX_SOURCE_ELAPSED_MS = 2_147_483_647


@dataclass(frozen=True, slots=True)
class DecodedVideoFrame:
    """Transient decoded frame passed across the project-owned boundary."""

    payload: memoryview
    source_elapsed_ms: int


class PlaybackDecoder(Protocol):
    async def decode(
        self,
        packet: memoryview,
        source_elapsed_ms: int,
    ) -> Sequence[DecodedVideoFrame]: ...

    async def flush(self) -> Sequence[DecodedVideoFrame]: ...

    async def close(self) -> None: ...


FrameConsumer = Callable[[memoryview, int], Awaitable[None]]


class PlaybackDecodeState(enum.StrEnum):
    CREATED = "created"
    RUNNING = "running"
    COMPLETE = "complete"
    CANCELLED = "cancelled"
    FAILED = "failed"


class PlaybackDecodeErrorCode(enum.StrEnum):
    INVALID_STATE = "invalid_state"
    INVALID_DECODER = "invalid_decoder"
    PUMP_FAILURE = "pump_failure"
    DECODER_TIMEOUT = "decoder_timeout"
    DECODER_FAILURE = "decoder_failure"
    INVALID_FRAME = "invalid_frame"
    FRAME_LIMIT = "frame_limit"
    CONSUMER_TIMEOUT = "consumer_timeout"
    CONSUMER_FAILURE = "consumer_failure"
    CLEANUP_FAILURE = "cleanup_failure"


class PlaybackDecodeError(RuntimeError):
    """Sanitized decoder-boundary failure."""

    def __init__(
        self,
        code: PlaybackDecodeErrorCode,
        message: str,
        *,
        pump_error_code: PlaybackPumpErrorCode | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.pump_error_code = pump_error_code


class PlaybackDecodeSnapshot(BaseModel):
    """Source-free decoder/frame-consumer observability."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: typing.Literal["1"] = "1"
    state: PlaybackDecodeState
    decoded_frames: int = Field(ge=0, le=_MAX_FRAMES)
    decoded_bytes: int = Field(ge=0)
    source_span_ms: int = Field(ge=0)
    pump_delivered_packets: int = Field(ge=0)
    descriptor_verified: bool = False


class BoundedPlaybackDecodeBridge:
    """Feed paced RTP into an injected decoder and bounded frame consumer."""

    def __init__(
        self,
        path: pathlib.Path,
        descriptor: RecordingStreamDescriptor,
        start_ms: int,
        end_ms: int,
        decoder: PlaybackDecoder,
        rate: PlaybackRate = PlaybackRate.NORMAL,
        *,
        max_packets: int = 1_000_000,
        max_frames: int = _MAX_FRAMES,
        max_frames_per_packet: int = 8,
        max_frame_bytes: int = 32 * 1024 * 1024,
        decoder_timeout_seconds: float = 0.5,
        frame_consumer_timeout_seconds: float = 0.5,
        pump_consumer_timeout_seconds: float = 8.0,
        cleanup_timeout_seconds: float = 1.0,
    ) -> None:
        if not 1 <= max_frames <= _MAX_FRAMES:
            raise ValueError("max_frames must be between 1 and 1000000")
        if not 1 <= max_frames_per_packet <= _MAX_FRAMES_PER_PACKET:
            raise ValueError("max_frames_per_packet must be between 1 and 32")
        if not 1 <= max_frame_bytes <= _MAX_FRAME_BYTES:
            raise ValueError("max_frame_bytes must be between 1 and 134217728")
        for name, value in (
            ("decoder_timeout_seconds", decoder_timeout_seconds),
            ("frame_consumer_timeout_seconds", frame_consumer_timeout_seconds),
            ("cleanup_timeout_seconds", cleanup_timeout_seconds),
        ):
            if not 0 < value <= 10:
                raise ValueError(f"{name} must be between zero and 10")
        if not 0 < pump_consumer_timeout_seconds <= 10:
            raise ValueError("pump_consumer_timeout_seconds must be between zero and 10")
        if not all(callable(getattr(decoder, name, None)) for name in ("decode", "flush", "close")):
            raise PlaybackDecodeError(
                PlaybackDecodeErrorCode.INVALID_DECODER,
                "playback decoder does not satisfy the required boundary",
            )

        self._pump = BoundedPlaybackPump(
            path,
            descriptor,
            start_ms,
            end_ms,
            rate,
            max_packets=max_packets,
            consumer_timeout_seconds=pump_consumer_timeout_seconds,
        )
        self._decoder = decoder
        self._max_frames = max_frames
        self._max_frames_per_packet = max_frames_per_packet
        self._max_frame_bytes = max_frame_bytes
        self._decoder_timeout_seconds = decoder_timeout_seconds
        self._frame_consumer_timeout_seconds = frame_consumer_timeout_seconds
        self._cleanup_timeout_seconds = cleanup_timeout_seconds
        self._state = PlaybackDecodeState.CREATED
        self._decoded_frames = 0
        self._decoded_bytes = 0
        self._source_span_ms = 0
        self._pump_delivered_packets = 0
        self._descriptor_verified = False

    @property
    def snapshot(self) -> PlaybackDecodeSnapshot:
        return PlaybackDecodeSnapshot(
            state=self._state,
            decoded_frames=self._decoded_frames,
            decoded_bytes=self._decoded_bytes,
            source_span_ms=self._source_span_ms,
            pump_delivered_packets=self._pump_delivered_packets,
            descriptor_verified=self._descriptor_verified,
        )

    async def _decode(
        self,
        packet: memoryview,
        source_elapsed_ms: int,
    ) -> Sequence[DecodedVideoFrame]:
        try:
            return await asyncio.wait_for(
                self._decoder.decode(packet, source_elapsed_ms),
                timeout=self._decoder_timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            raise PlaybackDecodeError(
                PlaybackDecodeErrorCode.DECODER_TIMEOUT,
                "playback decoder timed out",
            ) from None
        except Exception:
            raise PlaybackDecodeError(
                PlaybackDecodeErrorCode.DECODER_FAILURE,
                "playback decoder failed",
            ) from None

    async def _emit_frames(
        self,
        frames: Sequence[DecodedVideoFrame],
        consumer: FrameConsumer,
    ) -> None:
        if len(frames) > self._max_frames_per_packet:
            raise PlaybackDecodeError(
                PlaybackDecodeErrorCode.FRAME_LIMIT,
                "playback decoder emitted too many frames for one packet",
            )
        for frame in frames:
            if not isinstance(frame, DecodedVideoFrame):
                raise PlaybackDecodeError(
                    PlaybackDecodeErrorCode.INVALID_FRAME,
                    "playback decoder emitted an invalid frame",
                )
            if not 0 <= frame.source_elapsed_ms <= _MAX_SOURCE_ELAPSED_MS:
                raise PlaybackDecodeError(
                    PlaybackDecodeErrorCode.INVALID_FRAME,
                    "playback decoder emitted invalid frame timing",
                )
            frame_bytes = len(frame.payload)
            if not 1 <= frame_bytes <= self._max_frame_bytes:
                raise PlaybackDecodeError(
                    PlaybackDecodeErrorCode.INVALID_FRAME,
                    "playback decoder emitted an invalid frame size",
                )
            if self._decoded_frames >= self._max_frames:
                raise PlaybackDecodeError(
                    PlaybackDecodeErrorCode.FRAME_LIMIT,
                    "playback decoded-frame limit exceeded",
                )
            try:
                await asyncio.wait_for(
                    consumer(frame.payload, frame.source_elapsed_ms),
                    timeout=self._frame_consumer_timeout_seconds,
                )
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                raise PlaybackDecodeError(
                    PlaybackDecodeErrorCode.CONSUMER_TIMEOUT,
                    "playback frame consumer timed out",
                ) from None
            except Exception:
                raise PlaybackDecodeError(
                    PlaybackDecodeErrorCode.CONSUMER_FAILURE,
                    "playback frame consumer failed",
                ) from None
            self._decoded_frames += 1
            self._decoded_bytes += frame_bytes
            self._source_span_ms = max(self._source_span_ms, frame.source_elapsed_ms)

    async def _close_decoder(self) -> None:
        try:
            await asyncio.wait_for(
                self._decoder.close(),
                timeout=self._cleanup_timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            raise PlaybackDecodeError(
                PlaybackDecodeErrorCode.CLEANUP_FAILURE,
                "playback decoder cleanup failed",
            ) from None

    async def run(self, consumer: FrameConsumer) -> PlaybackDecodeSnapshot:
        """Run paced decode/consume exactly once with deterministic cleanup."""
        if self._state != PlaybackDecodeState.CREATED:
            raise PlaybackDecodeError(
                PlaybackDecodeErrorCode.INVALID_STATE,
                "playback decoder boundary cannot be reused",
            )
        if not callable(consumer):
            raise TypeError("consumer must be callable")

        self._state = PlaybackDecodeState.RUNNING
        primary_error: BaseException | None = None

        async def packet_consumer(packet: memoryview, source_elapsed_ms: int) -> None:
            frames = await self._decode(packet, source_elapsed_ms)
            await self._emit_frames(frames, consumer)

        try:
            try:
                pump_snapshot = await self._pump.run(packet_consumer)
                try:
                    flush_frames = await asyncio.wait_for(
                        self._decoder.flush(),
                        timeout=self._decoder_timeout_seconds,
                    )
                except asyncio.CancelledError:
                    raise
                except TimeoutError:
                    raise PlaybackDecodeError(
                        PlaybackDecodeErrorCode.DECODER_TIMEOUT,
                        "playback decoder flush timed out",
                    ) from None
                except Exception:
                    raise PlaybackDecodeError(
                        PlaybackDecodeErrorCode.DECODER_FAILURE,
                        "playback decoder flush failed",
                    ) from None
                await self._emit_frames(flush_frames, consumer)
                self._pump_delivered_packets = pump_snapshot.delivered_packets
                self._descriptor_verified = pump_snapshot.descriptor_verified
                self._state = PlaybackDecodeState.COMPLETE
            except asyncio.CancelledError as exc:
                self._state = PlaybackDecodeState.CANCELLED
                primary_error = exc
                raise
            except PlaybackDecodeError as exc:
                self._state = PlaybackDecodeState.FAILED
                primary_error = exc
                raise
            except PlaybackPumpError as exc:
                self._state = PlaybackDecodeState.FAILED
                wrapped = PlaybackDecodeError(
                    PlaybackDecodeErrorCode.PUMP_FAILURE,
                    "playback pump failed",
                    pump_error_code=exc.code,
                )
                primary_error = wrapped
                raise wrapped from None
            return self.snapshot
        finally:
            try:
                await self._close_decoder()
            except asyncio.CancelledError:
                if primary_error is None:
                    self._state = PlaybackDecodeState.CANCELLED
                    raise
            except PlaybackDecodeError:
                if primary_error is None:
                    self._state = PlaybackDecodeState.FAILED
                    raise
