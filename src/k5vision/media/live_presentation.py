"""Bounded live RTP-to-presentation delivery boundary.

This module composes the accepted ephemeral RTP delivery path with the accepted
presentation-ready GStreamer decoder. Camera/source identity remains an execution
input only; retained state contains bounded counters/lifecycle metadata and never
contains source URIs, credentials, paths, RTP payloads, or decoded frame bytes.
"""

from __future__ import annotations

import asyncio
import enum
import pathlib
import typing
from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from k5vision.media.gstreamer_playback_decoder import NativePlaybackDecoderError
from k5vision.media.presentation_decoder import GStreamerPresentationDecoder
from k5vision.media.presentation_frame import PresentationVideoFrame
from k5vision.media.rtp_delivery import (
    EphemeralRtpDelivery,
    RtpDeliveryError,
    RtpDeliveryErrorCode,
    RtpDeliveryResult,
)

_MAX_FRAMES = 1_000_000
_MAX_FRAME_BYTES = 128 * 1024 * 1024
_MAX_FRAMES_PER_PACKET = 32
_MAX_SOURCE_SPAN_MS = 600_000
_RTP_TIMESTAMP_MODULUS = 1 << 32


class LivePresentationDecoder(Protocol):
    async def decode(
        self,
        packet: memoryview,
        source_elapsed_ms: int,
    ) -> Sequence[PresentationVideoFrame]: ...

    async def flush(self) -> Sequence[PresentationVideoFrame]: ...

    async def close(self) -> None: ...


class LiveRtpDelivery(Protocol):
    async def deliver(
        self,
        source_uri: str,
        consumer: Callable[[memoryview], Awaitable[None]],
    ) -> RtpDeliveryResult: ...


LivePresentationDecoderFactory = Callable[[int], LivePresentationDecoder]
LivePresentationFrameConsumer = Callable[[PresentationVideoFrame], Awaitable[None]]


class LivePresentationState(enum.StrEnum):
    CREATED = "created"
    RUNNING = "running"
    COMPLETE = "complete"
    CANCELLED = "cancelled"
    FAILED = "failed"


class LivePresentationErrorCode(enum.StrEnum):
    INVALID_STATE = "invalid_state"
    INVALID_RTP = "invalid_rtp"
    DELIVERY_TIMEOUT = "delivery_timeout"
    DELIVERY_FAILURE = "delivery_failure"
    DECODER_INIT_FAILURE = "decoder_init_failure"
    DECODER_TIMEOUT = "decoder_timeout"
    DECODER_FAILURE = "decoder_failure"
    INVALID_FRAME = "invalid_frame"
    FRAME_LIMIT = "frame_limit"
    CONSUMER_TIMEOUT = "consumer_timeout"
    CONSUMER_FAILURE = "consumer_failure"
    CLEANUP_FAILURE = "cleanup_failure"


class LivePresentationError(RuntimeError):
    """Sanitized live-presentation failure."""

    def __init__(self, code: LivePresentationErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class LivePresentationSnapshot(BaseModel):
    """Source-free retained observability for one bounded live delivery."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: typing.Literal["1"] = "1"
    state: LivePresentationState
    decoder_initialized: bool = False
    accepted_packets: int = Field(ge=0, le=4096)
    rtp_valid_packets: int = Field(ge=0, le=4096)
    rtp_invalid_packets: int = Field(ge=0, le=4096)
    rtp_delivered_bytes: int = Field(ge=0)
    delivered_frames: int = Field(ge=0, le=_MAX_FRAMES)
    delivered_frame_bytes: int = Field(ge=0)
    source_span_ms: int = Field(ge=0, le=_MAX_SOURCE_SPAN_MS)


class BoundedLivePresentationDelivery:
    """Decode one bounded live RTP sample into transient presentation frames."""

    def __init__(
        self,
        payload_type: int,
        *,
        clock_rate_hz: int = 90_000,
        runtime_root: pathlib.Path | None = None,
        decoder_factory: LivePresentationDecoderFactory | None = None,
        rtp_delivery: LiveRtpDelivery | None = None,
        packet_goal: int = 256,
        delivery_timeout_seconds: float = 15.0,
        packet_consumer_timeout_seconds: float = 4.0,
        max_frames: int = _MAX_FRAMES,
        max_frames_per_packet: int = 8,
        max_frame_bytes: int = 32 * 1024 * 1024,
        max_source_span_ms: int = 60_000,
        decoder_timeout_seconds: float = 0.5,
        frame_consumer_timeout_seconds: float = 0.5,
        cleanup_timeout_seconds: float = 1.0,
        relay_startup_probe_seconds: float = 0.15,
    ) -> None:
        if not 96 <= payload_type <= 127:
            raise ValueError("payload_type must be a dynamic RTP payload type")
        if clock_rate_hz != 90_000:
            raise ValueError("supported live video RTP clock rate is 90000 Hz")
        if decoder_factory is not None and not callable(decoder_factory):
            raise TypeError("decoder_factory must be callable")
        if not 1 <= packet_goal <= 4096:
            raise ValueError("packet_goal must be between 1 and 4096")
        if not 1 <= max_frames <= _MAX_FRAMES:
            raise ValueError("max_frames must be between 1 and 1000000")
        if not 1 <= max_frames_per_packet <= _MAX_FRAMES_PER_PACKET:
            raise ValueError("max_frames_per_packet must be between 1 and 32")
        if not 1 <= max_frame_bytes <= _MAX_FRAME_BYTES:
            raise ValueError("max_frame_bytes must be between 1 and 134217728")
        if not 1 <= max_source_span_ms <= _MAX_SOURCE_SPAN_MS:
            raise ValueError("max_source_span_ms must be between 1 and 600000")
        for name, value in (
            ("delivery_timeout_seconds", delivery_timeout_seconds),
            ("packet_consumer_timeout_seconds", packet_consumer_timeout_seconds),
            ("decoder_timeout_seconds", decoder_timeout_seconds),
            ("frame_consumer_timeout_seconds", frame_consumer_timeout_seconds),
            ("cleanup_timeout_seconds", cleanup_timeout_seconds),
        ):
            if not 0 < value <= 60:
                raise ValueError(f"{name} must be between zero and 60")

        self._payload_type = payload_type
        self._clock_rate_hz = clock_rate_hz
        self._runtime_root = runtime_root
        self._decoder_factory = decoder_factory
        self._rtp_delivery = rtp_delivery or EphemeralRtpDelivery(
            packet_goal=packet_goal,
            delivery_timeout_seconds=delivery_timeout_seconds,
            consumer_timeout_seconds=packet_consumer_timeout_seconds,
            relay_startup_probe_seconds=relay_startup_probe_seconds,
        )
        self._max_frames = max_frames
        self._max_frames_per_packet = max_frames_per_packet
        self._max_frame_bytes = max_frame_bytes
        self._max_source_span_ms = max_source_span_ms
        self._decoder_timeout_seconds = decoder_timeout_seconds
        self._frame_consumer_timeout_seconds = frame_consumer_timeout_seconds
        self._cleanup_timeout_seconds = cleanup_timeout_seconds
        self._state = LivePresentationState.CREATED
        self._decoder_initialized = False
        self._accepted_packets = 0
        self._rtp_valid_packets = 0
        self._rtp_invalid_packets = 0
        self._rtp_delivered_bytes = 0
        self._delivered_frames = 0
        self._delivered_frame_bytes = 0
        self._source_span_ms = 0
        self._last_packet_elapsed_ms = 0
        self._rtp_timestamp_origin: int | None = None

    @property
    def snapshot(self) -> LivePresentationSnapshot:
        return LivePresentationSnapshot(
            state=self._state,
            decoder_initialized=self._decoder_initialized,
            accepted_packets=self._accepted_packets,
            rtp_valid_packets=self._rtp_valid_packets,
            rtp_invalid_packets=self._rtp_invalid_packets,
            rtp_delivered_bytes=self._rtp_delivered_bytes,
            delivered_frames=self._delivered_frames,
            delivered_frame_bytes=self._delivered_frame_bytes,
            source_span_ms=self._source_span_ms,
        )

    def _new_decoder(self) -> LivePresentationDecoder:
        try:
            if self._decoder_factory is not None:
                decoder = self._decoder_factory(self._payload_type)
            else:
                decoder = GStreamerPresentationDecoder(
                    self._payload_type,
                    runtime_root=self._runtime_root,
                    max_frame_bytes=self._max_frame_bytes,
                    max_frames_per_push=self._max_frames_per_packet,
                    operation_timeout_seconds=self._decoder_timeout_seconds,
                )
        except NativePlaybackDecoderError:
            raise LivePresentationError(
                LivePresentationErrorCode.DECODER_INIT_FAILURE,
                "live presentation decoder could not be initialized",
            ) from None
        except Exception:
            raise LivePresentationError(
                LivePresentationErrorCode.DECODER_INIT_FAILURE,
                "live presentation decoder could not be initialized",
            ) from None

        if not all(callable(getattr(decoder, name, None)) for name in ("decode", "flush", "close")):
            raise LivePresentationError(
                LivePresentationErrorCode.DECODER_INIT_FAILURE,
                "live presentation decoder does not satisfy the required boundary",
            )
        self._decoder_initialized = True
        return decoder

    def _source_elapsed_ms(self, packet: memoryview) -> int:
        if len(packet) < 12 or int(packet[1] & 0x7F) != self._payload_type:
            raise LivePresentationError(
                LivePresentationErrorCode.INVALID_RTP,
                "live presentation received unsupported RTP metadata",
            )
        timestamp = int.from_bytes(packet[4:8], "big")
        if self._rtp_timestamp_origin is None:
            self._rtp_timestamp_origin = timestamp
        delta_ticks = (timestamp - self._rtp_timestamp_origin) % _RTP_TIMESTAMP_MODULUS
        elapsed_ms = (delta_ticks * 1000) // self._clock_rate_hz
        if elapsed_ms > self._max_source_span_ms:
            raise LivePresentationError(
                LivePresentationErrorCode.INVALID_RTP,
                "live presentation RTP timing exceeded the bounded source span",
            )
        self._last_packet_elapsed_ms = elapsed_ms
        return elapsed_ms

    async def _decode(
        self,
        decoder: LivePresentationDecoder,
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
            raise LivePresentationError(
                LivePresentationErrorCode.DECODER_TIMEOUT,
                "live presentation decoder timed out",
            ) from None
        except Exception:
            raise LivePresentationError(
                LivePresentationErrorCode.DECODER_FAILURE,
                "live presentation decoder failed",
            ) from None

    async def _emit_frames(
        self,
        frames: Sequence[PresentationVideoFrame],
        expected_source_elapsed_ms: int,
        consumer: LivePresentationFrameConsumer,
    ) -> None:
        if len(frames) > self._max_frames_per_packet:
            raise LivePresentationError(
                LivePresentationErrorCode.FRAME_LIMIT,
                "live presentation decoder emitted too many frames",
            )
        for frame in frames:
            if not isinstance(frame, PresentationVideoFrame):
                raise LivePresentationError(
                    LivePresentationErrorCode.INVALID_FRAME,
                    "live presentation decoder emitted an invalid frame",
                )
            frame_bytes = len(frame.payload)
            if (
                frame.source_elapsed_ms != expected_source_elapsed_ms
                or not 1 <= frame_bytes <= self._max_frame_bytes
            ):
                raise LivePresentationError(
                    LivePresentationErrorCode.INVALID_FRAME,
                    "live presentation decoder emitted invalid frame metadata",
                )
            if self._delivered_frames >= self._max_frames:
                raise LivePresentationError(
                    LivePresentationErrorCode.FRAME_LIMIT,
                    "live presentation frame limit exceeded",
                )
            try:
                await asyncio.wait_for(
                    consumer(frame),
                    timeout=self._frame_consumer_timeout_seconds,
                )
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                raise LivePresentationError(
                    LivePresentationErrorCode.CONSUMER_TIMEOUT,
                    "live presentation frame consumer timed out",
                ) from None
            except Exception:
                raise LivePresentationError(
                    LivePresentationErrorCode.CONSUMER_FAILURE,
                    "live presentation frame consumer failed",
                ) from None
            self._delivered_frames += 1
            self._delivered_frame_bytes += frame_bytes
            self._source_span_ms = max(self._source_span_ms, frame.source_elapsed_ms)

    async def _flush(
        self,
        decoder: LivePresentationDecoder,
        consumer: LivePresentationFrameConsumer,
    ) -> None:
        try:
            frames = await asyncio.wait_for(
                decoder.flush(),
                timeout=self._decoder_timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            raise LivePresentationError(
                LivePresentationErrorCode.DECODER_TIMEOUT,
                "live presentation decoder flush timed out",
            ) from None
        except Exception:
            raise LivePresentationError(
                LivePresentationErrorCode.DECODER_FAILURE,
                "live presentation decoder flush failed",
            ) from None

        rebound: list[PresentationVideoFrame] = []
        for frame in frames:
            if not isinstance(frame, PresentationVideoFrame):
                raise LivePresentationError(
                    LivePresentationErrorCode.INVALID_FRAME,
                    "live presentation decoder emitted an invalid flush frame",
                )
            rebound.append(
                PresentationVideoFrame(
                    payload=frame.payload,
                    width=frame.width,
                    height=frame.height,
                    stride_bytes=frame.stride_bytes,
                    pixel_format=frame.pixel_format,
                    source_elapsed_ms=self._last_packet_elapsed_ms,
                )
            )
        await self._emit_frames(rebound, self._last_packet_elapsed_ms, consumer)

    async def _close_decoder(self, decoder: LivePresentationDecoder) -> None:
        try:
            await asyncio.wait_for(decoder.close(), timeout=self._cleanup_timeout_seconds)
        except asyncio.CancelledError:
            raise
        except Exception:
            raise LivePresentationError(
                LivePresentationErrorCode.CLEANUP_FAILURE,
                "live presentation decoder cleanup failed",
            ) from None

    async def run(
        self,
        source_uri: str,
        consumer: LivePresentationFrameConsumer,
    ) -> LivePresentationSnapshot:
        """Execute one bounded live RTP-to-presentation delivery."""
        if self._state != LivePresentationState.CREATED:
            raise LivePresentationError(
                LivePresentationErrorCode.INVALID_STATE,
                "live presentation delivery cannot be reused",
            )
        if not isinstance(source_uri, str) or not source_uri.strip():
            raise ValueError("source_uri must be a non-empty string")
        if not callable(consumer):
            raise TypeError("consumer must be callable")

        self._state = LivePresentationState.RUNNING
        decoder: LivePresentationDecoder | None = None
        primary_error: BaseException | None = None
        boundary_error: LivePresentationError | None = None

        try:
            decoder = self._new_decoder()

            async def packet_consumer(packet: memoryview) -> None:
                nonlocal boundary_error
                try:
                    source_elapsed_ms = self._source_elapsed_ms(packet)
                    frames = await self._decode(decoder, packet, source_elapsed_ms)
                    await self._emit_frames(frames, source_elapsed_ms, consumer)
                    self._accepted_packets += 1
                except LivePresentationError as exc:
                    boundary_error = exc
                    raise

            try:
                result = await self._rtp_delivery.deliver(source_uri, packet_consumer)
                await self._flush(decoder, consumer)
                self._rtp_valid_packets = result.valid_packets
                self._rtp_invalid_packets = result.invalid_packets
                self._rtp_delivered_bytes = result.delivered_bytes
                self._state = LivePresentationState.COMPLETE
                return self.snapshot
            except asyncio.CancelledError as exc:
                self._state = LivePresentationState.CANCELLED
                primary_error = exc
                raise
            except LivePresentationError as exc:
                self._state = LivePresentationState.FAILED
                primary_error = exc
                raise
            except RtpDeliveryError as exc:
                self._state = LivePresentationState.FAILED
                if boundary_error is not None:
                    primary_error = boundary_error
                    raise boundary_error from None
                code = (
                    LivePresentationErrorCode.DELIVERY_TIMEOUT
                    if exc.code == RtpDeliveryErrorCode.TIMEOUT
                    else LivePresentationErrorCode.DELIVERY_FAILURE
                )
                wrapped = LivePresentationError(code, "live RTP presentation delivery failed")
                primary_error = wrapped
                raise wrapped from None
        finally:
            if decoder is not None:
                try:
                    await self._close_decoder(decoder)
                except asyncio.CancelledError:
                    if primary_error is None:
                        self._state = LivePresentationState.CANCELLED
                        raise
                except LivePresentationError:
                    if primary_error is None:
                        self._state = LivePresentationState.FAILED
                        raise
