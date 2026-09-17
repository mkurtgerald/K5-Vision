"""Deterministic bounded navigation over the accepted playback timeline."""

from __future__ import annotations

import enum
import pathlib
import typing

from pydantic import BaseModel, ConfigDict, Field

from k5vision.media.playback_timeline import (
    BoundedPlaybackTimeline,
    PlaybackTimelineError,
    PlaybackTimelineErrorCode,
    TimedPlaybackPacket,
)
from k5vision.media.recording_descriptor import RecordingStreamDescriptor

_MAX_PACKETS = 1_000_000


class PlaybackNavigationState(enum.StrEnum):
    CREATED = "created"
    SCANNING = "scanning"
    POSITIONED = "positioned"
    COMPLETE = "complete"
    ABORTED = "aborted"
    FAILED = "failed"


class PlaybackNavigationErrorCode(enum.StrEnum):
    INVALID_STATE = "invalid_state"
    TIMELINE_FAILURE = "timeline_failure"


class PlaybackNavigationError(RuntimeError):
    """Sanitized navigation-boundary failure."""

    def __init__(
        self,
        code: PlaybackNavigationErrorCode,
        message: str,
        *,
        timeline_error_code: PlaybackTimelineErrorCode | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.timeline_error_code = timeline_error_code


class PlaybackNavigationSnapshot(BaseModel):
    """Source-free navigation observability."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: typing.Literal["1"] = "1"
    state: PlaybackNavigationState
    target_ms: int = Field(ge=0)
    scanned_packets: int = Field(ge=0, le=_MAX_PACKETS)
    emitted_packets: int = Field(ge=0, le=_MAX_PACKETS)
    positioned_at_ms: int | None = Field(default=None, ge=0)
    descriptor_verified: bool = False


class BoundedPlaybackNavigator:
    """Select the first packet at or after a bounded relative target once."""

    def __init__(
        self,
        path: pathlib.Path,
        descriptor: RecordingStreamDescriptor,
        target_ms: int,
        *,
        max_packets: int = _MAX_PACKETS,
    ) -> None:
        if not isinstance(target_ms, int) or isinstance(target_ms, bool):
            raise TypeError("target_ms must be an integer")
        if not 0 <= target_ms <= descriptor.duration_ms:
            raise ValueError("target_ms is outside the recording duration")
        if not 1 <= max_packets <= _MAX_PACKETS:
            raise ValueError("max_packets must be between 1 and 1000000")
        if descriptor.packet_count > max_packets:
            raise ValueError("descriptor packet count exceeds navigation limit")

        self._timeline = BoundedPlaybackTimeline(
            path,
            descriptor,
            max_packets=max_packets,
        )
        self._target_ms = target_ms
        self._state = PlaybackNavigationState.CREATED
        self._scanned_packets = 0
        self._emitted_packets = 0
        self._positioned_at_ms: int | None = None
        self._descriptor_verified = False

    @property
    def snapshot(self) -> PlaybackNavigationSnapshot:
        return PlaybackNavigationSnapshot(
            state=self._state,
            target_ms=self._target_ms,
            scanned_packets=self._scanned_packets,
            emitted_packets=self._emitted_packets,
            positioned_at_ms=self._positioned_at_ms,
            descriptor_verified=self._descriptor_verified,
        )

    def iter_packets(self) -> typing.Iterator[TimedPlaybackPacket]:
        """Yield packets from the first packet at or after the requested target."""
        if self._state != PlaybackNavigationState.CREATED:
            raise PlaybackNavigationError(
                PlaybackNavigationErrorCode.INVALID_STATE,
                "playback navigator cannot be reused",
            )

        iterator = self._timeline.iter_packets()
        self._state = PlaybackNavigationState.SCANNING
        completed = False
        try:
            for item in iterator:
                self._scanned_packets += 1
                if self._positioned_at_ms is None:
                    if item.elapsed_ms < self._target_ms:
                        continue
                    self._positioned_at_ms = item.elapsed_ms
                    self._state = PlaybackNavigationState.POSITIONED

                self._emitted_packets += 1
                yield item

            self._descriptor_verified = self._timeline.snapshot.descriptor_verified
            self._state = PlaybackNavigationState.COMPLETE
            completed = True
        except PlaybackTimelineError as exc:
            self._state = PlaybackNavigationState.FAILED
            raise PlaybackNavigationError(
                PlaybackNavigationErrorCode.TIMELINE_FAILURE,
                "playback navigation failed",
                timeline_error_code=exc.code,
            ) from None
        finally:
            if not completed:
                iterator.close()
                if self._state in {
                    PlaybackNavigationState.SCANNING,
                    PlaybackNavigationState.POSITIONED,
                }:
                    self._state = PlaybackNavigationState.ABORTED
