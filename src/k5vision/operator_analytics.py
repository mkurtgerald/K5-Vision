"""Bounded optional person boxes for transient, authenticated operator views.

The caller reauthorizes the human session and enrolled camera before each call.
Pixels supplied by a browser are not source-attested camera evidence: this module
produces view-only boxes and never records events, media, track IDs or credentials.
The default recording product does not instantiate this optional engineering path.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal
from uuid import UUID

from k5vision.analytics_runtime import OwnedAnalyticsProvider
from k5vision.media.analytics_detection_adapter import adapt_analytics_tracked_detections
from k5vision.media.detection_overlay import DetectionOverlayObservation
from k5vision.media.presentation_frame import PixelFormat, PresentationVideoFrame
from k5vision.operator_analytics_person import (
    OperatorPersonConfiguration,
    PersonProviderFactory,
    validate_person_runtime,
)

MAX_VIEW_WIDTH = 960
MAX_VIEW_HEIGHT = 960
MAX_VIEW_PIXELS = 640 * 640
MAX_VIEW_FRAME_BYTES = MAX_VIEW_PIXELS * 4
MAX_VIEW_BOXES = 32
_MAX_FRAME_ID = 2_147_483_647


@dataclass(frozen=True, slots=True)
class OperatorAnalyticsResult:
    """One transient view result; no producer identity or source claims."""

    status: Literal["active", "busy", "unavailable"]
    frame_id: int
    observations: tuple[DetectionOverlayObservation, ...] = ()

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "frame_id": self.frame_id,
            "boxes": [
                {
                    "category": item.category,
                    "confidence": item.confidence,
                    "x_min": item.x_min,
                    "y_min": item.y_min,
                    "x_max": item.x_max,
                    "y_max": item.y_max,
                }
                for item in self.observations
            ],
        }


@dataclass(slots=True, eq=False)
class _View:
    created: float
    last_used: float
    last_submission: float = -math.inf
    provider: OwnedAnalyticsProvider | None = None
    operation: asyncio.Task | None = None
    closed: bool = False
    dispose_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class OperatorAnalyticsBroker:
    """Keep at most four view workers with no frame queue or retained results.

    ``owner_id`` is a server-owned identifier for one authenticated login session,
    never a user-supplied token or persistent account identifier. Authentication,
    camera membership and logout revocation remain the product API's authority.
    Different sessions and cameras never share tracker state. Idle workers expire;
    cancellation cannot free the underlying native provider's capacity early.
    """

    def __init__(
        self,
        config: OperatorPersonConfiguration,
        *,
        max_views: int = 4,
        minimum_interval_seconds: float = 0.5,
        inference_timeout_seconds: float = 2.0,
        idle_seconds: float = 15.0,
    ) -> None:
        if type(max_views) is not int or not 1 <= max_views <= 4:
            raise ValueError("Analytics view capacity was refused")
        values = (minimum_interval_seconds, inference_timeout_seconds, idle_seconds)
        if any(type(value) not in (int, float) or not math.isfinite(value) for value in values):
            raise ValueError("Analytics view timing was refused")
        if not (
            0.25 <= minimum_interval_seconds <= 5.0
            and 0.05 <= inference_timeout_seconds <= 15.0
            and 1.0 <= idle_seconds <= 120.0
        ):
            raise ValueError("Analytics view timing was refused")
        # Preserve exact dependency, artifact and local-path admission before
        # native imports. No restricted Analytics Lab source is involved.
        validate_person_runtime(config)
        self._factory = PersonProviderFactory(config)
        self._max_views = max_views
        self._interval = float(minimum_interval_seconds)
        self._timeout = float(inference_timeout_seconds)
        self._idle = float(idle_seconds)
        self._clock: Callable[[], float] = time.monotonic
        self._views: dict[tuple[UUID, UUID], _View] = {}
        self._closed = False

    @staticmethod
    def _validate(owner_id: UUID, camera_id: UUID, frame_id: int, frame) -> None:
        if not isinstance(owner_id, UUID) or not isinstance(camera_id, UUID):
            raise ValueError("Analytics view identity was refused")
        if type(frame_id) is not int or not 0 <= frame_id <= _MAX_FRAME_ID:
            raise ValueError("Analytics frame identifier was refused")
        if not isinstance(frame, PresentationVideoFrame) or (
            type(frame.width) is not int
            or type(frame.height) is not int
            or type(frame.stride_bytes) is not int
            or frame.width > MAX_VIEW_WIDTH
            or frame.height > MAX_VIEW_HEIGHT
            or frame.width * frame.height > MAX_VIEW_PIXELS
            or frame.stride_bytes != frame.width * 4
            or frame.pixel_format is not PixelFormat.BGRX
            or frame.payload.itemsize != 1
            or frame.payload.ndim != 1
            or not frame.payload.c_contiguous
            or frame.payload.nbytes != frame.height * frame.stride_bytes
            or frame.payload.nbytes > MAX_VIEW_FRAME_BYTES
        ):
            raise ValueError("Analytics view frame was refused")

    async def _dispose(self, view: _View) -> None:
        async with view.dispose_lock:
            view.closed = True
            operation = view.operation
            if operation is not None and operation is not asyncio.current_task():
                operation.cancel()
                try:
                    await operation
                except (Exception, asyncio.CancelledError):
                    pass
            provider, view.provider = view.provider, None
            if provider is not None:
                try:
                    await provider.aclose()
                except Exception:
                    # The admitted factory retains capacity until the real
                    # native worker finishes; this never certifies its cleanup.
                    pass

    async def expire_idle(self) -> None:
        """Called by product housekeeping; never expires an in-flight frame."""
        now = self._clock()
        expired = []
        for key, view in tuple(self._views.items()):
            if view.operation is None and now - view.last_used >= self._idle:
                self._views.pop(key, None)
                expired.append(view)
        for view in expired:
            await self._dispose(view)

    async def infer(
        self,
        *,
        owner_id: UUID,
        camera_id: UUID,
        frame_id: int,
        frame: PresentationVideoFrame,
    ) -> OperatorAnalyticsResult:
        self._validate(owner_id, camera_id, frame_id, frame)
        if self._closed:
            return OperatorAnalyticsResult("unavailable", frame_id)
        key = owner_id, camera_id
        view = self._views.get(key)
        now = self._clock()
        if view is None:
            if len(self._views) >= self._max_views:
                return OperatorAnalyticsResult("busy", frame_id)
            view = _View(created=now, last_used=now)
            self._views[key] = view
        if view.closed:
            return OperatorAnalyticsResult("unavailable", frame_id)
        if view.operation is not None or now - view.last_submission < self._interval:
            return OperatorAnalyticsResult("busy", frame_id)
        view.last_used = view.last_submission = now

        async def run() -> OperatorAnalyticsResult:
            if view.provider is None:
                view.provider = await self._factory()
            if view.closed or self._closed:
                return OperatorAnalyticsResult("unavailable", frame_id)
            # Caller timing is a view correlation hint, never tracker authority.
            elapsed_ms = min(_MAX_FRAME_ID, max(0, int((now - view.created) * 1000)))
            transient = PresentationVideoFrame(
                memoryview(bytes(frame.payload)),
                frame.width,
                frame.height,
                frame.stride_bytes,
                PixelFormat.BGRX,
                elapsed_ms,
            )
            values = await asyncio.wait_for(view.provider(transient), timeout=self._timeout)
            observations = adapt_analytics_tracked_detections(
                values, max_observations=MAX_VIEW_BOXES
            )
            selected = tuple(
                item
                for item in observations
                if item.category == "person" and item.confidence >= 0.5
            )
            if view.closed or self._closed:
                return OperatorAnalyticsResult("unavailable", frame_id)
            return OperatorAnalyticsResult("active", frame_id, selected)

        operation = view.operation = asyncio.create_task(run())
        failed = False
        try:
            return await operation
        except asyncio.CancelledError:
            failed = True
            raise
        except Exception:
            failed = True
            return OperatorAnalyticsResult("unavailable", frame_id)
        finally:
            if view.operation is operation:
                view.operation = None
            view.last_used = self._clock()
            if failed or view.closed:
                if self._views.get(key) is view:
                    self._views.pop(key, None)
                await self._dispose(view)

    async def close_owner(self, owner_id: UUID) -> None:
        """Close every camera view for one revoked/logout human session."""
        if not isinstance(owner_id, UUID):
            raise ValueError("Analytics view identity was refused")
        selected = []
        for key, view in tuple(self._views.items()):
            if key[0] == owner_id:
                self._views.pop(key, None)
                view.closed = True
                selected.append(view)
        for view in selected:
            await self._dispose(view)

    async def aclose(self) -> None:
        self._closed = True
        selected = tuple(self._views.values())
        self._views.clear()
        for view in selected:
            view.closed = True
        for view in selected:
            await self._dispose(view)
