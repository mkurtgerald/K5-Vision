"""Deterministic bounded playback window over accepted navigation.

The window yields only ephemeral media packets whose relative playback time falls
inside the requested inclusive interval. Observable state intentionally retains no
source details, filesystem paths, identifiers, credentials, or media payloads.
"""

from __future__ import annotations

import enum
import pathlib
import typing

from pydantic import BaseModel, ConfigDict, Field

from k5vision.media.playback_navigation import (
    BoundedPlaybackNavigator,
    PlaybackNavigationError,
    PlaybackNavigationErrorCode,
)
from k5vision.media.playback_timeline import TimedPlaybackPacket
from k5vision.media.recording_descriptor import RecordingStreamDescriptor

_MAX_PACKETS = 1_000_000


class PlaybackWindowState(enum.StrEnum):
    CREATED = "created"
    READING = "reading"
    COMPLETE = "complete"
    ABORTED = "aborted"
    FAILED = "failed"


class PlaybackWindowErrorCode(enum.StrEnum):
    INVALID_STATE = "invalid_state"
    NAVIGATION_FAILURE = "navigation_failure"


class PlaybackWindowError(RuntimeError):
    """Sanitized playback-window failure."""

    def __init__(
        self,
        code: PlaybackWindowErrorCode,
        message: str,
        *,
        navigation_error_code: PlaybackNavigationErrorCode | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.navigation_error_code = navigation_error_code


class PlaybackWindowSnapshot(BaseModel):
    """Source-free playback-window observability."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: typing.Literal["1"] = "1"
    state: PlaybackWindowState
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    scanned_packets: int = Field(ge=0, le=_MAX_PACKETS)
    emitted_packets: int = Field(ge=0, le=_MAX_PACKETS)
    first_emitted_ms: int | None = Field(default=None, ge=0)
    last_emitted_ms: int | None = Field(default=None, ge=0)
    descriptor_verified: bool = False


class BoundedPlaybackWindow:
    """Yield one inclusive relative-time window while verifying the full recording."""

    def __init__(
        self,
        path: pathlib.Path,
        descriptor: RecordingStreamDescriptor,
        start_ms: int,
        end_ms: int,
        *,
        max_packets: int = _MAX_PACKETS,
    ) -> None:
        if not isinstance(start_ms, int) or isinstance(start_ms, bool):
            raise TypeError("start_ms must be an integer")
        if not isinstance(end_ms, int) or isinstance(end_ms, bool):
            raise TypeError("end_ms must be an integer")
        if not 0 <= start_ms <= end_ms <= descriptor.duration_ms:
            raise ValueError("playback window is outside the recording duration")
        if not 1 <= max_packets <= _MAX_PACKETS:
            raise ValueError("max_packets must be between 1 and 1000000")
        if descriptor.packet_count > max_packets:
            raise ValueError("descriptor packet count exceeds playback-window limit")

        self._navigator = BoundedPlaybackNavigator(
            path,
            descriptor,
            start_ms,
            max_packets=max_packets,
        )
        self._start_ms = start_ms
        self._end_ms = end_ms
        self._state = PlaybackWindowState.CREATED
        self._scanned_packets = 0
        self._emitted_packets = 0
        self._first_emitted_ms: int | None = None
        self._last_emitted_ms: int | None = None
        self._descriptor_verified = False

    @property
    def snapshot(self) -> PlaybackWindowSnapshot:
        return PlaybackWindowSnapshot(
            state=self._state,
            start_ms=self._start_ms,
            end_ms=self._end_ms,
            scanned_packets=self._scanned_packets,
            emitted_packets=self._emitted_packets,
            first_emitted_ms=self._first_emitted_ms,
            last_emitted_ms=self._last_emitted_ms,
            descriptor_verified=self._descriptor_verified,
        )

    def iter_packets(self) -> typing.Iterator[TimedPlaybackPacket]:
        """Yield packets in the inclusive window and verify the bounded recording once."""
        if self._state != PlaybackWindowState.CREATED:
            raise PlaybackWindowError(
                PlaybackWindowErrorCode.INVALID_STATE,
                "playback window cannot be reused",
            )

        iterator = self._navigator.iter_packets()
        self._state = PlaybackWindowState.READING
        completed = False
        try:
            for item in iterator:
                self._scanned_packets += 1
                if item.elapsed_ms > self._end_ms:
                    continue

                if self._first_emitted_ms is None:
                    self._first_emitted_ms = item.elapsed_ms
                self._last_emitted_ms = item.elapsed_ms
                self._emitted_packets += 1
                yield item

            self._descriptor_verified = self._navigator.snapshot.descriptor_verified
            self._state = PlaybackWindowState.COMPLETE
            completed = True
        except PlaybackNavigationError as exc:
            self._state = PlaybackWindowState.FAILED
            raise PlaybackWindowError(
                PlaybackWindowErrorCode.NAVIGATION_FAILURE,
                "playback window read failed",
                navigation_error_code=exc.code,
            ) from None
        finally:
            if not completed:
                iterator.close()
                if self._state == PlaybackWindowState.READING:
                    self._state = PlaybackWindowState.ABORTED
