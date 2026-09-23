"""Bounded optional analytics overlay wrapper for transient live presentation frames.

The wrapper never owns source resolution, credentials, recording, or presentation
lifecycle. It gives an optional analytics provider only the already-decoded transient
``PresentationVideoFrame`` and fails open to the original live frame when analytics
is unavailable, slow, malformed, or over capacity. Retained state is aggregate only.
"""

from __future__ import annotations

import asyncio
import typing
from collections.abc import Awaitable, Callable, Iterable
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from k5vision.media.analytics_detection_adapter import adapt_analytics_tracked_detections
from k5vision.media.detection_overlay import BoundedDetectionOverlayRenderer
from k5vision.media.live_presentation import LivePresentationSnapshot
from k5vision.media.presentation_frame import PresentationVideoFrame

_MAX_FRAMES = 1_000_000
_MAX_OBSERVATIONS = 512
_MAX_RENDERED_BOXES = _MAX_FRAMES * _MAX_OBSERVATIONS


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

    schema_version: typing.Literal["1"] = "1"
    processed_frames: int = Field(ge=0, le=_MAX_FRAMES)
    overlay_frames: int = Field(ge=0, le=_MAX_FRAMES)
    passthrough_frames: int = Field(ge=0, le=_MAX_FRAMES)
    analytics_failures: int = Field(ge=0, le=_MAX_FRAMES)
    capacity_bypasses: int = Field(ge=0, le=_MAX_FRAMES)
    rendered_boxes: int = Field(ge=0, le=_MAX_RENDERED_BOXES)


class BoundedAnalyticsOverlayDelivery:
    """Wrap one live-presentation runner with bounded, fail-open analytics overlays."""

    def __init__(
        self,
        runner: LivePresentationRunner,
        provider: AnalyticsObservationProvider,
        *,
        max_observations: int = 128,
        max_frames: int = 100_000,
        provider_timeout_seconds: float = 0.25,
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

        self._runner = runner
        self._provider = provider
        self._max_observations = max_observations
        self._max_frames = max_frames
        self._provider_timeout_seconds = provider_timeout_seconds
        self._renderer = BoundedDetectionOverlayRenderer(
            max_boxes=max_observations,
            border_width=border_width,
            minimum_confidence=minimum_confidence,
        )
        self._used = False
        self._processed_frames = 0
        self._overlay_frames = 0
        self._passthrough_frames = 0
        self._analytics_failures = 0
        self._capacity_bypasses = 0
        self._rendered_boxes = 0

    @property
    def snapshot(self) -> AnalyticsOverlayDeliverySnapshot:
        return AnalyticsOverlayDeliverySnapshot(
            processed_frames=self._processed_frames,
            overlay_frames=self._overlay_frames,
            passthrough_frames=self._passthrough_frames,
            analytics_failures=self._analytics_failures,
            capacity_bypasses=self._capacity_bypasses,
            rendered_boxes=self._rendered_boxes,
        )

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

            if self._processed_frames >= self._max_frames:
                self._capacity_bypasses += 1
                self._passthrough_frames += 1
                await consumer(frame)
                return

            self._processed_frames += 1
            output = frame
            rendered_boxes = 0
            try:
                values = await asyncio.wait_for(
                    self._provider(frame),
                    timeout=self._provider_timeout_seconds,
                )
                observations = adapt_analytics_tracked_detections(
                    values,
                    max_observations=self._max_observations,
                )
                rendered = self._renderer.render(frame, observations)
                output = rendered.frame
                rendered_boxes = rendered.snapshot.rendered_boxes
            except asyncio.CancelledError:
                raise
            except Exception:
                self._analytics_failures += 1
                self._passthrough_frames += 1
                await consumer(frame)
                return

            self._rendered_boxes += rendered_boxes
            if rendered_boxes:
                self._overlay_frames += 1
            else:
                self._passthrough_frames += 1
            await consumer(output)

        return await self._runner.run(source_uri, overlay_consumer)
