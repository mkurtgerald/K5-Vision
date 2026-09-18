"""Concrete presentation-ready decoder over the accepted GStreamer backend.

The native pipeline and runtime are inherited from the accepted Stage-20 decoder.
This adapter adds sample-caps extraction using exports already qualified by the
Stage-19 ABI, then emits the project-owned presentation-frame contract.
"""

from __future__ import annotations

import ctypes
import os
import pathlib
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from k5vision.media.gstreamer_playback_decoder import (
    GStreamerNativePlaybackDecoder,
    NativePlaybackDecoderError,
    NativePlaybackDecoderErrorCode,
    NativePlaybackDecoderState,
    _CtypesGStreamerBackend,
    _GstMapInfo,
)
from k5vision.media.presentation_frame import (
    PixelFormat,
    PresentationFrameError,
    PresentationVideoFrame,
)

_GST_MAP_READ = 1


@dataclass(frozen=True, slots=True)
class _PresentationPayload:
    payload: bytes
    width: int
    height: int
    stride_bytes: int


class _PresentationBackend(Protocol):
    def push(self, packet: bytes) -> Sequence[_PresentationPayload]: ...

    def end_of_stream(self) -> Sequence[_PresentationPayload]: ...

    def close(self) -> None: ...


class _PresentationCtypesBackend(_CtypesGStreamerBackend):
    """Accepted decoder pipeline with bounded sample geometry extraction."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        # Backend calls run in worker threads. A timed-out asyncio waiter cannot
        # cancel an already-running native call, so cleanup must never unref the
        # pipeline/appsink until that call has actually left the native surface.
        self._native_operation_lock = threading.RLock()
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]

    def _bind_signatures(self) -> None:
        super()._bind_signatures()
        self._core.gst_sample_get_caps.argtypes = [ctypes.c_void_p]
        self._core.gst_sample_get_caps.restype = ctypes.c_void_p
        self._core.gst_caps_get_structure.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        self._core.gst_caps_get_structure.restype = ctypes.c_void_p
        self._core.gst_structure_get_int.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.POINTER(ctypes.c_int),
        ]
        self._core.gst_structure_get_int.restype = ctypes.c_int

    def _copy_sample(self, sample: int) -> _PresentationPayload:
        try:
            caps = self._core.gst_sample_get_caps(sample)
            if not caps:
                raise NativePlaybackDecoderError(
                    NativePlaybackDecoderErrorCode.NATIVE_FAILURE,
                    "native decoder produced frame metadata without caps",
                )
            structure = self._core.gst_caps_get_structure(caps, 0)
            if not structure:
                raise NativePlaybackDecoderError(
                    NativePlaybackDecoderErrorCode.NATIVE_FAILURE,
                    "native decoder produced invalid frame metadata",
                )
            width = ctypes.c_int()
            height = ctypes.c_int()
            if not self._core.gst_structure_get_int(structure, b"width", ctypes.byref(width)):
                raise NativePlaybackDecoderError(
                    NativePlaybackDecoderErrorCode.NATIVE_FAILURE,
                    "native decoder frame width is unavailable",
                )
            if not self._core.gst_structure_get_int(structure, b"height", ctypes.byref(height)):
                raise NativePlaybackDecoderError(
                    NativePlaybackDecoderErrorCode.NATIVE_FAILURE,
                    "native decoder frame height is unavailable",
                )
            if width.value < 1 or height.value < 1:
                raise NativePlaybackDecoderError(
                    NativePlaybackDecoderErrorCode.NATIVE_FAILURE,
                    "native decoder frame geometry is invalid",
                )

            buffer_ptr = self._core.gst_sample_get_buffer(sample)
            if not buffer_ptr:
                raise NativePlaybackDecoderError(
                    NativePlaybackDecoderErrorCode.NATIVE_FAILURE,
                    "native decoder produced an invalid frame",
                )
            size = int(self._core.gst_buffer_get_size(buffer_ptr))
            if size < 1 or size > self._max_frame_bytes:
                raise NativePlaybackDecoderError(
                    NativePlaybackDecoderErrorCode.FRAME_TOO_LARGE,
                    "native decoder frame violated the byte bound",
                )
            if size % height.value != 0:
                raise NativePlaybackDecoderError(
                    NativePlaybackDecoderErrorCode.NATIVE_FAILURE,
                    "native decoder frame stride is invalid",
                )
            stride_bytes = size // height.value
            if stride_bytes < width.value * 4:
                raise NativePlaybackDecoderError(
                    NativePlaybackDecoderErrorCode.NATIVE_FAILURE,
                    "native decoder frame stride is invalid",
                )

            info = _GstMapInfo()
            if not self._core.gst_buffer_map(buffer_ptr, ctypes.byref(info), _GST_MAP_READ):
                raise NativePlaybackDecoderError(
                    NativePlaybackDecoderErrorCode.NATIVE_FAILURE,
                    "native decoder frame mapping failed",
                )
            try:
                if not info.data or int(info.size) != size:
                    raise NativePlaybackDecoderError(
                        NativePlaybackDecoderErrorCode.NATIVE_FAILURE,
                        "native decoder frame mapping failed",
                    )
                payload = ctypes.string_at(info.data, size)
            finally:
                self._core.gst_buffer_unmap(buffer_ptr, ctypes.byref(info))

            return _PresentationPayload(
                payload=payload,
                width=width.value,
                height=height.value,
                stride_bytes=stride_bytes,
            )
        finally:
            self._core.gst_mini_object_unref(sample)

    def push(self, packet: bytes) -> Sequence[_PresentationPayload]:
        with self._native_operation_lock:
            return super().push(packet)  # type: ignore[return-value]

    def end_of_stream(self) -> Sequence[_PresentationPayload]:
        with self._native_operation_lock:
            return super().end_of_stream()  # type: ignore[return-value]

    def close(self) -> None:
        with self._native_operation_lock:
            super().close()


class GStreamerPresentationDecoder(GStreamerNativePlaybackDecoder):
    """Bounded decoder that emits validated presentation-ready BGRx frames."""

    def __init__(
        self,
        payload_type: int,
        *,
        runtime_root: pathlib.Path | None = None,
        max_packet_bytes: int = 65_535,
        max_frame_bytes: int = 32 * 1024 * 1024,
        max_frames_per_push: int = 8,
        operation_timeout_seconds: float = 0.5,
        pull_timeout_ms: int = 5,
        backend: _PresentationBackend | None = None,
    ) -> None:
        selected_backend = backend
        if selected_backend is None:
            root = runtime_root
            if root is None:
                configured = os.environ.get("K5_GSTREAMER_ROOT", "").strip()
                if not configured:
                    raise NativePlaybackDecoderError(
                        NativePlaybackDecoderErrorCode.RUNTIME_UNAVAILABLE,
                        "native decoder runtime is unavailable",
                    )
                root = pathlib.Path(configured)
            selected_backend = _PresentationCtypesBackend(
                root,
                payload_type,
                max_frame_bytes=max_frame_bytes,
                max_frames_per_push=max_frames_per_push,
                pull_timeout_ms=pull_timeout_ms,
            )

        super().__init__(
            payload_type,
            max_packet_bytes=max_packet_bytes,
            max_frame_bytes=max_frame_bytes,
            max_frames_per_push=max_frames_per_push,
            operation_timeout_seconds=operation_timeout_seconds,
            pull_timeout_ms=pull_timeout_ms,
            backend=selected_backend,  # type: ignore[arg-type]
        )

    def _frames(
        self,
        payloads: Sequence[object],
        source_elapsed_ms: int,
    ) -> list[PresentationVideoFrame]:
        if len(payloads) > self._max_frames_per_push:
            self._state = NativePlaybackDecoderState.FAILED
            raise NativePlaybackDecoderError(
                NativePlaybackDecoderErrorCode.FRAME_LIMIT,
                "native decoder emitted too many presentation frames",
            )

        frames: list[PresentationVideoFrame] = []
        for item in payloads:
            if not isinstance(item, _PresentationPayload):
                self._state = NativePlaybackDecoderState.FAILED
                raise NativePlaybackDecoderError(
                    NativePlaybackDecoderErrorCode.NATIVE_FAILURE,
                    "native decoder emitted invalid presentation metadata",
                )
            size = len(item.payload)
            if not 1 <= size <= self._max_frame_bytes:
                self._state = NativePlaybackDecoderState.FAILED
                raise NativePlaybackDecoderError(
                    NativePlaybackDecoderErrorCode.FRAME_TOO_LARGE,
                    "native decoder emitted an invalid frame size",
                )
            try:
                frame = PresentationVideoFrame(
                    payload=memoryview(item.payload),
                    width=item.width,
                    height=item.height,
                    stride_bytes=item.stride_bytes,
                    pixel_format=PixelFormat.BGRX,
                    source_elapsed_ms=source_elapsed_ms,
                )
            except PresentationFrameError:
                self._state = NativePlaybackDecoderState.FAILED
                raise NativePlaybackDecoderError(
                    NativePlaybackDecoderErrorCode.NATIVE_FAILURE,
                    "native decoder emitted invalid presentation metadata",
                ) from None
            self._emitted_frames += 1
            self._emitted_bytes += size
            frames.append(frame)
        return frames
