"""Deterministic timing boundary over accepted bounded playback.

Media payloads are yielded only to the caller. Retained state contains counters and
timing metadata only; it never includes source details, filesystem paths, or payloads.
"""

from __future__ import annotations

import enum
import pathlib
import typing
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

from k5vision.media.playback import (
    BoundedRecordingPlayback,
    PlaybackError,
    PlaybackErrorCode,
)
from k5vision.media.recording_descriptor import RecordingStreamDescriptor

_RTP_MODULUS = 1 << 32
_RTP_HALF_RANGE = 1 << 31
_MAX_PACKETS = 1_000_000


class PlaybackTimelineState(enum.StrEnum):
    CREATED = "created"
    READING = "reading"
    COMPLETE = "complete"
    ABORTED = "aborted"
    FAILED = "failed"


class PlaybackTimelineErrorCode(enum.StrEnum):
    INVALID_STATE = "invalid_state"
    PLAYBACK_FAILURE = "playback_failure"
    TIMESTAMP_REGRESSION = "timestamp_regression"


class PlaybackTimelineError(RuntimeError):
    """Sanitized failure from the timing boundary."""

    def __init__(
        self,
        code: PlaybackTimelineErrorCode,
        message: str,
        *,
        playback_error_code: PlaybackErrorCode | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.playback_error_code = playback_error_code


@dataclass(frozen=True, slots=True)
class TimedPlaybackPacket:
    """Ephemeral media packet plus deterministic relative timing."""

    packet: bytes
    ordinal: int
    rtp_timestamp: int
    elapsed_ms: int


class PlaybackTimelineSnapshot(BaseModel):
    """Source-free timing observability."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: typing.Literal["1"] = "1"
    state: PlaybackTimelineState
    packets: int = Field(ge=0, le=_MAX_PACKETS)
    elapsed_ms: int = Field(ge=0)
    timestamp_wraps: int = Field(ge=0)
    descriptor_verified: bool = False


class BoundedPlaybackTimeline:
    """Convert accepted RTP packet timing into a bounded relative timeline once."""

    def __init__(
        self,
        path: pathlib.Path,
        descriptor: RecordingStreamDescriptor,
        *,
        max_packets: int = _MAX_PACKETS,
    ) -> None:
        if not 1 <= max_packets <= _MAX_PACKETS:
            raise ValueError("max_packets must be between 1 and 1000000")
        if descriptor.packet_count > max_packets:
            raise ValueError("descriptor packet count exceeds timeline limit")

        self._descriptor = descriptor
        self._playback = BoundedRecordingPlayback(
            path,
            descriptor,
            max_packets=max_packets,
        )
        self._state = PlaybackTimelineState.CREATED
        self._packets = 0
        self._elapsed_ms = 0
        self._timestamp_wraps = 0
        self._descriptor_verified = False

    @property
    def snapshot(self) -> PlaybackTimelineSnapshot:
        return PlaybackTimelineSnapshot(
            state=self._state,
            packets=self._packets,
            elapsed_ms=self._elapsed_ms,
            timestamp_wraps=self._timestamp_wraps,
            descriptor_verified=self._descriptor_verified,
        )

    @staticmethod
    def _timestamp(packet: bytes) -> int:
        return int.from_bytes(packet[4:8], "big")

    def iter_packets(self) -> typing.Iterator[TimedPlaybackPacket]:
        """Yield packet timing relative to the descriptor's RTP timestamp origin."""
        if self._state != PlaybackTimelineState.CREATED:
            raise PlaybackTimelineError(
                PlaybackTimelineErrorCode.INVALID_STATE,
                "playback timeline cannot be reused",
            )

        iterator = self._playback.iter_packets()
        self._state = PlaybackTimelineState.READING
        previous_timestamp: int | None = None
        elapsed_ticks = 0
        completed = False
        try:
            for packet in iterator:
                timestamp = self._timestamp(packet)
                if previous_timestamp is not None:
                    delta = (timestamp - previous_timestamp) % _RTP_MODULUS
                    if delta >= _RTP_HALF_RANGE:
                        raise PlaybackTimelineError(
                            PlaybackTimelineErrorCode.TIMESTAMP_REGRESSION,
                            "playback RTP timestamp regressed",
                        )
                    if timestamp < previous_timestamp and delta > 0:
                        self._timestamp_wraps += 1
                    elapsed_ticks += delta

                elapsed_ms = (elapsed_ticks * 1000) // self._descriptor.clock_rate_hz
                self._packets += 1
                self._elapsed_ms = elapsed_ms
                previous_timestamp = timestamp
                yield TimedPlaybackPacket(
                    packet=packet,
                    ordinal=self._packets - 1,
                    rtp_timestamp=timestamp,
                    elapsed_ms=elapsed_ms,
                )

            self._descriptor_verified = self._playback.snapshot.descriptor_verified
            self._state = PlaybackTimelineState.COMPLETE
            completed = True
        except PlaybackTimelineError:
            self._state = PlaybackTimelineState.FAILED
            raise
        except PlaybackError as exc:
            self._state = PlaybackTimelineState.FAILED
            raise PlaybackTimelineError(
                PlaybackTimelineErrorCode.PLAYBACK_FAILURE,
                "playback timeline read failed",
                playback_error_code=exc.code,
            ) from None
        finally:
            if not completed:
                iterator.close()
                if self._state == PlaybackTimelineState.READING:
                    self._state = PlaybackTimelineState.ABORTED
