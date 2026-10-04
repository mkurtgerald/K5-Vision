"""Product-owned adapter for the pinned, separately owned Analytics-lab runtime.

Only decoded transient frames cross this adapter. It knows no source URI, device,
credential, recording path or user identity. Each live launch owns a new detector
and tracker. A cancelled native call stays single-flight and occupies its bounded
capacity until it actually ends; cancellation is never reported as native cleanup.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Protocol

from k5vision.analytics_config import AnalyticsConfiguration, validate_analytics_runtime
from k5vision.media.presentation_frame import PresentationVideoFrame

_MAX_FRAME_BYTES = 16 * 1024 * 1024
_START_TIMEOUT_SECONDS = 15.0
_CLOSE_TIMEOUT_SECONDS = 2.0
_MAX_NATIVE_SESSIONS = 4


def _observe_result(future: asyncio.Future[Any]) -> None:
    if not future.cancelled():
        # A cancelled waiter must not log a later native exception containing
        # local model details through asyncio's unhandled-future diagnostics.
        future.exception()


def _protected_future(future: Future[Any]) -> asyncio.Future[Any]:
    wrapped = asyncio.wrap_future(future)
    wrapped.add_done_callback(_observe_result)
    return asyncio.shield(wrapped)


class OwnedAnalyticsProvider(Protocol):
    async def __call__(self, frame: PresentationVideoFrame) -> tuple[object, ...]: ...

    async def aclose(self) -> None: ...


class _DetectorTracker:
    """Preserve the qualified detector/tracker math behind the K5 frame boundary."""

    def __init__(self, config: AnalyticsConfiguration) -> None:
        # Recheck after authentication and before constructing native inference.
        validate_analytics_runtime(config)
        import numpy as np
        from analytics_lab.iou_tracker import SimpleIoUAssociationBackend
        from analytics_lab.openvino_omz import OpenVINOOMZConfig, OpenVINOOMZPoseBackend
        from analytics_lab.tracking import DetectionCandidate, NormalizedBox, TrackingSession

        self._numpy = np
        self._candidate = DetectionCandidate
        self._box = NormalizedBox
        self._detector = OpenVINOOMZPoseBackend(
            config.artifact_root, config=OpenVINOOMZConfig(max_people=4, device="CPU")
        )
        self._tracker = TrackingSession(SimpleIoUAssociationBackend())
        self._index = 0
        self._timestamp = -1

    def infer(
        self, payload: bytes, width: int, height: int, stride: int, elapsed_ms: int
    ) -> tuple[object, ...]:
        np = self._numpy
        raw = np.frombuffer(payload, dtype=np.uint8, count=height * stride)
        bgrx = raw.reshape(height, stride)[:, : width * 4].reshape(height, width, 4)
        bgr = np.ascontiguousarray(bgrx[:, :, :3])
        index = self._index
        self._index += 1
        # Live source elapsed time may repeat at sub-millisecond resolution.
        # Keep association monotonic without claiming wall-clock or identity.
        timestamp = max(self._timestamp + 1, elapsed_ms)
        self._timestamp = timestamp
        poses = self._detector(bgr, index, timestamp)
        candidates = []
        for pose in poses:
            box = pose.bbox
            left = min(1.0, max(0.0, float(box.x1) / width))
            top = min(1.0, max(0.0, float(box.y1) / height))
            right = min(1.0, max(0.0, float(box.x2) / width))
            bottom = min(1.0, max(0.0, float(box.y2) / height))
            if right <= left or bottom <= top:
                continue
            candidates.append(
                self._candidate(
                    category="person",
                    confidence=pose.confidence,
                    box=self._box(left, top, right, bottom),
                    model_class_id=1,
                )
            )
        return tuple(self._tracker.update(index, timestamp, tuple(candidates)))


class _AnalyticsSession:
    def __init__(self, config: AnalyticsConfiguration, release: Callable[[], None]) -> None:
        self._config = config
        self._release = release
        self._released = False
        self._release_lock = threading.Lock()
        self._closed = False
        self._interrupted = False
        self._backend: _DetectorTracker | None = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="k5-analytics")
        self._pending: Future[Any] = self._executor.submit(self._initialize)

    def _initialize(self) -> bool:
        try:
            self._backend = _DetectorTracker(self._config)
            return True
        except BaseException:
            # Native exception tracebacks can retain partially initialized
            # inference state. Store only a scalar result in the worker Future.
            return False

    async def start(self) -> None:
        ready = await asyncio.wait_for(_protected_future(self._pending), _START_TIMEOUT_SECONDS)
        if ready is not True:
            raise RuntimeError("Analytics session initialization failed.")

    @staticmethod
    def _infer(
        backend: _DetectorTracker,
        payload: bytes,
        width: int,
        height: int,
        stride: int,
        elapsed_ms: int,
    ) -> tuple[object, ...] | None:
        try:
            return backend.infer(payload, width, height, stride, elapsed_ms)
        except BaseException:
            # Do not let a Future keep an exception traceback and its decoded
            # image/payload locals alive after the native call has finished.
            return None

    async def __call__(self, frame: PresentationVideoFrame) -> tuple[object, ...]:
        if self._closed or self._interrupted or not self._pending.done():
            raise RuntimeError("Analytics session is unavailable.")
        if not isinstance(frame, PresentationVideoFrame) or (
            frame.payload.itemsize != 1
            or frame.payload.ndim != 1
            or not frame.payload.c_contiguous
            or frame.payload.nbytes != frame.height * frame.stride_bytes
            or frame.payload.nbytes > _MAX_FRAME_BYTES
        ):
            raise ValueError("Analytics frame exceeds the supported boundary.")
        backend = self._backend
        if backend is None:
            raise RuntimeError("Analytics session is unavailable.")
        # Own exactly one bounded transient copy; inference never retains the
        # renderer's mutable buffer and never submits another call after timeout.
        self._pending = self._executor.submit(
            self._infer,
            backend,
            bytes(frame.payload),
            frame.width,
            frame.height,
            frame.stride_bytes,
            frame.source_elapsed_ms,
        )
        try:
            result = await _protected_future(self._pending)
            if result is None:
                raise RuntimeError("Analytics inference failed.")
            return result
        except asyncio.CancelledError:
            self._interrupted = True
            raise
        except Exception:
            raise RuntimeError("Analytics inference failed.") from None

    def _release_completed(self, _future: Future[Any]) -> None:
        with self._release_lock:
            if not self._released:
                self._released = True
                self._backend = None
                self._pending = Future()
                self._pending.set_result(None)
                self._release()

    async def aclose(self) -> None:
        self._closed = True
        try:
            await asyncio.wait_for(_protected_future(self._pending), _CLOSE_TIMEOUT_SECONDS)
        except TimeoutError:
            raise RuntimeError("Analytics native cleanup is incomplete.") from None
        except Exception:
            # Failed inference is already reported through aggregate analytics;
            # a completed failed worker still permits deterministic cleanup.
            pass
        finally:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._pending.add_done_callback(self._release_completed)


class AnalyticsProviderFactory:
    """Bound native workers across cameras, cancellation and later relaunches."""

    def __init__(self, config: AnalyticsConfiguration) -> None:
        self._config = config
        self._capacity = threading.BoundedSemaphore(_MAX_NATIVE_SESSIONS)

    async def __call__(self) -> OwnedAnalyticsProvider:
        if not self._capacity.acquire(blocking=False):
            raise RuntimeError("Analytics native capacity is unavailable.")
        try:
            session = _AnalyticsSession(self._config, self._capacity.release)
        except Exception:
            self._capacity.release()
            raise RuntimeError("Analytics session initialization failed.") from None
        try:
            await session.start()
            return session
        except BaseException:
            await session.aclose()
            raise
