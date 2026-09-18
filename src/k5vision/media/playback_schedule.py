"""Deterministic playback pacing schedule over the accepted bounded window.

This boundary computes source-relative due times only. It performs no wall-clock
sleeping, decoding, rendering, network I/O, or media retention. Observable state
contains counters/timing only and remains source/path/identifier/payload free.
"""

from __future__ import annotations

import enum
import pathlib
import typing
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

from k5vision.media.playback_window import (
    BoundedPlaybackWindow,
    PlaybackWindowError,
    PlaybackWindowErrorCode,
)
from k5vision.media.recording_descriptor import RecordingStreamDescriptor

_MAX_PACKETS = 1_000_000


class PlaybackRate(enum.StrEnum):
    QUARTER = "0.25x"
    HALF = "0.5x"
    NORMAL = "1x"
    DOUBLE = "2x"
    QUADRUPLE = "4x"
    OCTUPLE = "8x"
    SIXTEEN = "16x"


_RATE_RATIO: dict[PlaybackRate, tuple[int, int]] = {
    PlaybackRate.QUARTER: (1, 4),
    PlaybackRate.HALF: (1, 2),
    PlaybackRate.NORMAL: (1, 1),
    PlaybackRate.DOUBLE: (2, 1),
    PlaybackRate.QUADRUPLE: (4, 1),
    PlaybackRate.OCTUPLE: (8, 1),
    PlaybackRate.SIXTEEN: (16, 1),
}


class PlaybackScheduleState(enum.StrEnum):
    CREATED = "created"
    SCHEDULING = "scheduling"
    COMPLETE = "complete"
    ABORTED = "aborted"
    FAILED = "failed"


class PlaybackScheduleErrorCode(enum.StrEnum):
    INVALID_STATE = "invalid_state"
    WINDOW_FAILURE = "window_failure"


class PlaybackScheduleError(RuntimeError):
    """Sanitized playback-schedule failure."""

    def __init__(
        self,
        code: PlaybackScheduleErrorCode,
        message: str,
        *,
        window_error_code: PlaybackWindowErrorCode | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.window_error_code = window_error_code


@dataclass(frozen=True, slots=True)
class ScheduledPlaybackPacket:
    """Ephemeral packet with deterministic due time relative to window start."""

    packet: bytes
    ordinal: int
    source_elapsed_ms: int
    due_ms: int


class PlaybackScheduleSnapshot(BaseModel):
    """Source-free pacing observability."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: typing.Literal["1"] = "1"
    state: PlaybackScheduleState
    rate: PlaybackRate
    packets: int = Field(ge=0, le=_MAX_PACKETS)
    source_span_ms: int = Field(ge=0)
    scheduled_span_ms: int = Field(ge=0)
    descriptor_verified: bool = False


class BoundedPlaybackSchedule:
    """Compute one bounded deterministic pacing schedule for an accepted window."""

    def __init__(
        self,
        path: pathlib.Path,
        descriptor: RecordingStreamDescriptor,
        start_ms: int,
        end_ms: int,
        rate: PlaybackRate = PlaybackRate.NORMAL,
        *,
        max_packets: int = _MAX_PACKETS,
    ) -> None:
        if not isinstance(rate, PlaybackRate):
            raise TypeError("rate must be a PlaybackRate")

        self._window = BoundedPlaybackWindow(
            path,
            descriptor,
            start_ms,
            end_ms,
            max_packets=max_packets,
        )
        self._rate = rate
        self._state = PlaybackScheduleState.CREATED
        self._packets = 0
        self._source_span_ms = 0
        self._scheduled_span_ms = 0
        self._descriptor_verified = False

    @property
    def snapshot(self) -> PlaybackScheduleSnapshot:
        return PlaybackScheduleSnapshot(
            state=self._state,
            rate=self._rate,
            packets=self._packets,
            source_span_ms=self._source_span_ms,
            scheduled_span_ms=self._scheduled_span_ms,
            descriptor_verified=self._descriptor_verified,
        )

    def iter_packets(self) -> typing.Iterator[ScheduledPlaybackPacket]:
        """Yield ephemeral packets with integer deterministic relative due times."""
        if self._state != PlaybackScheduleState.CREATED:
            raise PlaybackScheduleError(
                PlaybackScheduleErrorCode.INVALID_STATE,
                "playback schedule cannot be reused",
            )

        iterator = self._window.iter_packets()
        self._state = PlaybackScheduleState.SCHEDULING
        origin_ms: int | None = None
        numerator, denominator = _RATE_RATIO[self._rate]
        completed = False
        try:
            for item in iterator:
                if origin_ms is None:
                    origin_ms = item.elapsed_ms
                source_delta_ms = item.elapsed_ms - origin_ms
                due_ms = (source_delta_ms * denominator) // numerator

                self._packets += 1
                self._source_span_ms = source_delta_ms
                self._scheduled_span_ms = due_ms
                yield ScheduledPlaybackPacket(
                    packet=item.packet,
                    ordinal=self._packets - 1,
                    source_elapsed_ms=item.elapsed_ms,
                    due_ms=due_ms,
                )

            self._descriptor_verified = self._window.snapshot.descriptor_verified
            self._state = PlaybackScheduleState.COMPLETE
            completed = True
        except PlaybackWindowError as exc:
            self._state = PlaybackScheduleState.FAILED
            raise PlaybackScheduleError(
                PlaybackScheduleErrorCode.WINDOW_FAILURE,
                "playback schedule failed",
                window_error_code=exc.code,
            ) from None
        finally:
            if not completed:
                iterator.close()
                if self._state == PlaybackScheduleState.SCHEDULING:
                    self._state = PlaybackScheduleState.ABORTED
