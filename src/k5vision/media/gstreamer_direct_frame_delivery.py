"""Direct decoded-frame delivery for the loopback synthetic alpha lane.

The synthetic source is decoded in-process from RTSP/TCP directly into transient
BGRx presentation frames. No RTP loopback socket or recording path is involved.
"""

from __future__ import annotations

import asyncio
import ctypes
import os
import pathlib
import time
from collections.abc import Callable
from typing import Protocol

from k5vision.media.gstreamer_playback_decoder import (
    _GST_STATE_CHANGE_FAILURE,
    _GST_STATE_PLAYING,
    NativePlaybackDecoderError,
    NativePlaybackDecoderErrorCode,
)
from k5vision.media.live_presentation import (
    LivePresentationError,
    LivePresentationErrorCode,
    LivePresentationFrameConsumer,
    LivePresentationSnapshot,
    LivePresentationState,
)
from k5vision.media.native_rtsp_pipeline import quote_pipeline_value
from k5vision.media.presentation_decoder import (
    _PresentationCtypesBackend,
    _PresentationPayload,
)
from k5vision.media.presentation_frame import PixelFormat, PresentationVideoFrame


class _FramePullBackend(Protocol):
    def pull(self, timeout_ms: int) -> _PresentationPayload | None: ...

    def close(self) -> None: ...


BackendFactory = Callable[[str, int, int], _FramePullBackend]


class _DirectRtspFrameBackend(_PresentationCtypesBackend):  # pragma: no cover
    """Windows-native RTSP/TCP -> decoded BGRx appsink backend."""

    def __init__(
        self,
        source_uri: str,
        max_frame_bytes: int,
        startup_probe_ms: int,
    ) -> None:
        self._source_uri = source_uri
        self._startup_probe_ms = startup_probe_ms
        configured = os.environ.get("K5_GSTREAMER_ROOT", "").strip()
        if not configured:
            raise NativePlaybackDecoderError(
                NativePlaybackDecoderErrorCode.RUNTIME_UNAVAILABLE,
                "native decoder runtime is unavailable",
            )
        super().__init__(
            pathlib.Path(configured),
            96,
            max_frame_bytes=max_frame_bytes,
            max_frames_per_push=1,
            pull_timeout_ms=0,
        )

    def _build_pipeline(self, _payload_type: int) -> None:
        if not self._core.gst_init_check(None, None, None):
            raise NativePlaybackDecoderError(
                NativePlaybackDecoderErrorCode.NATIVE_FAILURE,
                "native decoder initialization failed",
            )
        location = quote_pipeline_value(self._source_uri)
        description = (
            f"rtspsrc location={location} protocols=tcp latency=100 "
            "tcp-timeout=5000000 teardown-timeout=0 "
            "! rtph264depay ! h264parse ! d3d11h264dec ! videoconvert "
            "! video/x-raw,format=BGRx "
            "! appsink name=k5sink sync=false max-buffers=4 drop=true"
        ).encode()
        pipeline = self._core.gst_parse_launch(description, None)
        if not pipeline:
            raise NativePlaybackDecoderError(
                NativePlaybackDecoderErrorCode.NATIVE_FAILURE,
                "direct live decoder pipeline could not be created",
            )
        self._pipeline = ctypes.c_void_p(pipeline)
        sink = self._core.gst_bin_get_by_name(self._pipeline, b"k5sink")
        if not sink:
            self.close()
            raise NativePlaybackDecoderError(
                NativePlaybackDecoderErrorCode.NATIVE_FAILURE,
                "direct live decoder sink is unavailable",
            )
        self._sink = ctypes.c_void_p(sink)
        state_result = self._core.gst_element_set_state(self._pipeline, _GST_STATE_PLAYING)
        if state_result == _GST_STATE_CHANGE_FAILURE:
            self.close()
            raise NativePlaybackDecoderError(
                NativePlaybackDecoderErrorCode.NATIVE_FAILURE,
                "direct live decoder pipeline failed to start",
            )
        current = ctypes.c_int()
        pending = ctypes.c_int()
        state_result = self._core.gst_element_get_state(
            self._pipeline,
            ctypes.byref(current),
            ctypes.byref(pending),
            max(1, self._startup_probe_ms) * 1_000_000,
        )
        if state_result == _GST_STATE_CHANGE_FAILURE:
            self.close()
            raise NativePlaybackDecoderError(
                NativePlaybackDecoderErrorCode.NATIVE_FAILURE,
                "direct live decoder pipeline failed during startup",
            )

    def pull(self, timeout_ms: int) -> _PresentationPayload | None:
        if not 1 <= timeout_ms <= 5_000:
            raise ValueError("timeout_ms must be between 1 and 5000")
        with self._native_operation_lock:
            sample = self._app.gst_app_sink_try_pull_sample(
                self._sink,
                timeout_ms * 1_000_000,
            )
            if not sample:
                return None
            return self._copy_sample(sample)


def _default_backend_factory(
    source_uri: str,
    max_frame_bytes: int,
    startup_probe_ms: int,
) -> _FramePullBackend:  # pragma: no cover
    return _DirectRtspFrameBackend(source_uri, max_frame_bytes, startup_probe_ms)


class GStreamerDirectFrameDelivery:
    """Deliver a bounded live decoded-frame sample directly from RTSP/TCP."""

    def __init__(
        self,
        *,
        frame_goal: int = 60,
        delivery_timeout_seconds: float = 15.0,
        consumer_timeout_seconds: float = 2.0,
        max_frame_bytes: int = 32 * 1024 * 1024,
        pull_poll_ms: int = 100,
        startup_probe_ms: int = 500,
        backend_factory: BackendFactory | None = None,
    ) -> None:
        if not 1 <= frame_goal <= 10_000:
            raise ValueError("frame_goal must be between 1 and 10000")
        if not 0 < delivery_timeout_seconds <= 60:
            raise ValueError("delivery_timeout_seconds must be between zero and 60")
        if not 0 < consumer_timeout_seconds <= 10:
            raise ValueError("consumer_timeout_seconds must be between zero and 10")
        if not 1 <= max_frame_bytes <= 128 * 1024 * 1024:
            raise ValueError("max_frame_bytes must be between 1 and 134217728")
        if not 1 <= pull_poll_ms <= 5_000:
            raise ValueError("pull_poll_ms must be between 1 and 5000")
        if not 1 <= startup_probe_ms <= 30_000:
            raise ValueError("startup_probe_ms must be between 1 and 30000")
        self._frame_goal = frame_goal
        self._delivery_timeout_seconds = delivery_timeout_seconds
        self._consumer_timeout_seconds = consumer_timeout_seconds
        self._max_frame_bytes = max_frame_bytes
        self._pull_poll_ms = pull_poll_ms
        self._startup_probe_ms = startup_probe_ms
        self._backend_factory = backend_factory or _default_backend_factory
        self._state = LivePresentationState.CREATED
        self._delivered_frames = 0
        self._delivered_frame_bytes = 0
        self._source_span_ms = 0

    async def run(
        self,
        source_uri: str,
        consumer: LivePresentationFrameConsumer,
    ) -> LivePresentationSnapshot:
        """Decode and present one transient bounded sample without RTP extraction."""
        if self._state is not LivePresentationState.CREATED:
            raise LivePresentationError(
                LivePresentationErrorCode.INVALID_STATE,
                "direct live delivery cannot be reused",
            )
        if not isinstance(source_uri, str) or not source_uri.strip():
            raise ValueError("source_uri must be a non-empty string")
        if not callable(consumer):
            raise TypeError("consumer must be callable")

        self._state = LivePresentationState.RUNNING
        started = time.monotonic()
        try:
            try:
                backend = await asyncio.to_thread(
                    self._backend_factory,
                    source_uri,
                    self._max_frame_bytes,
                    self._startup_probe_ms,
                )
            except asyncio.CancelledError:
                self._state = LivePresentationState.CANCELLED
                raise
            except Exception:
                self._state = LivePresentationState.FAILED
                raise LivePresentationError(
                    LivePresentationErrorCode.DECODER_INIT_FAILURE,
                    "direct live decoder could not be initialized",
                ) from None

            try:
                deadline = started + self._delivery_timeout_seconds
                while self._delivered_frames < self._frame_goal:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        self._state = LivePresentationState.FAILED
                        raise LivePresentationError(
                            LivePresentationErrorCode.DELIVERY_TIMEOUT,
                            "direct live frame delivery timed out",
                        )
                    timeout_ms = max(1, min(self._pull_poll_ms, int(remaining * 1000)))
                    try:
                        payload = await asyncio.to_thread(backend.pull, timeout_ms)
                    except asyncio.CancelledError:
                        self._state = LivePresentationState.CANCELLED
                        raise
                    except Exception:
                        self._state = LivePresentationState.FAILED
                        raise LivePresentationError(
                            LivePresentationErrorCode.DECODER_FAILURE,
                            "direct live decoder failed",
                        ) from None
                    if payload is None:
                        continue

                    elapsed_ms = max(0, int((time.monotonic() - started) * 1000))
                    frame = PresentationVideoFrame(
                        payload=memoryview(payload.payload),
                        width=payload.width,
                        height=payload.height,
                        stride_bytes=payload.stride_bytes,
                        pixel_format=PixelFormat.BGRX,
                        source_elapsed_ms=elapsed_ms,
                    )
                    try:
                        await asyncio.wait_for(
                            consumer(frame),
                            timeout=self._consumer_timeout_seconds,
                        )
                    except asyncio.CancelledError:
                        self._state = LivePresentationState.CANCELLED
                        raise
                    except TimeoutError:
                        self._state = LivePresentationState.FAILED
                        raise LivePresentationError(
                            LivePresentationErrorCode.CONSUMER_TIMEOUT,
                            "direct live frame consumer timed out",
                        ) from None
                    except Exception:
                        self._state = LivePresentationState.FAILED
                        raise LivePresentationError(
                            LivePresentationErrorCode.CONSUMER_FAILURE,
                            "direct live frame consumer failed",
                        ) from None

                    self._delivered_frames += 1
                    self._delivered_frame_bytes += len(frame.payload)
                    self._source_span_ms = max(self._source_span_ms, elapsed_ms)

                self._state = LivePresentationState.COMPLETE
                return LivePresentationSnapshot(
                    state=self._state,
                    decoder_initialized=True,
                    accepted_packets=0,
                    rtp_valid_packets=0,
                    rtp_invalid_packets=0,
                    rtp_delivered_bytes=0,
                    delivered_frames=self._delivered_frames,
                    delivered_frame_bytes=self._delivered_frame_bytes,
                    source_span_ms=self._source_span_ms,
                )
            finally:
                await asyncio.to_thread(backend.close)
        except LivePresentationError:
            raise
