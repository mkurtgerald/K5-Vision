"""Bounded playback-to-presentation delivery boundary.

This module composes the accepted paced playback pump with the accepted
presentation-ready GStreamer decoder. Decoded frame payloads remain transient and
are handed only to a caller-provided async consumer; retained state contains
counters/lifecycle metadata only.
"""

from __future__ import annotations

import asyncio
import enum
import pathlib
import time
import typing
from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from k5vision.media.gstreamer_playback_decoder import NativePlaybackDecoderError
from k5vision.media.playback_pump import (
    BoundedPlaybackPump,
    Clock,
    PlaybackPumpError,
    Sleeper,
)
from k5vision.media.playback_schedule import PlaybackRate
from k5vision.media.presentation_decoder import GStreamerPresentationDecoder
from k5vision.media.presentation_frame import PresentationVideoFrame
from k5vision.media.recording_descriptor import RecordingStreamDescriptor, VideoCodec

_MAX_FRAMES = 1_000_000
_MAX_FRAME_BYTES = 128 * 1024 * 1024
_MAX_FRAMES_PER_PACKET = 32
_MAX_SOURCE_ELAPSED_MS = 2_147_483_647


class PresentationDecoder(Protocol):
    async def decode(
        self,
        packet: memoryview,
        source_elapsed_ms: int,
    ) -> Sequence[PresentationVideoFrame]: ...

    async def flush(self) -> Sequence[PresentationVideoFrame]: ...

    async def close(self) -> None: ...


PresentationDecoderFactory = Callable[[int], PresentationDecoder]
PresentationFrameConsumer = Callable[[PresentationVideoFrame], Awaitable[None]]


class PresentationPlaybackState(enum.StrEnum):
    CREATED = "created"
    RUNNING = "running"
    COMPLETE = "complete"
    CANCELLED = "cancelled"
    FAILED = "failed"


class PresentationPlaybackErrorCode(enum.StrEnum):
    INVALID_STATE = "invalid_state"
    UNSUPPORTED_CODEC = "unsupported_codec"
    DECODER_INIT_FAILURE = "decoder_init_failure"
    PUMP_FAILURE = "pump_failure"
    DECODER_TIMEOUT = "decoder_timeout"
    DECODER_FAILURE = "decoder_failure"
    INVALID_FRAME = "invalid_frame"
    FRAME_LIMIT = "frame_limit"
    CONSUMER_TIMEOUT = "consumer_timeout"
    CONSUMER_FAILURE = "consumer_failure"
    CLEANUP_FAILURE = "cleanup_failure"


class PresentationPlaybackError(RuntimeError):
    """Sanitized presentation-playback failure."""

    def __init__(self, code: PresentationPlaybackErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class PresentationPlaybackSnapshot(BaseModel):
    """Source-free retained observability for one playback delivery."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: typing.Literal["1"] = "1"
    state: PresentationPlaybackState
    decoder_initialized: bool = False
    delivered_frames: int = Field(ge=0, le=_MAX_FRAMES)
    delivered_bytes: int = Field(ge=0)
    source_span_ms: int = Field(ge=0)
    pump_delivered_packets: int = Field(ge=0)
    late_packets: int = Field(ge=0)
    descriptor_verified: bool = False


class BoundedPresentationPlaybackDelivery:
    """Run one recording window through decoder and transient frame consumer."""

    def __init__(
        self,
        path: pathlib.Path,
        descriptor: RecordingStreamDescriptor,
        start_ms: int,
        end_ms: int,
        rate: PlaybackRate = PlaybackRate.NORMAL,
        *,
        runtime_root: pathlib.Path | None = None,
        decoder_factory: PresentationDecoderFactory | None = None,
        max_packets: int = 1_000_000,
        max_frames: int = _MAX_FRAMES,
        max_frames_per_packet: int = 8,
        max_frame_bytes: int = 32 * 1024 * 1024,
        decoder_timeout_seconds: float = 0.5,
        frame_consumer_timeout_seconds: float = 0.5,
        pump_consumer_timeout_seconds: float = 8.0,
        cleanup_timeout_seconds: float = 1.0,
        clock: Clock = time.monotonic,
        sleep: Sleeper = asyncio.sleep,
    ) -> None:
        if descriptor.codec != VideoCodec.H264:
            raise PresentationPlaybackError(
                PresentationPlaybackErrorCode.UNSUPPORTED_CODEC,
                "presentation playback codec is unsupported",
            )
        if decoder_factory is not None and not callable(decoder_factory):
            raise TypeError("decoder_factory must be callable")
        if not 1 <= max_frames <= _MAX_FRAMES:
            raise ValueError("max_frames must be between 1 and 1000000")
        if not 1 <= max_frames_per_packet <= _MAX_FRAMES_PER_PACKET:
            raise ValueError("max_frames_per_packet must be between 1 and 32")
        if not 1 <= max_frame_bytes <= _MAX_FRAME_BYTES:
            raise ValueError("max_frame_bytes must be between 1 and 134217728")
        for name, value in (
            ("decoder_timeout_seconds", decoder_timeout_seconds),
            ("frame_consumer_timeout_seconds", frame_consumer_timeout_seconds),
            ("pump_consumer_timeout_seconds", pump_consumer_timeout_seconds),
            ("cleanup_timeout_seconds", cleanup_timeout_seconds),
        ):
            if not 0 < value <= 10:
                raise ValueError(f"{name} must be between zero and 10")

        self._descriptor = descriptor
        self._runtime_root = runtime_root
        self._decoder_factory = decoder_factory
        self._max_frames = max_frames
        self._max_frames_per_packet = max_frames_per_packet
        self._max_frame_bytes = max_frame_bytes
        self._decoder_timeout_seconds = decoder_timeout_seconds
        self._frame_consumer_timeout_seconds = frame_consumer_timeout_seconds
        self._cleanup_timeout_seconds = cleanup_timeout_seconds
        self._pump = BoundedPlaybackPump(
            pathlib.Path(path),
            descriptor,
            start_ms,
            end_ms,
            rate,
            max_packets=max_packets,
            consumer_timeout_seconds=pump_consumer_timeout_seconds,
            clock=clock,
            sleep=sleep,
        )
        self._state = PresentationPlaybackState.CREATED
        self._decoder_initialized = False
        self._delivered_frames = 0
        self._delivered_bytes = 0
        self._source_span_ms = 0
        self._pump_delivered_packets = 0
        self._late_packets = 0
        self._descriptor_verified = False

    @property
    def snapshot(self) -> PresentationPlaybackSnapshot:
        return PresentationPlaybackSnapshot(
            state=self._state,
            decoder_initialized=self._decoder_initialized,
            delivered_frames=self._delivered_frames,
            delivered_bytes=self._delivered_bytes,
            source_span_ms=self._source_span_ms,
            pump_delivered_packets=self._pump_delivered_packets,
            late_packets=self._late_packets,
            descriptor_verified=self._descriptor_verified,
        )

    def _new_decoder(self) -> PresentationDecoder:
        try:
            if self._decoder_factory is not None:
                decoder = self._decoder_factory(self._descriptor.payload_type)
            else:
                decoder = GStreamerPresentationDecoder(
                    self._descriptor.payload_type,
                    runtime_root=self._runtime_root,
                    max_frame_bytes=self._max_frame_bytes,
                    max_frames_per_push=self._max_frames_per_packet,
                    operation_timeout_seconds=self._decoder_timeout_seconds,
                )
        except NativePlaybackDecoderError:
            raise PresentationPlaybackError(
                PresentationPlaybackErrorCode.DECODER_INIT_FAILURE,
                "presentation playback decoder could not be initialized",
            ) from None
        except Exception:
            raise PresentationPlaybackError(
                PresentationPlaybackErrorCode.DECODER_INIT_FAILURE,
                "presentation playback decoder could not be initialized",
            ) from None

        if not all(callable(getattr(decoder, name, None)) for name in ("decode", "flush", "close")):
            raise PresentationPlaybackError(
                PresentationPlaybackErrorCode.DECODER_INIT_FAILURE,
                "presentation playback decoder does not satisfy the required boundary",
            )
        self._decoder_initialized = True
        return decoder

    async def _decode(
        self,
        decoder: PresentationDecoder,
        packet: memoryview,
        source_elapsed_ms: int,
    ) -> Sequence[PresentationVideoFrame]:
        try:
            return await asyncio.wait_for(
                decoder.decode(packet, source_elapsed_ms),
                timeout=self._decoder_timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            raise PresentationPlaybackError(
                PresentationPlaybackErrorCode.DECODER_TIMEOUT,
                "presentation playback decoder timed out",
            ) from None
        except Exception:
            raise PresentationPlaybackError(
                PresentationPlaybackErrorCode.DECODER_FAILURE,
                "presentation playback decoder failed",
            ) from None

    async def _emit_frames(
        self,
        frames: Sequence[PresentationVideoFrame],
        consumer: PresentationFrameConsumer,
    ) -> None:
        if len(frames) > self._max_frames_per_packet:
            raise PresentationPlaybackError(
                PresentationPlaybackErrorCode.FRAME_LIMIT,
                "presentation playback decoder emitted too many frames",
            )
        for frame in frames:
            if not isinstance(frame, PresentationVideoFrame):
                raise PresentationPlaybackError(
                    PresentationPlaybackErrorCode.INVALID_FRAME,
                    "presentation playback decoder emitted an invalid frame",
                )
            if not 0 <= frame.source_elapsed_ms <= _MAX_SOURCE_ELAPSED_MS:
                raise PresentationPlaybackError(
                    PresentationPlaybackErrorCode.INVALID_FRAME,
                    "presentation playback decoder emitted invalid timing",
                )
            frame_bytes = len(frame.payload)
            if not 1 <= frame_bytes <= self._max_frame_bytes:
                raise PresentationPlaybackError(
                    PresentationPlaybackErrorCode.INVALID_FRAME,
                    "presentation playback decoder emitted an invalid frame size",
                )
            if self._delivered_frames >= self._max_frames:
                raise PresentationPlaybackError(
                    PresentationPlaybackErrorCode.FRAME_LIMIT,
                    "presentation playback frame limit exceeded",
                )
            try:
                await asyncio.wait_for(
                    consumer(frame),
                    timeout=self._frame_consumer_timeout_seconds,
                )
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                raise PresentationPlaybackError(
                    PresentationPlaybackErrorCode.CONSUMER_TIMEOUT,
                    "presentation frame consumer timed out",
                ) from None
            except Exception:
                raise PresentationPlaybackError(
                    PresentationPlaybackErrorCode.CONSUMER_FAILURE,
                    "presentation frame consumer failed",
                ) from None
            self._delivered_frames += 1
            self._delivered_bytes += frame_bytes
            self._source_span_ms = max(self._source_span_ms, frame.source_elapsed_ms)

    async def _flush(
        self,
        decoder: PresentationDecoder,
        consumer: PresentationFrameConsumer,
    ) -> None:
        try:
            frames = await asyncio.wait_for(
                decoder.flush(),
                timeout=self._decoder_timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            raise PresentationPlaybackError(
                PresentationPlaybackErrorCode.DECODER_TIMEOUT,
                "presentation playback decoder flush timed out",
            ) from None
        except Exception:
            raise PresentationPlaybackError(
                PresentationPlaybackErrorCode.DECODER_FAILURE,
                "presentation playback decoder flush failed",
            ) from None
        await self._emit_frames(frames, consumer)

    async def _close_decoder(self, decoder: PresentationDecoder) -> None:
        try:
            await asyncio.wait_for(decoder.close(), timeout=self._cleanup_timeout_seconds)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise PresentationPlaybackError(
                PresentationPlaybackErrorCode.CLEANUP_FAILURE,
                "presentation playback decoder cleanup failed",
            ) from None

    async def run(self, consumer: PresentationFrameConsumer) -> PresentationPlaybackSnapshot:
        """Execute one bounded recording-to-presentation-frame delivery."""
        if self._state != PresentationPlaybackState.CREATED:
            raise PresentationPlaybackError(
                PresentationPlaybackErrorCode.INVALID_STATE,
                "presentation playback delivery cannot be reused",
            )
        if not callable(consumer):
            raise TypeError("consumer must be callable")

        self._state = PresentationPlaybackState.RUNNING
        decoder: PresentationDecoder | None = None
        primary_error: BaseException | None = None
        boundary_error: PresentationPlaybackError | None = None

        try:
            decoder = self._new_decoder()

            async def packet_consumer(packet: memoryview, source_elapsed_ms: int) -> None:
                nonlocal boundary_error
                try:
                    frames = await self._decode(decoder, packet, source_elapsed_ms)
                    await self._emit_frames(frames, consumer)
                except PresentationPlaybackError as exc:
                    boundary_error = exc
                    raise

            try:
                pump_snapshot = await self._pump.run(packet_consumer)
                await self._flush(decoder, consumer)
                self._pump_delivered_packets = pump_snapshot.delivered_packets
                self._late_packets = pump_snapshot.late_packets
                self._descriptor_verified = pump_snapshot.descriptor_verified
                self._state = PresentationPlaybackState.COMPLETE
                return self.snapshot
            except asyncio.CancelledError as exc:
                self._state = PresentationPlaybackState.CANCELLED
                primary_error = exc
                raise
            except PresentationPlaybackError as exc:
                self._state = PresentationPlaybackState.FAILED
                primary_error = exc
                raise
            except PlaybackPumpError:
                self._state = PresentationPlaybackState.FAILED
                if boundary_error is not None:
                    primary_error = boundary_error
                    raise boundary_error from None
                wrapped = PresentationPlaybackError(
                    PresentationPlaybackErrorCode.PUMP_FAILURE,
                    "presentation playback pump failed",
                )
                primary_error = wrapped
                raise wrapped from None
        finally:
            if decoder is not None:
                try:
                    await self._close_decoder(decoder)
                except asyncio.CancelledError:
                    if primary_error is None:
                        self._state = PresentationPlaybackState.CANCELLED
                        raise
                except PresentationPlaybackError:
                    if primary_error is None:
                        self._state = PresentationPlaybackState.FAILED
                        raise
