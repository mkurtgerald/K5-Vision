"""Bounded production playback-session composition.

This module wires the accepted paced playback/decode bridge to the accepted
concrete GStreamer decoder without exposing runtime paths or retaining media.
Decoded frames remain transient and are delivered only to the caller-provided
consumer. Observable state is counters/lifecycle metadata only.
"""

from __future__ import annotations

import asyncio
import enum
import pathlib
import typing
from collections.abc import Callable

from pydantic import BaseModel, ConfigDict, Field

from k5vision.media.gstreamer_playback_decoder import (
    GStreamerNativePlaybackDecoder,
    NativePlaybackDecoderError,
)
from k5vision.media.playback_decode import (
    BoundedPlaybackDecodeBridge,
    FrameConsumer,
    PlaybackDecodeError,
    PlaybackDecodeErrorCode,
    PlaybackDecoder,
)
from k5vision.media.playback_schedule import PlaybackRate
from k5vision.media.recording_descriptor import RecordingStreamDescriptor, VideoCodec

DecoderFactory = Callable[[int], PlaybackDecoder]


class PlaybackSessionState(enum.StrEnum):
    CREATED = "created"
    RUNNING = "running"
    COMPLETE = "complete"
    CANCELLED = "cancelled"
    FAILED = "failed"


class PlaybackSessionErrorCode(enum.StrEnum):
    INVALID_STATE = "invalid_state"
    UNSUPPORTED_CODEC = "unsupported_codec"
    DECODER_INIT_FAILURE = "decoder_init_failure"
    COMPOSITION_FAILURE = "composition_failure"
    DECODE_FAILURE = "decode_failure"


class PlaybackSessionError(RuntimeError):
    """Sanitized playback-session failure."""

    def __init__(
        self,
        code: PlaybackSessionErrorCode,
        message: str,
        *,
        decode_error_code: PlaybackDecodeErrorCode | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.decode_error_code = decode_error_code


class PlaybackSessionSnapshot(BaseModel):
    """Source-free playback-session observability."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: typing.Literal["1"] = "1"
    state: PlaybackSessionState
    decoder_initialized: bool = False
    decoded_frames: int = Field(ge=0)
    decoded_bytes: int = Field(ge=0)
    source_span_ms: int = Field(ge=0)
    delivered_packets: int = Field(ge=0)
    descriptor_verified: bool = False


class BoundedPlaybackSession:
    """Execute one bounded H.264 playback session with the accepted decoder."""

    def __init__(
        self,
        path: pathlib.Path,
        descriptor: RecordingStreamDescriptor,
        start_ms: int,
        end_ms: int,
        rate: PlaybackRate = PlaybackRate.NORMAL,
        *,
        runtime_root: pathlib.Path | None = None,
        decoder_factory: DecoderFactory | None = None,
        max_packets: int = 1_000_000,
        max_frames: int = 1_000_000,
        max_frames_per_packet: int = 8,
        max_frame_bytes: int = 32 * 1024 * 1024,
        decoder_timeout_seconds: float = 0.5,
        frame_consumer_timeout_seconds: float = 0.5,
        pump_consumer_timeout_seconds: float = 8.0,
        cleanup_timeout_seconds: float = 1.0,
    ) -> None:
        if descriptor.codec != VideoCodec.H264:
            raise PlaybackSessionError(
                PlaybackSessionErrorCode.UNSUPPORTED_CODEC,
                "playback codec is not supported by the accepted decoder",
            )
        if decoder_factory is not None and not callable(decoder_factory):
            raise TypeError("decoder_factory must be callable")

        self._path = pathlib.Path(path).expanduser().resolve(strict=False)
        self._descriptor = descriptor
        self._start_ms = start_ms
        self._end_ms = end_ms
        self._rate = rate
        self._runtime_root = runtime_root
        self._decoder_factory = decoder_factory
        self._max_packets = max_packets
        self._max_frames = max_frames
        self._max_frames_per_packet = max_frames_per_packet
        self._max_frame_bytes = max_frame_bytes
        self._decoder_timeout_seconds = decoder_timeout_seconds
        self._frame_consumer_timeout_seconds = frame_consumer_timeout_seconds
        self._pump_consumer_timeout_seconds = pump_consumer_timeout_seconds
        self._cleanup_timeout_seconds = cleanup_timeout_seconds
        self._state = PlaybackSessionState.CREATED
        self._decoder_initialized = False
        self._decoded_frames = 0
        self._decoded_bytes = 0
        self._source_span_ms = 0
        self._delivered_packets = 0
        self._descriptor_verified = False

    @property
    def snapshot(self) -> PlaybackSessionSnapshot:
        return PlaybackSessionSnapshot(
            state=self._state,
            decoder_initialized=self._decoder_initialized,
            decoded_frames=self._decoded_frames,
            decoded_bytes=self._decoded_bytes,
            source_span_ms=self._source_span_ms,
            delivered_packets=self._delivered_packets,
            descriptor_verified=self._descriptor_verified,
        )

    def _new_decoder(self) -> PlaybackDecoder:
        try:
            if self._decoder_factory is not None:
                decoder = self._decoder_factory(self._descriptor.payload_type)
            else:
                decoder = GStreamerNativePlaybackDecoder(
                    self._descriptor.payload_type,
                    runtime_root=self._runtime_root,
                    max_frame_bytes=self._max_frame_bytes,
                    max_frames_per_push=self._max_frames_per_packet,
                    operation_timeout_seconds=self._decoder_timeout_seconds,
                )
        except NativePlaybackDecoderError:
            raise PlaybackSessionError(
                PlaybackSessionErrorCode.DECODER_INIT_FAILURE,
                "playback decoder could not be initialized",
            ) from None
        except Exception:
            raise PlaybackSessionError(
                PlaybackSessionErrorCode.DECODER_INIT_FAILURE,
                "playback decoder could not be initialized",
            ) from None

        if not all(callable(getattr(decoder, name, None)) for name in ("decode", "flush", "close")):
            raise PlaybackSessionError(
                PlaybackSessionErrorCode.DECODER_INIT_FAILURE,
                "playback decoder does not satisfy the required boundary",
            )
        self._decoder_initialized = True
        return decoder

    async def _close_unbound_decoder(self, decoder: PlaybackDecoder) -> None:
        try:
            await asyncio.wait_for(decoder.close(), timeout=self._cleanup_timeout_seconds)
        except BaseException:
            return

    async def run(self, consumer: FrameConsumer) -> PlaybackSessionSnapshot:
        """Run the accepted recording-to-frame path exactly once."""
        if self._state != PlaybackSessionState.CREATED:
            raise PlaybackSessionError(
                PlaybackSessionErrorCode.INVALID_STATE,
                "playback session cannot be reused",
            )
        if not callable(consumer):
            raise TypeError("consumer must be callable")

        self._state = PlaybackSessionState.RUNNING
        decoder: PlaybackDecoder | None = None
        try:
            decoder = self._new_decoder()
            try:
                bridge = BoundedPlaybackDecodeBridge(
                    self._path,
                    self._descriptor,
                    self._start_ms,
                    self._end_ms,
                    decoder,
                    self._rate,
                    max_packets=self._max_packets,
                    max_frames=self._max_frames,
                    max_frames_per_packet=self._max_frames_per_packet,
                    max_frame_bytes=self._max_frame_bytes,
                    decoder_timeout_seconds=self._decoder_timeout_seconds,
                    frame_consumer_timeout_seconds=self._frame_consumer_timeout_seconds,
                    pump_consumer_timeout_seconds=self._pump_consumer_timeout_seconds,
                    cleanup_timeout_seconds=self._cleanup_timeout_seconds,
                )
            except Exception:
                await self._close_unbound_decoder(decoder)
                raise PlaybackSessionError(
                    PlaybackSessionErrorCode.COMPOSITION_FAILURE,
                    "playback session could not compose the accepted boundaries",
                ) from None

            result = await bridge.run(consumer)
            self._decoded_frames = result.decoded_frames
            self._decoded_bytes = result.decoded_bytes
            self._source_span_ms = result.source_span_ms
            self._delivered_packets = result.pump_delivered_packets
            self._descriptor_verified = result.descriptor_verified
            self._state = PlaybackSessionState.COMPLETE
            return self.snapshot
        except asyncio.CancelledError:
            self._state = PlaybackSessionState.CANCELLED
            raise
        except PlaybackSessionError:
            self._state = PlaybackSessionState.FAILED
            raise
        except PlaybackDecodeError as exc:
            self._state = PlaybackSessionState.FAILED
            raise PlaybackSessionError(
                PlaybackSessionErrorCode.DECODE_FAILURE,
                "playback session failed inside the accepted decode boundary",
                decode_error_code=exc.code,
            ) from None
