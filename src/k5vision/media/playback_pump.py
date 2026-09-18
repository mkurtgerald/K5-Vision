"""Asynchronous paced playback pump over the accepted deterministic schedule.

Media payloads exist only while an accepted scheduled packet is delivered to a
project-owned consumer callback. Retained state is limited to source-free counters
and timing metadata; it contains no source details, paths, identifiers, credentials,
or media payloads.
"""

from __future__ import annotations

import asyncio
import enum
import pathlib
import time
import typing
from collections.abc import Awaitable, Callable

from pydantic import BaseModel, ConfigDict, Field

from k5vision.media.playback_schedule import (
    BoundedPlaybackSchedule,
    PlaybackRate,
    PlaybackScheduleError,
    PlaybackScheduleErrorCode,
)
from k5vision.media.recording_descriptor import RecordingStreamDescriptor

_MAX_PACKETS = 1_000_000

PlaybackConsumer = Callable[[memoryview, int], Awaitable[None]]
Clock = Callable[[], float]
Sleeper = Callable[[float], Awaitable[None]]


class PlaybackPumpState(enum.StrEnum):
    CREATED = "created"
    RUNNING = "running"
    COMPLETE = "complete"
    CANCELLED = "cancelled"
    FAILED = "failed"


class PlaybackPumpErrorCode(enum.StrEnum):
    INVALID_STATE = "invalid_state"
    SCHEDULE_FAILURE = "schedule_failure"
    CONSUMER_TIMEOUT = "consumer_timeout"
    CONSUMER_FAILURE = "consumer_failure"
    PACING_FAILURE = "pacing_failure"


class PlaybackPumpError(RuntimeError):
    """Sanitized playback-pump failure."""

    def __init__(
        self,
        code: PlaybackPumpErrorCode,
        message: str,
        *,
        schedule_error_code: PlaybackScheduleErrorCode | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.schedule_error_code = schedule_error_code


class PlaybackPumpSnapshot(BaseModel):
    """Source-free paced-delivery observability."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: typing.Literal["1"] = "1"
    state: PlaybackPumpState
    rate: PlaybackRate
    delivered_packets: int = Field(ge=0, le=_MAX_PACKETS)
    delivered_bytes: int = Field(ge=0)
    late_packets: int = Field(ge=0, le=_MAX_PACKETS)
    source_span_ms: int = Field(ge=0)
    scheduled_span_ms: int = Field(ge=0)
    descriptor_verified: bool = False


class BoundedPlaybackPump:
    """Pace one accepted playback schedule into a bounded async consumer."""

    def __init__(
        self,
        path: pathlib.Path,
        descriptor: RecordingStreamDescriptor,
        start_ms: int,
        end_ms: int,
        rate: PlaybackRate = PlaybackRate.NORMAL,
        *,
        max_packets: int = _MAX_PACKETS,
        consumer_timeout_seconds: float = 0.5,
        clock: Clock = time.monotonic,
        sleep: Sleeper = asyncio.sleep,
    ) -> None:
        if not 0 < consumer_timeout_seconds <= 10:
            raise ValueError("consumer_timeout_seconds must be between zero and 10")
        if not callable(clock):
            raise TypeError("clock must be callable")
        if not callable(sleep):
            raise TypeError("sleep must be callable")

        self._schedule = BoundedPlaybackSchedule(
            path,
            descriptor,
            start_ms,
            end_ms,
            rate,
            max_packets=max_packets,
        )
        self._rate = rate
        self._consumer_timeout_seconds = consumer_timeout_seconds
        self._clock = clock
        self._sleep = sleep
        self._state = PlaybackPumpState.CREATED
        self._delivered_packets = 0
        self._delivered_bytes = 0
        self._late_packets = 0
        self._source_span_ms = 0
        self._scheduled_span_ms = 0
        self._descriptor_verified = False

    @property
    def snapshot(self) -> PlaybackPumpSnapshot:
        return PlaybackPumpSnapshot(
            state=self._state,
            rate=self._rate,
            delivered_packets=self._delivered_packets,
            delivered_bytes=self._delivered_bytes,
            late_packets=self._late_packets,
            source_span_ms=self._source_span_ms,
            scheduled_span_ms=self._scheduled_span_ms,
            descriptor_verified=self._descriptor_verified,
        )

    async def run(self, consumer: PlaybackConsumer) -> PlaybackPumpSnapshot:
        """Deliver one paced schedule; payloads are not retained after callback return."""
        if self._state != PlaybackPumpState.CREATED:
            raise PlaybackPumpError(
                PlaybackPumpErrorCode.INVALID_STATE,
                "playback pump cannot be reused",
            )
        if not callable(consumer):
            raise TypeError("consumer must be callable")

        iterator = self._schedule.iter_packets()
        self._state = PlaybackPumpState.RUNNING
        completed = False
        try:
            try:
                started = self._clock()
            except Exception:
                self._state = PlaybackPumpState.FAILED
                raise PlaybackPumpError(
                    PlaybackPumpErrorCode.PACING_FAILURE,
                    "playback pacing clock failed",
                ) from None

            for item in iterator:
                try:
                    now = self._clock()
                    remaining = started + (item.due_ms / 1000.0) - now
                    if remaining > 0:
                        await self._sleep(remaining)
                    elif item.due_ms > 0:
                        self._late_packets += 1
                except asyncio.CancelledError:
                    self._state = PlaybackPumpState.CANCELLED
                    raise
                except Exception:
                    self._state = PlaybackPumpState.FAILED
                    raise PlaybackPumpError(
                        PlaybackPumpErrorCode.PACING_FAILURE,
                        "playback pacing failed",
                    ) from None

                try:
                    await asyncio.wait_for(
                        consumer(memoryview(item.packet), item.source_elapsed_ms),
                        timeout=self._consumer_timeout_seconds,
                    )
                except asyncio.CancelledError:
                    self._state = PlaybackPumpState.CANCELLED
                    raise
                except TimeoutError:
                    self._state = PlaybackPumpState.FAILED
                    raise PlaybackPumpError(
                        PlaybackPumpErrorCode.CONSUMER_TIMEOUT,
                        "playback consumer timed out",
                    ) from None
                except Exception:
                    self._state = PlaybackPumpState.FAILED
                    raise PlaybackPumpError(
                        PlaybackPumpErrorCode.CONSUMER_FAILURE,
                        "playback consumer failed",
                    ) from None

                self._delivered_packets += 1
                self._delivered_bytes += len(item.packet)
                self._source_span_ms = self._schedule.snapshot.source_span_ms
                self._scheduled_span_ms = item.due_ms

            self._descriptor_verified = self._schedule.snapshot.descriptor_verified
            self._state = PlaybackPumpState.COMPLETE
            completed = True
            return self.snapshot
        except PlaybackScheduleError as exc:
            self._state = PlaybackPumpState.FAILED
            raise PlaybackPumpError(
                PlaybackPumpErrorCode.SCHEDULE_FAILURE,
                "playback schedule failed",
                schedule_error_code=exc.code,
            ) from None
        finally:
            if not completed:
                iterator.close()
