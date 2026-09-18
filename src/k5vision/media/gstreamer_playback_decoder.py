"""Concrete bounded GStreamer ``PlaybackDecoder`` over the accepted native ABI.

RTP packets and decoded frame bytes are transient only. Observable state contains
counters/lifecycle information and never source, credential, path, RTP, or frame data.
"""

from __future__ import annotations

import asyncio
import ctypes
import enum
import os
import pathlib
from collections.abc import Sequence
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from k5vision.media.playback_decode import DecodedVideoFrame
from k5vision.media.rtp_delivery import is_rtp_v2

_MAX_PACKET_BYTES = 65_535
_MAX_FRAME_BYTES = 128 * 1024 * 1024
_MAX_FRAMES_PER_PUSH = 32
_MAX_SOURCE_ELAPSED_MS = 2_147_483_647
_GST_STATE_NULL = 1
_GST_STATE_PLAYING = 4
_GST_STATE_CHANGE_FAILURE = 0
_GST_FLOW_OK = 0
_GST_MAP_READ = 1
_CORE_FILENAMES = ("gstreamer-1.0-0.dll", "libgstreamer-1.0-0.dll")
_APP_FILENAMES = ("gstapp-1.0-0.dll", "libgstapp-1.0-0.dll")


class NativePlaybackDecoderErrorCode(enum.StrEnum):
    INVALID_STATE = "invalid_state"
    INVALID_PACKET = "invalid_packet"
    RUNTIME_UNAVAILABLE = "runtime_unavailable"
    NATIVE_FAILURE = "native_failure"
    FRAME_LIMIT = "frame_limit"
    FRAME_TOO_LARGE = "frame_too_large"


class NativePlaybackDecoderError(RuntimeError):
    """Sanitized decoder failure that never includes runtime or media detail."""

    def __init__(self, code: NativePlaybackDecoderErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class NativePlaybackDecoderState(enum.StrEnum):
    CREATED = "created"
    RUNNING = "running"
    EOS = "eos"
    CLOSED = "closed"
    FAILED = "failed"


class NativePlaybackDecoderSnapshot(BaseModel):
    """Source-free adapter observability."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str = "1"
    state: NativePlaybackDecoderState
    pushed_packets: int = Field(ge=0)
    emitted_frames: int = Field(ge=0)
    emitted_bytes: int = Field(ge=0)


class _DecoderBackend(Protocol):
    def push(self, packet: bytes) -> Sequence[bytes]: ...

    def end_of_stream(self) -> Sequence[bytes]: ...

    def close(self) -> None: ...


class _GstMapInfo(ctypes.Structure):
    _fields_ = [
        ("memory", ctypes.c_void_p),
        ("flags", ctypes.c_int),
        ("data", ctypes.POINTER(ctypes.c_uint8)),
        ("size", ctypes.c_size_t),
        ("maxsize", ctypes.c_size_t),
        ("user_data", ctypes.c_void_p * 4),
        ("reserved", ctypes.c_void_p * 4),
    ]


def _find_runtime_library(bin_root: pathlib.Path, names: tuple[str, ...]) -> pathlib.Path:
    for name in names:
        candidate = bin_root / name
        if candidate.is_file():
            return candidate
    raise NativePlaybackDecoderError(
        NativePlaybackDecoderErrorCode.RUNTIME_UNAVAILABLE,
        "native decoder runtime is unavailable",
    )


class _CtypesGStreamerBackend:
    """Synchronous native pipeline; async bounds are enforced by the public adapter."""

    def __init__(
        self,
        runtime_root: pathlib.Path,
        payload_type: int,
        *,
        max_frame_bytes: int,
        max_frames_per_push: int,
        pull_timeout_ms: int,
    ) -> None:
        root = runtime_root.expanduser().resolve(strict=False)
        bin_root = root / "bin"
        if not root.is_dir() or not bin_root.is_dir():
            raise NativePlaybackDecoderError(
                NativePlaybackDecoderErrorCode.RUNTIME_UNAVAILABLE,
                "native decoder runtime is unavailable",
            )
        loader = getattr(ctypes, "WinDLL", None)
        if loader is None:
            raise NativePlaybackDecoderError(
                NativePlaybackDecoderErrorCode.RUNTIME_UNAVAILABLE,
                "native decoder runtime is unavailable",
            )

        self._dll_directory = None
        add_dll_directory = getattr(os, "add_dll_directory", None)
        if callable(add_dll_directory):
            try:
                self._dll_directory = add_dll_directory(str(bin_root))
            except OSError:
                raise NativePlaybackDecoderError(
                    NativePlaybackDecoderErrorCode.RUNTIME_UNAVAILABLE,
                    "native decoder runtime is unavailable",
                ) from None

        try:
            self._core = loader(str(_find_runtime_library(bin_root, _CORE_FILENAMES)))
            self._app = loader(str(_find_runtime_library(bin_root, _APP_FILENAMES)))
            self._bind_signatures()
            self._max_frame_bytes = max_frame_bytes
            self._max_frames_per_push = max_frames_per_push
            self._pull_timeout_ns = pull_timeout_ms * 1_000_000
            self._pipeline = ctypes.c_void_p()
            self._source = ctypes.c_void_p()
            self._sink = ctypes.c_void_p()
            self._closed = False
            self._build_pipeline(payload_type)
        except NativePlaybackDecoderError:
            self._close_dll_directory()
            raise
        except (AttributeError, OSError, ValueError, TypeError):
            self._close_dll_directory()
            raise NativePlaybackDecoderError(
                NativePlaybackDecoderErrorCode.NATIVE_FAILURE,
                "native decoder initialization failed",
            ) from None

    def _bind_signatures(self) -> None:
        self._core.gst_init_check.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        self._core.gst_init_check.restype = ctypes.c_int
        self._core.gst_parse_launch.argtypes = [ctypes.c_char_p, ctypes.c_void_p]
        self._core.gst_parse_launch.restype = ctypes.c_void_p
        self._core.gst_bin_get_by_name.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        self._core.gst_bin_get_by_name.restype = ctypes.c_void_p
        self._core.gst_element_set_state.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self._core.gst_element_set_state.restype = ctypes.c_int
        self._core.gst_object_unref.argtypes = [ctypes.c_void_p]
        self._core.gst_object_unref.restype = None
        self._core.gst_buffer_new_allocate.argtypes = [
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_void_p,
        ]
        self._core.gst_buffer_new_allocate.restype = ctypes.c_void_p
        self._core.gst_buffer_fill.argtypes = [
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.c_size_t,
        ]
        self._core.gst_buffer_fill.restype = ctypes.c_size_t
        self._core.gst_buffer_get_size.argtypes = [ctypes.c_void_p]
        self._core.gst_buffer_get_size.restype = ctypes.c_size_t
        self._core.gst_buffer_map.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_GstMapInfo),
            ctypes.c_int,
        ]
        self._core.gst_buffer_map.restype = ctypes.c_int
        self._core.gst_buffer_unmap.argtypes = [ctypes.c_void_p, ctypes.POINTER(_GstMapInfo)]
        self._core.gst_buffer_unmap.restype = None
        self._core.gst_sample_get_buffer.argtypes = [ctypes.c_void_p]
        self._core.gst_sample_get_buffer.restype = ctypes.c_void_p
        self._core.gst_mini_object_unref.argtypes = [ctypes.c_void_p]
        self._core.gst_mini_object_unref.restype = None
        self._app.gst_app_src_push_buffer.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self._app.gst_app_src_push_buffer.restype = ctypes.c_int
        self._app.gst_app_src_end_of_stream.argtypes = [ctypes.c_void_p]
        self._app.gst_app_src_end_of_stream.restype = ctypes.c_int
        self._app.gst_app_sink_try_pull_sample.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
        self._app.gst_app_sink_try_pull_sample.restype = ctypes.c_void_p
        self._app.gst_app_sink_is_eos.argtypes = [ctypes.c_void_p]
        self._app.gst_app_sink_is_eos.restype = ctypes.c_int

    def _build_pipeline(self, payload_type: int) -> None:
        if not self._core.gst_init_check(None, None, None):
            raise NativePlaybackDecoderError(
                NativePlaybackDecoderErrorCode.NATIVE_FAILURE,
                "native decoder initialization failed",
            )
        description = (
            "appsrc name=k5src is-live=false block=true format=time "
            f"caps=application/x-rtp,media=video,encoding-name=H264,"
            f"clock-rate=90000,payload={payload_type} "
            "! rtph264depay ! h264parse ! d3d11h264dec ! videoconvert "
            "! video/x-raw,format=BGRx "
            "! appsink name=k5sink sync=false max-buffers=4 drop=true"
        ).encode("ascii")
        pipeline = self._core.gst_parse_launch(description, None)
        if not pipeline:
            raise NativePlaybackDecoderError(
                NativePlaybackDecoderErrorCode.NATIVE_FAILURE,
                "native decoder pipeline could not be created",
            )
        self._pipeline = ctypes.c_void_p(pipeline)
        source = self._core.gst_bin_get_by_name(self._pipeline, b"k5src")
        sink = self._core.gst_bin_get_by_name(self._pipeline, b"k5sink")
        if not source or not sink:
            self.close()
            raise NativePlaybackDecoderError(
                NativePlaybackDecoderErrorCode.NATIVE_FAILURE,
                "native decoder endpoints are unavailable",
            )
        self._source = ctypes.c_void_p(source)
        self._sink = ctypes.c_void_p(sink)
        state_result = self._core.gst_element_set_state(self._pipeline, _GST_STATE_PLAYING)
        if state_result == _GST_STATE_CHANGE_FAILURE:
            self.close()
            raise NativePlaybackDecoderError(
                NativePlaybackDecoderErrorCode.NATIVE_FAILURE,
                "native decoder pipeline failed to start",
            )

    def _copy_sample(self, sample: int) -> bytes:
        try:
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
                return ctypes.string_at(info.data, size)
            finally:
                self._core.gst_buffer_unmap(buffer_ptr, ctypes.byref(info))
        finally:
            self._core.gst_mini_object_unref(sample)

    def _drain(self, *, flush: bool) -> list[bytes]:
        frames: list[bytes] = []
        idle_polls = 0
        while len(frames) < self._max_frames_per_push:
            timeout_ns = self._pull_timeout_ns if flush or not frames else 0
            sample = self._app.gst_app_sink_try_pull_sample(self._sink, timeout_ns)
            if sample:
                frames.append(self._copy_sample(sample))
                idle_polls = 0
                continue
            if self._app.gst_app_sink_is_eos(self._sink):
                break
            if not flush:
                break
            idle_polls += 1
            if idle_polls >= 4:
                break
        if (
            len(frames) >= self._max_frames_per_push
            and flush
            and not self._app.gst_app_sink_is_eos(self._sink)
        ):
            raise NativePlaybackDecoderError(
                NativePlaybackDecoderErrorCode.FRAME_LIMIT,
                "native decoder flush exceeded the frame bound",
            )
        return frames

    def push(self, packet: bytes) -> Sequence[bytes]:
        buffer_ptr = self._core.gst_buffer_new_allocate(None, len(packet), None)
        if not buffer_ptr:
            raise NativePlaybackDecoderError(
                NativePlaybackDecoderErrorCode.NATIVE_FAILURE,
                "native decoder input allocation failed",
            )
        source = ctypes.create_string_buffer(packet)
        written = int(self._core.gst_buffer_fill(buffer_ptr, 0, source, len(packet)))
        if written != len(packet):
            self._core.gst_mini_object_unref(buffer_ptr)
            raise NativePlaybackDecoderError(
                NativePlaybackDecoderErrorCode.NATIVE_FAILURE,
                "native decoder input copy failed",
            )
        flow = int(self._app.gst_app_src_push_buffer(self._source, buffer_ptr))
        if flow != _GST_FLOW_OK:
            raise NativePlaybackDecoderError(
                NativePlaybackDecoderErrorCode.NATIVE_FAILURE,
                "native decoder rejected input",
            )
        return self._drain(flush=False)

    def end_of_stream(self) -> Sequence[bytes]:
        flow = int(self._app.gst_app_src_end_of_stream(self._source))
        if flow != _GST_FLOW_OK:
            raise NativePlaybackDecoderError(
                NativePlaybackDecoderErrorCode.NATIVE_FAILURE,
                "native decoder failed to enter end-of-stream",
            )
        return self._drain(flush=True)

    def _close_dll_directory(self) -> None:
        handle = self._dll_directory
        self._dll_directory = None
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass

    def close(self) -> None:
        if getattr(self, "_closed", True):
            self._close_dll_directory()
            return
        self._closed = True
        pipeline = self._pipeline
        source = self._source
        sink = self._sink
        self._pipeline = ctypes.c_void_p()
        self._source = ctypes.c_void_p()
        self._sink = ctypes.c_void_p()
        if pipeline:
            try:
                self._core.gst_element_set_state(pipeline, _GST_STATE_NULL)
            except (AttributeError, OSError, ValueError):
                pass
        for item in (source, sink, pipeline):
            if item:
                try:
                    self._core.gst_object_unref(item)
                except (AttributeError, OSError, ValueError):
                    pass
        self._close_dll_directory()


class GStreamerNativePlaybackDecoder:
    """Async bounded decoder implementing the project-owned Stage-17 protocol."""

    def __init__(
        self,
        payload_type: int,
        *,
        runtime_root: pathlib.Path | None = None,
        max_packet_bytes: int = _MAX_PACKET_BYTES,
        max_frame_bytes: int = 32 * 1024 * 1024,
        max_frames_per_push: int = 8,
        operation_timeout_seconds: float = 0.5,
        pull_timeout_ms: int = 5,
        backend: _DecoderBackend | None = None,
    ) -> None:
        if not 96 <= payload_type <= 127:
            raise ValueError("payload_type must be between 96 and 127")
        if not 12 <= max_packet_bytes <= _MAX_PACKET_BYTES:
            raise ValueError("max_packet_bytes must be between 12 and 65535")
        if not 1 <= max_frame_bytes <= _MAX_FRAME_BYTES:
            raise ValueError("max_frame_bytes must be between 1 and 134217728")
        if not 1 <= max_frames_per_push <= _MAX_FRAMES_PER_PUSH:
            raise ValueError("max_frames_per_push must be between 1 and 32")
        if not 0 < operation_timeout_seconds <= 10:
            raise ValueError("operation_timeout_seconds must be between zero and 10")
        if not 0 <= pull_timeout_ms <= 100:
            raise ValueError("pull_timeout_ms must be between zero and 100")

        if backend is None:
            root = runtime_root
            if root is None:
                configured = os.environ.get("K5_GSTREAMER_ROOT", "").strip()
                if not configured:
                    raise NativePlaybackDecoderError(
                        NativePlaybackDecoderErrorCode.RUNTIME_UNAVAILABLE,
                        "native decoder runtime is unavailable",
                    )
                root = pathlib.Path(configured)
            backend = _CtypesGStreamerBackend(
                root,
                payload_type,
                max_frame_bytes=max_frame_bytes,
                max_frames_per_push=max_frames_per_push,
                pull_timeout_ms=pull_timeout_ms,
            )

        self._backend = backend
        self._max_packet_bytes = max_packet_bytes
        self._max_frame_bytes = max_frame_bytes
        self._max_frames_per_push = max_frames_per_push
        self._operation_timeout_seconds = operation_timeout_seconds
        self._state = NativePlaybackDecoderState.CREATED
        self._pushed_packets = 0
        self._emitted_frames = 0
        self._emitted_bytes = 0

    @property
    def snapshot(self) -> NativePlaybackDecoderSnapshot:
        return NativePlaybackDecoderSnapshot(
            state=self._state,
            pushed_packets=self._pushed_packets,
            emitted_frames=self._emitted_frames,
            emitted_bytes=self._emitted_bytes,
        )

    async def _call_backend(self, method: str, *args: object) -> Sequence[bytes]:
        try:
            function = getattr(self._backend, method)
            result = await asyncio.wait_for(
                asyncio.to_thread(function, *args),
                timeout=self._operation_timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            self._state = NativePlaybackDecoderState.FAILED
            raise NativePlaybackDecoderError(
                NativePlaybackDecoderErrorCode.NATIVE_FAILURE,
                "native decoder operation timed out",
            ) from None
        except NativePlaybackDecoderError:
            self._state = NativePlaybackDecoderState.FAILED
            raise
        except Exception:
            self._state = NativePlaybackDecoderState.FAILED
            raise NativePlaybackDecoderError(
                NativePlaybackDecoderErrorCode.NATIVE_FAILURE,
                "native decoder operation failed",
            ) from None
        return result

    def _frames(self, payloads: Sequence[bytes], source_elapsed_ms: int) -> list[DecodedVideoFrame]:
        if len(payloads) > self._max_frames_per_push:
            self._state = NativePlaybackDecoderState.FAILED
            raise NativePlaybackDecoderError(
                NativePlaybackDecoderErrorCode.FRAME_LIMIT,
                "native decoder emitted too many frames",
            )
        frames: list[DecodedVideoFrame] = []
        for payload in payloads:
            size = len(payload)
            if not 1 <= size <= self._max_frame_bytes:
                self._state = NativePlaybackDecoderState.FAILED
                raise NativePlaybackDecoderError(
                    NativePlaybackDecoderErrorCode.FRAME_TOO_LARGE,
                    "native decoder emitted an invalid frame size",
                )
            self._emitted_frames += 1
            self._emitted_bytes += size
            frames.append(DecodedVideoFrame(memoryview(payload), source_elapsed_ms))
        return frames

    async def decode(
        self,
        packet: memoryview,
        source_elapsed_ms: int,
    ) -> Sequence[DecodedVideoFrame]:
        if self._state not in {
            NativePlaybackDecoderState.CREATED,
            NativePlaybackDecoderState.RUNNING,
        }:
            raise NativePlaybackDecoderError(
                NativePlaybackDecoderErrorCode.INVALID_STATE,
                "native decoder cannot accept input from current state",
            )
        if not 0 <= source_elapsed_ms <= _MAX_SOURCE_ELAPSED_MS:
            raise NativePlaybackDecoderError(
                NativePlaybackDecoderErrorCode.INVALID_PACKET,
                "native decoder packet timing is invalid",
            )
        if not 12 <= len(packet) <= self._max_packet_bytes or not is_rtp_v2(packet):
            raise NativePlaybackDecoderError(
                NativePlaybackDecoderErrorCode.INVALID_PACKET,
                "native decoder input is not a valid bounded RTP packet",
            )
        self._state = NativePlaybackDecoderState.RUNNING
        payloads = await self._call_backend("push", bytes(packet))
        self._pushed_packets += 1
        return self._frames(payloads, source_elapsed_ms)

    async def flush(self) -> Sequence[DecodedVideoFrame]:
        if self._state == NativePlaybackDecoderState.EOS:
            return []
        if self._state not in {
            NativePlaybackDecoderState.CREATED,
            NativePlaybackDecoderState.RUNNING,
        }:
            raise NativePlaybackDecoderError(
                NativePlaybackDecoderErrorCode.INVALID_STATE,
                "native decoder cannot flush from current state",
            )
        payloads = await self._call_backend("end_of_stream")
        self._state = NativePlaybackDecoderState.EOS
        return self._frames(payloads, 0)

    async def close(self) -> None:
        if self._state == NativePlaybackDecoderState.CLOSED:
            return
        try:
            await asyncio.wait_for(
                asyncio.to_thread(self._backend.close),
                timeout=self._operation_timeout_seconds,
            )
        except asyncio.CancelledError:
            self._state = NativePlaybackDecoderState.CLOSED
            raise
        except Exception:
            self._state = NativePlaybackDecoderState.FAILED
            raise NativePlaybackDecoderError(
                NativePlaybackDecoderErrorCode.NATIVE_FAILURE,
                "native decoder cleanup failed",
            ) from None
        self._state = NativePlaybackDecoderState.CLOSED
