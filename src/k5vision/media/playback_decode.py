"""Project-owned decode/frame-consumer boundary over paced playback.

A concrete decoder is injected behind this contract. Encoded and decoded media exist
only while callbacks execute; retained state contains counters and bounded metadata
only, with no source details, paths, identifiers, credentials, or media payloads.
"""

from __future__ import annotations

import asyncio
import enum
import pathlib
import typing
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from k5vision.media.playback_pump import (
    BoundedPlaybackPump,
    Clock,
    PlaybackPumpError,
    PlaybackPumpErrorCode,
    Sleeper,
)
from k5vision.media.playback_schedule import PlaybackRate
from k5vision.media.recording_descriptor import RecordingStreamDescriptor

_MAX_PACKETS = 1_000_000
_MAX_FRAMES_PER_PACKET = 8
_MAX_FRAME_BYTES = 64 * 1024 * 1024
_MAX_DIMENSION = 8192
_MAX_STRIDE = 131_072


class PixelFormat(enum.StrEnum):
    RGB24 = "rgb24"
    BGR24 = "bgr24"
    RGBA = "rgba"
    BGRA = "bgra"
    NV12 = "nv12"
    I420 = "i420"


@dataclass(frozen=True, slots=True)
class DecodedVideoFrame:
    """Ephemeral decoded frame passed only through the consumer boundary."""

    pixels: memoryview
    width: int
    height: int
    stride: int
    pixel_format: PixelFormat

    def __post_init__(self) -> None:
        if not isinstance(self.pixels, memoryview):
            raise TypeError("pixels must be a memoryview")
        if not 1 <= self.pixels.nbytes <= _MAX_FRAME_BYTES:
            raise ValueError("decoded frame byte size is outside bounds")
        if not 1 <= self.width <= _MAX_DIMENSION:
            raise ValueError("decoded frame width is outside bounds")
        if not 1 <= self.height <= _MAX_DIMENSION:
            raise ValueError("decoded frame height is outside bounds")
        if not 1 <= self.stride <= _MAX_STRIDE:
            raise ValueError("decoded frame stride is outside bounds")
        if not isinstance(self.pixel_format, PixelFormat):
            raise TypeError("pixel_format must be a PixelFormat")


DecodedFrameBatch = tuple[DecodedVideoFrame, ...]
FrameConsumer = Callable[[DecodedVideoFrame, int], Awaitable[None]]


class PlaybackDecoder(Protocol):
    async def decode(
        self,
        packet: memoryview,
        source_elapsed_ms: int,
    ) -> DecodedFrameBatch: ...

    async def close(self) -> None: ...


class DecodedPlaybackState(enum.StrEnum):
    CREATED = "created"
    RUNNING = "running"
    COMPLETE = "complete"
    CANCELLED = "cancelled"
    FAILED = "failed"


class DecodedPlaybackErrorCode(enum.StrEnum):
    INVALID_STATE = "invalid_state"
    PUMP_FAILURE = "pump_failure"
    DECODER_TIMEOUT = "decoder_timeout"
    DECODER_FAILURE = "decoder_failure"
    FRAME_BATCH_INVALID = "frame_batch_invalid"
    FRAME_LIMIT = "frame_limit"
    FRAME_CONSUMER_TIMEOUT = "frame_consumer_timeout"
    FRAME_CONSUMER_FAILURE = "frame_consumer_failure"
    DECODER_CLOSE_FAILURE = "decoder_close_failure"


class DecodedPlaybackError(RuntimeError):
    """Sanitized decoded-playback failure."""

    def __init__(
        self,
        code: DecodedPlaybackErrorCode,
        message: str,
        *,
        pump_error_code: PlaybackPumpErrorCode | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.pump_error_code = pump_error_code


class DecodedPlaybackSnapshot(BaseModel):
    """Source-free decode/consumer observability."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: typing.Literal["1"] = "1"
    state: DecodedPlaybackState
    rate: PlaybackRate
    packets_decoded: int = Field(ge=0, le=_MAX_PACKETS)
    frames_delivered: int = Field(ge=0, le=_MAX_PACKETS * _MAX_FRAMES_PER_PACKET)
    frame_bytes_delivered: int = Field(ge=0)
    max_width: int = Field(ge=0, le=_MAX_DIMENSION)
    max_height: int = Field(ge=0, le=_MAX_DIMENSION)
    decoder_closed: bool = False
    descriptor_verified: bool = False


class BoundedDecodedPlayback:
    """Decode paced packets into bounded ephemeral frames and deliver them once."""

    def __init__(
        self,
        path: pathlib.Path,
        descriptor: RecordingStreamDescriptor,
        start_ms: int,
        end_ms: int,
        decoder: PlaybackDecoder,
        rate: PlaybackRate = PlaybackRate.NORMAL,
        *,
        max_packets: int = _MAX_PACKETS,
        max_frames_per_packet: int = _MAX_FRAMES_PER_PACKET,
        decoder_timeout_seconds: float = 0.5,
        frame_consumer_timeout_seconds: float = 0.5,
        decoder_close_timeout_seconds: float = 1.0,
        clock: Clock | None = None,
        sleep: Sleeper | None = None,
    ) -> None:
        if not callable(getattr(decoder, "decode", None)):
            raise TypeError("decoder.decode must be callable")
        if not callable(getattr(decoder, "close", None)):
            raise TypeError("decoder.close must be callable")
        if not 1 <= max_frames_per_packet <= _MAX_FRAMES_PER_PACKET:
            raise ValueError("max_frames_per_packet must be between 1 and 8")
        if not 0 < decoder_timeout_seconds <= 5:
            raise ValueError("decoder_timeout_seconds must be between zero and 5")
        if not 0 < frame_consumer_timeout_seconds <= 5:
            raise ValueError("frame_consumer_timeout_seconds must be between zero and 5")
        if not 0 < decoder_close_timeout_seconds <= 5:
            raise ValueError("decoder_close_timeout_seconds must be between zero and 5")

        handler_timeout = (
            decoder_timeout_seconds
            + (max_frames_per_packet * frame_consumer_timeout_seconds)
            + 0.25
        )
        if handler_timeout > 10:
            raise ValueError("combined decoder/frame consumer timeout exceeds pump bound")

        pump_kwargs: dict[str, object] = {
            "max_packets": max_packets,
            "consumer_timeout_seconds": handler_timeout,
        }
        if clock is not None:
            pump_kwargs["clock"] = clock
        if sleep is not None:
            pump_kwargs["sleep"] = sleep

        self._pump = BoundedPlaybackPump(
            path,
            descriptor,
            start_ms,
            end_ms,
            rate,
            **pump_kwargs,  # type: ignore[arg-type]
        )
        self._decoder = decoder
        self._rate = rate
        self._max_frames_per_packet = max_frames_per_packet
        self._decoder_timeout_seconds = decoder_timeout_seconds
        self._frame_consumer_timeout_seconds = frame_consumer_timeout_seconds
        self._decoder_close_timeout_seconds = decoder_close_timeout_seconds
        self._state = DecodedPlaybackState.CREATED
        self._packets_decoded = 0
        self._frames_delivered = 0
        self._frame_bytes_delivered = 0
        self._max_width = 0
        self._max_height = 0
        self._decoder_closed = False
        self._descriptor_verified = False

    @property
    def snapshot(self) -> DecodedPlaybackSnapshot:
        return DecodedPlaybackSnapshot(
            state=self._state,
            rate=self._rate,
            packets_decoded=self._packets_decoded,
            frames_delivered=self._frames_delivered,
            frame_bytes_delivered=self._frame_bytes_delivered,
            max_width=self._max_width,
            max_height=self._max_height,
            decoder_closed=self._decoder_closed,
            descriptor_verified=self._descriptor_verified,
        )

    async def run(self, consumer: FrameConsumer) -> DecodedPlaybackSnapshot:
        """Run one decode session and close the injected decoder on every exit path."""
        if self._state != DecodedPlaybackState.CREATED:
            raise DecodedPlaybackError(
                DecodedPlaybackErrorCode.INVALID_STATE,
                "decoded playback cannot be reused",
            )
        if not callable(consumer):
            raise TypeError("consumer must be callable")

        self._state = DecodedPlaybackState.RUNNING
        pending_error: DecodedPlaybackError | None = None
        primary_error: BaseException | None = None
        pump_snapshot = None

        async def decode_packet(packet: memoryview, source_elapsed_ms: int) -> None:
            nonlocal pending_error
            try:
                frames = await asyncio.wait_for(
                    self._decoder.decode(packet, source_elapsed_ms),
                    timeout=self._decoder_timeout_seconds,
                )
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                pending_error = DecodedPlaybackError(
                    DecodedPlaybackErrorCode.DECODER_TIMEOUT,
                    "playback decoder timed out",
                )
                raise pending_error from None
            except Exception:
                pending_error = DecodedPlaybackError(
                    DecodedPlaybackErrorCode.DECODER_FAILURE,
                    "playback decoder failed",
                )
                raise pending_error from None

            if not isinstance(frames, tuple) or any(
                not isinstance(frame, DecodedVideoFrame) for frame in frames
            ):
                pending_error = DecodedPlaybackError(
                    DecodedPlaybackErrorCode.FRAME_BATCH_INVALID,
                    "playback decoder returned an invalid frame batch",
                )
                raise pending_error
            if len(frames) > self._max_frames_per_packet:
                pending_error = DecodedPlaybackError(
                    DecodedPlaybackErrorCode.FRAME_LIMIT,
                    "playback decoder frame limit exceeded",
                )
                raise pending_error

            self._packets_decoded += 1
            for frame in frames:
                try:
                    await asyncio.wait_for(
                        consumer(frame, source_elapsed_ms),
                        timeout=self._frame_consumer_timeout_seconds,
                    )
                except asyncio.CancelledError:
                    raise
                except TimeoutError:
                    pending_error = DecodedPlaybackError(
                        DecodedPlaybackErrorCode.FRAME_CONSUMER_TIMEOUT,
                        "decoded-frame consumer timed out",
                    )
                    raise pending_error from None
                except Exception:
                    pending_error = DecodedPlaybackError(
                        DecodedPlaybackErrorCode.FRAME_CONSUMER_FAILURE,
                        "decoded-frame consumer failed",
                    )
                    raise pending_error from None

                self._frames_delivered += 1
                self._frame_bytes_delivered += frame.pixels.nbytes
                self._max_width = max(self._max_width, frame.width)
                self._max_height = max(self._max_height, frame.height)

        try:
            try:
                pump_snapshot = await self._pump.run(decode_packet)
            except asyncio.CancelledError as exc:
                self._state = DecodedPlaybackState.CANCELLED
                primary_error = exc
            except PlaybackPumpError as exc:
                self._state = DecodedPlaybackState.FAILED
                primary_error = pending_error or DecodedPlaybackError(
                    DecodedPlaybackErrorCode.PUMP_FAILURE,
                    "paced playback pump failed",
                    pump_error_code=exc.code,
                )
        finally:
            try:
                await asyncio.wait_for(
                    self._decoder.close(),
                    timeout=self._decoder_close_timeout_seconds,
                )
                self._decoder_closed = True
            except asyncio.CancelledError as exc:
                self._state = DecodedPlaybackState.CANCELLED
                if primary_error is None:
                    primary_error = exc
            except Exception:
                if primary_error is None:
                    self._state = DecodedPlaybackState.FAILED
                    primary_error = DecodedPlaybackError(
                        DecodedPlaybackErrorCode.DECODER_CLOSE_FAILURE,
                        "playback decoder failed to close",
                    )

        if primary_error is not None:
            raise primary_error

        if pump_snapshot is None:
            self._state = DecodedPlaybackState.FAILED
            raise DecodedPlaybackError(
                DecodedPlaybackErrorCode.PUMP_FAILURE,
                "paced playback pump did not complete",
            )

        self._descriptor_verified = pump_snapshot.descriptor_verified
        self._state = DecodedPlaybackState.COMPLETE
        return self.snapshot
