"""One authenticated, transient analyzed sample, independent of live HLS.

No native operation is cancelled to satisfy an HTTP lifetime. Ambiguous native
cleanup permanently closes this coordinator's admission until product restart.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from k5vision.analytics_config import load_analytics_configuration
from k5vision.analytics_runtime import AnalyticsProviderFactory
from k5vision.media.analytics_detection_adapter import adapt_analytics_tracked_detections
from k5vision.media.detection_overlay import BoundedDetectionOverlayRenderer
from k5vision.media.gstreamer_direct_frame_delivery import GStreamerDirectFrameDelivery
from k5vision.media.presentation_frame import PixelFormat, PresentationVideoFrame

MAX_FRAME_BYTES = 16 * 1024 * 1024
SAMPLE_TTL_SECONDS = 60


class SampleError(RuntimeError):
    """Fixed, source-free public failure."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class SampleOwner:
    account: str
    session: str
    camera: str
    generation: int


def session_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def configured_sample_coordinator() -> AnalyzedSampleCoordinator:
    """Read-only prerequisite check; never installs, loads DLLs or downloads."""
    try:
        config = load_analytics_configuration(os.environ)
        root = Path(os.environ.get("K5_GSTREAMER_ROOT", ""))
        if config is None or os.name != "nt" or not os.environ.get("K5_GSTREAMER_ROOT"):
            return AnalyzedSampleCoordinator(None)
        if not all(
            any((root / "bin" / name).is_file() for name in alternatives)
            for alternatives in (
                ("gstreamer-1.0-0.dll", "libgstreamer-1.0-0.dll"),
                ("gstapp-1.0-0.dll", "libgstapp-1.0-0.dll"),
            )
        ):
            return AnalyzedSampleCoordinator(None)
        return AnalyzedSampleCoordinator(AnalyticsProviderFactory(config))
    except Exception:
        return AnalyzedSampleCoordinator(None)


class AnalyzedSampleCoordinator:
    """One operation and one expiring RAM result across every account/camera."""

    def __init__(self, provider_factory, *, runner_factory=None, clock=time.monotonic):
        self._provider_factory = provider_factory
        self._runner_factory = runner_factory or (
            lambda: GStreamerDirectFrameDelivery(frame_goal=1, max_frame_bytes=MAX_FRAME_BYTES)
        )
        self._clock = clock
        self._task = None
        self._owner = None
        self._ticket = None
        self._result = None
        self._expires = 0.0
        self._expiry_handle = None
        self._invalid = False
        self._failed = False
        self._consumed = False
        self._latched = False
        self._closing = False

    def status(self) -> str:
        if self._provider_factory is None or self._latched or self._closing:
            return "unavailable"
        if self._task is not None and not self._task.done():
            return "busy"
        return "sample_available"

    def invalidate(self) -> None:
        self._invalid = True
        self._result = None

    def invalidate_session(self, digest: str) -> None:
        if self._owner is not None and self._owner.session == digest:
            self.invalidate()

    def _expire(self) -> None:
        self._result = None
        self._invalid = True
        self._expiry_handle = None

    def admit(self, owner: SampleOwner, source_uri: str, *, still_current=None) -> dict:
        status = self.status()
        if status != "sample_available":
            raise SampleError(409 if status == "busy" else 503, "Analyzed sample unavailable")
        if self._expiry_handle is not None:
            self._expiry_handle.cancel()
        self._owner = owner
        self._ticket = secrets.token_urlsafe(24)
        self._expires = self._clock() + SAMPLE_TTL_SECONDS
        self._result = None
        self._invalid = self._failed = self._consumed = False
        self._expiry_handle = asyncio.get_running_loop().call_later(
            SAMPLE_TTL_SECONDS, self._expire
        )
        self._task = asyncio.create_task(
            self._run(source_uri, still_current), name="operator-analyzed-sample"
        )
        return {
            "ticket": self._ticket,
            "status": "pending",
            "expires_in_seconds": SAMPLE_TTL_SECONDS,
        }

    def poll(self, ticket: str, owner: SampleOwner) -> dict:
        if self._ticket != ticket or self._owner != owner:
            raise SampleError(404, "Analyzed sample not found")
        remaining = self._expires - self._clock()
        if remaining <= 0:
            self._expire()
            raise SampleError(410, "Analyzed sample expired")
        if self._invalid:
            raise SampleError(404, "Analyzed sample not found")
        result = {"status": "pending", "expires_in_seconds": max(0, int(remaining))}
        if self._result is not None:
            result.update(self._result)
            self._result = None
            self._consumed = True
        elif self._consumed:
            result["status"] = "consumed"
        elif self._failed:
            result["status"] = "failed"
        return result

    async def _run(self, source_uri: str, still_current=None) -> None:
        frame = None
        provider = None
        capture_complete = False
        factory_started = False
        provider_acquired = False
        cleanup_complete = False
        cleanup_attempted = False
        received_at = None

        async def wanted():
            if self._invalid or self._clock() >= self._expires:
                return False
            if still_current is not None and not await still_current():
                self.invalidate()
                return False
            return True

        try:
            if not await wanted():
                capture_complete = True  # No native construction was attempted.
                return

            async def capture(value):
                nonlocal frame, received_at
                if frame is not None or not isinstance(value, PresentationVideoFrame):
                    raise ValueError("Invalid sample frame")
                if (
                    value.pixel_format is not PixelFormat.BGRX
                    or value.payload.ndim != 1
                    or value.payload.itemsize != 1
                    or not value.payload.c_contiguous
                    or value.payload.nbytes != value.height * value.stride_bytes
                    or value.payload.nbytes > MAX_FRAME_BYTES
                ):
                    raise ValueError("Invalid sample frame")
                # Immutable storage ensures provider and renderer see the same image.
                frame = replace(value, payload=memoryview(bytes(value.payload)))
                received_at = datetime.now(UTC).isoformat()

            # This task is never cancelled by HTTP requests, expiry or invalidation.
            await self._runner_factory().run(source_uri, capture)
            capture_complete = True
            source_uri = ""
            if frame is None:
                raise ValueError("Sample frame unavailable")
            if not await wanted():
                return
            factory_started = True
            provider = await self._provider_factory()
            provider_acquired = True
            if not await wanted():
                return
            values = await provider(frame)
            observations = adapt_analytics_tracked_detections(values, max_observations=128)
            rendered = BoundedDetectionOverlayRenderer(
                max_boxes=128, minimum_confidence=0.5
            ).render(frame, observations)
            # Do not publish pixels until native inference teardown has completed.
            cleanup_attempted = True
            await provider.aclose()
            cleanup_complete = True
            if await wanted():
                self._result = {
                    "status": "ready",
                    "width": rendered.frame.width,
                    "height": rendered.frame.height,
                    "stride_bytes": rendered.frame.stride_bytes,
                    "pixel_format": "BGRx",
                    "pixels": bytes(rendered.frame.payload),
                    "rendered_boxes": rendered.snapshot.rendered_boxes,
                    "received_at": received_at,
                }
        except BaseException:
            # Never retain a native traceback (which can own pixels or secrets).
            self._failed = True
        finally:
            if provider is not None and not cleanup_attempted:
                try:
                    await provider.aclose()
                    cleanup_complete = True
                except BaseException:
                    pass
            if (
                not capture_complete
                or (factory_started and not provider_acquired)
                or (provider_acquired and not cleanup_complete)
            ):
                self._latched = True
                self._failed = True
                self._result = None
            frame = provider = None

    async def aclose(self) -> None:
        self._closing = True
        self.invalidate()
        if self._expiry_handle is not None:
            self._expiry_handle.cancel()
            self._expiry_handle = None
        cancelled = False
        if self._task is not None:
            # A cancelled shutdown waiter still owns the product lease. Drain the
            # underlying task, including through repeated cancellation requests.
            while not self._task.done():
                try:
                    await asyncio.shield(self._task)
                except asyncio.CancelledError:
                    cancelled = True
            self._task.result()
        if self._latched:
            raise SampleError(503, "Analyzed sample cleanup incomplete")
        if cancelled:
            raise asyncio.CancelledError
