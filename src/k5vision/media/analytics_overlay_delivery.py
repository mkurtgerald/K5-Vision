"""Bounded optional analytics overlay wrapper for transient live presentation frames.

The wrapper never owns source resolution, credentials, recording, or presentation
lifecycle. It gives an optional analytics provider only an already-decoded transient
``PresentationVideoFrame`` through a single-flight background task. Live presentation
never waits for analytics: the newest bounded, non-stale result is rendered when
available and every unavailable/slow/malformed case fails open to the original frame.
Retained state is aggregate only.
"""

from __future__ import annotations

import asyncio
import typing
from collections.abc import Awaitable, Callable, Iterable
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from k5vision.media.analytics_detection_adapter import adapt_analytics_tracked_detections
from k5vision.media.detection_overlay import (
    BoundedDetectionOverlayRenderer,
    DetectionOverlayObservation,
)
from k5vision.media.live_presentation import LivePresentationSnapshot
from k5vision.media.presentation_frame import PresentationVideoFrame

_MAX_FRAMES = 1_000_000
_MAX_OBSERVATIONS = 512
_MAX_RENDERED_BOXES = _MAX_FRAMES * _MAX_OBSERVATIONS
_MAX_STALE_MS = 5_000


class LivePresentationRunner(Protocol):
    async def run(
        self,
        source_uri: str,
        consumer: Callable[[PresentationVideoFrame], Awaitable[None]],
    ) -> LivePresentationSnapshot: ...


type AnalyticsObservationProvider = Callable[
    [PresentationVideoFrame],
    Awaitable[Iterable[Any]],
]


class AnalyticsOverlayDeliverySnapshot(BaseModel):
    """Payload/source/identity-free aggregate analytics overlay observability."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: typing.Literal["2"] = "2"
    processed_frames: int = Field(ge=0, le=_MAX_FRAMES)
    overlay_frames: int = Field(ge=0, le=_MAX_FRAMES)
    passthrough_frames: int = Field(ge=0, le=_MAX_FRAMES)
    provider_submissions: int = Field(ge=0, le=_MAX_FRAMES)
    provider_completions: int = Field(ge=0, le=_MAX_FRAMES)
    analytics_failures: int = Field(ge=0, le=_MAX_FRAMES)
    busy_bypasses: int = Field(ge=0, le=_MAX_FRAMES)
    capacity_bypasses: int = Field(ge=0, le=_MAX_FRAMES)
    stale_bypasses: int = Field(ge=0, le=_MAX_FRAMES)
    rendered_boxes: int = Field(ge=0, le=_MAX_RENDERED_BOXES)


class BoundedAnalyticsOverlayDelivery:
    """Add single-flight, fail-open analytics without blocking the live consumer."""

    def __init__(
        self,
        runner: LivePresentationRunner,
        provider: AnalyticsObservationProvider,
        *,
        max_observations: int = 128,
        max_frames: int = 100_000,
        provider_timeout_seconds: float = 0.25,
        max_stale_ms: int = 750,
        minimum_confidence: float = 0.5,
        border_width: int = 2,
    ) -> None:
        if not callable(getattr(runner, "run", None)):
            raise TypeError("runner must implement the live presentation boundary")
        if not callable(provider):
            raise TypeError("provider must be callable")
        if type(max_observations) is not int or not 1 <= max_observations <= _MAX_OBSERVATIONS:
            raise ValueError("max_observations must be between 1 and 512")
        if type(max_frames) is not int or not 1 <= max_frames <= _MAX_FRAMES:
            raise ValueError("max_frames must be between 1 and 1000000")
        if not 0 < provider_timeout_seconds <= 2:
            raise ValueError("provider_timeout_seconds must be between zero and 2")
        if type(max_stale_ms) is not int or not 1 <= max_stale_ms <= _MAX_STALE_MS:
            raise ValueError("max_stale_ms must be between 1 and 5000")

        self._runner = runner
        self._provider = provider
        self._max_observations = max_observations
        self._max_frames = max_frames
        self._provider_timeout_seconds = provider_timeout_seconds
        self._max_stale_ms = max_stale_ms
        self._renderer = BoundedDetectionOverlayRenderer(
            max_boxes=max_observations,
            border_width=border_width,
            minimum_confidence=minimum_confidence,
        )
        self._used = False
        self._processed_frames = 0
        self._overlay_frames = 0
        self._passthrough_frames = 0
        self._provider_submissions = 0
        self._provider_completions = 0
        self._analytics_failures = 0
        self._busy_bypasses = 0
        self._capacity_bypasses = 0
        self._stale_bypasses = 0
        self._rendered_boxes = 0
        self._provider_task: asyncio.Task[
            tuple[tuple[DetectionOverlayObservation, ...], int]
        ] | None = None
        self._latest_observations: tuple[DetectionOverlayObservation, ...] | None = None
        self._latest_source_elapsed_ms: int | None = None

    @property
    def snapshot(self) -> AnalyticsOverlayDeliverySnapshot:
        return AnalyticsOverlayDeliverySnapshot(
            processed_frames=self._processed_frames,
            overlay_frames=self._overlay_frames,
            passthrough_frames=self._passthrough_frames,
            provider_submissions=self._provider_submissions,
            provider_completions=self._provider_completions,
            analytics_failures=self._analytics_failures,
            busy_bypasses=self._busy_bypasses,
            capacity_bypasses=self._capacity_bypasses,
            stale_bypasses=self._stale_bypasses,
            rendered_boxes=self._rendered_boxes,
        )

    async def _collect(
        self,
        frame: PresentationVideoFrame,
    ) -> tuple[tuple[DetectionOverlayObservation, ...], int]:
        values = await asyncio.wait_for(
            self._provider(frame),
            timeout=self._provider_timeout_seconds,
        )
        observations = adapt_analytics_tracked_detections(
            values,
            max_observations=self._max_observations,
        )
        return observations, frame.source_elapsed_ms

    def _harvest_provider(self) -> None:
        task = self._provider_task
        if task is None or not task.done():
            return
        self._provider_task = None
        try:
            observations, source_elapsed_ms = task.result()
        except asyncio.CancelledError:
            self._analytics_failures += 1
        except Exception:
            self._analytics_failures += 1
        else:
            self._provider_completions += 1
            self._latest_observations = observations
            self._latest_source_elapsed_ms = source_elapsed_ms

    def _submit_provider(self, frame: PresentationVideoFrame) -> None:
        self._provider_submissions += 1
        self._provider_task = asyncio.create_task(
            self._collect(frame),
            name="k5-optional-analytics-overlay",
        )

    def _current_observations(
        self,
        frame: PresentationVideoFrame,
    ) -> tuple[DetectionOverlayObservation, ...] | None:
        if self._latest_observations is None or self._latest_source_elapsed_ms is None:
            return None
        age_ms = frame.source_elapsed_ms - self._latest_source_elapsed_ms
        if age_ms < 0 or age_ms > self._max_stale_ms:
            self._stale_bypasses += 1
            return None
        return self._latest_observations

    async def run(
        self,
        source_uri: str,
        consumer: Callable[[PresentationVideoFrame], Awaitable[None]],
    ) -> LivePresentationSnapshot:
        """Run once, preserving the wrapped live runner's lifecycle/result contract."""
        if self._used:
            raise RuntimeError("analytics overlay delivery cannot be reused")
        if not callable(consumer):
            raise TypeError("consumer must be callable")
        self._used = True

        async def overlay_consumer(frame: PresentationVideoFrame) -> None:
            if not isinstance(frame, PresentationVideoFrame):
                await consumer(frame)
                return

            self._harvest_provider()
            self._processed_frames += 1

            if self._provider_task is not None:
                self._busy_bypasses += 1
            elif self._provider_submissions < self._max_frames:
                self._submit_provider(frame)
            else:
                self._capacity_bypasses += 1

            observations = self._current_observations(frame)
            if observations is None:
                self._passthrough_frames += 1
                await consumer(frame)
                return

            try:
                rendered = self._renderer.render(frame, observations)
            except Exception:
                self._analytics_failures += 1
                self._passthrough_frames += 1
                await consumer(frame)
                return

            rendered_boxes = rendered.snapshot.rendered_boxes
            self._rendered_boxes += rendered_boxes
            if rendered_boxes:
                self._overlay_frames += 1
            else:
                self._passthrough_frames += 1
            await consumer(rendered.frame)

        try:
            return await self._runner.run(source_uri, overlay_consumer)
        finally:
            task = self._provider_task
            if task is not None:
                if task.done():
                    self._harvest_provider()
                else:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    self._provider_task = None
