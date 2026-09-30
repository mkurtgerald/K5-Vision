"""Direct in-process RTSP/TCP RTP pull for the local synthetic alpha lane.

This boundary removes the loopback UDP handoff from the synthetic acceptance path.
RTSP media remains transient and source material never appears in child-process argv.
"""

from __future__ import annotations

import asyncio
import ctypes
import os
import pathlib
import time
from collections.abc import Awaitable, Callable
from typing import Protocol

from k5vision.media.native_rtsp_pipeline import quote_pipeline_value
from k5vision.media.rtp_delivery import (
    RtpConsumer,
    RtpDeliveryError,
    RtpDeliveryErrorCode,
    RtpDeliveryResult,
    is_rtp_v2,
)

_GST_STATE_NULL = 1
_GST_STATE_PLAYING = 4
_GST_STATE_CHANGE_FAILURE = 0
_GST_MAP_READ = 1
_CORE_FILENAMES = ("gstreamer-1.0-0.dll", "libgstreamer-1.0-0.dll")
_APP_FILENAMES = ("gstapp-1.0-0.dll", "libgstapp-1.0-0.dll")


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


class _RtpPullBackend(Protocol):
    def pull(self, timeout_ms: int) -> bytes | None: ...

    def close(self) -> None: ...


BackendFactory = Callable[[str, int, int], _RtpPullBackend]


def _runtime_root() -> pathlib.Path:
    configured = os.environ.get("K5_GSTREAMER_ROOT", "").strip()
    if not configured:
        raise RuntimeError("reviewed GStreamer runtime is unavailable")
    root = pathlib.Path(configured).expanduser().resolve(strict=False)
    if not root.is_dir() or not (root / "bin").is_dir():
        raise RuntimeError("reviewed GStreamer runtime is unavailable")
    return root


def _find_runtime_library(bin_root: pathlib.Path, names: tuple[str, ...]) -> pathlib.Path:
    for name in names:
        candidate = bin_root / name
        if candidate.is_file():
            return candidate
    raise RuntimeError("reviewed GStreamer runtime is unavailable")


class _CtypesRtpPullBackend:
    """Synchronous appsink-backed RTP reader over RTSP/TCP."""

    def __init__(self, source_uri: str, max_packet_bytes: int, startup_probe_ms: int) -> None:
        root = _runtime_root()
        bin_root = root / "bin"
        loader = getattr(ctypes, "WinDLL", None)
        if loader is None:
            raise RuntimeError("reviewed GStreamer runtime is unavailable")

        self._dll_directory = None
        self._pipeline = ctypes.c_void_p()
        self._sink = ctypes.c_void_p()
        self._core = None
        self._app = None
        self._max_packet_bytes = max_packet_bytes
        add_dll_directory = getattr(os, "add_dll_directory", None)
        try:
            if callable(add_dll_directory):
                self._dll_directory = add_dll_directory(str(bin_root))
            self._core = loader(str(_find_runtime_library(bin_root, _CORE_FILENAMES)))
            self._app = loader(str(_find_runtime_library(bin_root, _APP_FILENAMES)))
            self._bind()
            self._start(source_uri, startup_probe_ms)
        except Exception:
            self.close()
            raise RuntimeError("direct RTSP RTP reader failed to initialize") from None

    def _bind(self) -> None:
        core = self._core
        app = self._app
        if core is None or app is None:
            raise RuntimeError("reviewed GStreamer runtime is unavailable")
        core.gst_init_check.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        core.gst_init_check.restype = ctypes.c_int
        core.gst_parse_launch.argtypes = [ctypes.c_char_p, ctypes.c_void_p]
        core.gst_parse_launch.restype = ctypes.c_void_p
        core.gst_bin_get_by_name.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        core.gst_bin_get_by_name.restype = ctypes.c_void_p
        core.gst_element_set_state.argtypes = [ctypes.c_void_p, ctypes.c_int]
        core.gst_element_set_state.restype = ctypes.c_int
        core.gst_element_get_state.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
            ctypes.c_uint64,
        ]
        core.gst_element_get_state.restype = ctypes.c_int
        core.gst_sample_get_buffer.argtypes = [ctypes.c_void_p]
        core.gst_sample_get_buffer.restype = ctypes.c_void_p
        core.gst_buffer_get_size.argtypes = [ctypes.c_void_p]
        core.gst_buffer_get_size.restype = ctypes.c_size_t
        core.gst_buffer_map.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_GstMapInfo),
            ctypes.c_int,
        ]
        core.gst_buffer_map.restype = ctypes.c_int
        core.gst_buffer_unmap.argtypes = [ctypes.c_void_p, ctypes.POINTER(_GstMapInfo)]
        core.gst_buffer_unmap.restype = None
        core.gst_mini_object_unref.argtypes = [ctypes.c_void_p]
        core.gst_mini_object_unref.restype = None
        core.gst_object_unref.argtypes = [ctypes.c_void_p]
        core.gst_object_unref.restype = None
        app.gst_app_sink_try_pull_sample.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
        app.gst_app_sink_try_pull_sample.restype = ctypes.c_void_p

    def _start(self, source_uri: str, startup_probe_ms: int) -> None:
        core = self._core
        if core is None or not core.gst_init_check(None, None, None):
            raise RuntimeError("native GStreamer initialization failed")
        location = quote_pipeline_value(source_uri)
        description = (
            f"rtspsrc location={location} protocols=tcp latency=100 "
            "tcp-timeout=5000000 teardown-timeout=0 "
            "! application/x-rtp,media=video "
            "! queue max-size-buffers=16 max-size-bytes=0 max-size-time=0 leaky=downstream "
            "! appsink name=k5sink sync=false max-buffers=16 drop=true"
        ).encode("utf-8")
        pipeline = core.gst_parse_launch(description, None)
        if not pipeline:
            raise RuntimeError("direct RTSP RTP pipeline could not be created")
        self._pipeline = ctypes.c_void_p(pipeline)
        sink = core.gst_bin_get_by_name(self._pipeline, b"k5sink")
        if not sink:
            raise RuntimeError("direct RTSP RTP sink is unavailable")
        self._sink = ctypes.c_void_p(sink)
        if core.gst_element_set_state(self._pipeline, _GST_STATE_PLAYING) == _GST_STATE_CHANGE_FAILURE:
            raise RuntimeError("direct RTSP RTP pipeline failed to start")
        current = ctypes.c_int()
        pending = ctypes.c_int()
        result = core.gst_element_get_state(
            self._pipeline,
            ctypes.byref(current),
            ctypes.byref(pending),
            max(1, startup_probe_ms) * 1_000_000,
        )
        if result == _GST_STATE_CHANGE_FAILURE:
            raise RuntimeError("direct RTSP RTP pipeline failed during startup")

    def pull(self, timeout_ms: int) -> bytes | None:
        if not 1 <= timeout_ms <= 5_000:
            raise ValueError("timeout_ms must be between 1 and 5000")
        if self._app is None or not self._sink:
            raise RuntimeError("direct RTSP RTP reader is unavailable")
        sample = self._app.gst_app_sink_try_pull_sample(self._sink, timeout_ms * 1_000_000)
        if not sample:
            return None
        try:
            if self._core is None:
                raise RuntimeError("direct RTSP RTP reader is unavailable")
            buffer_ptr = self._core.gst_sample_get_buffer(sample)
            if not buffer_ptr:
                raise RuntimeError("direct RTSP RTP reader produced an invalid packet")
            size = int(self._core.gst_buffer_get_size(buffer_ptr))
            if not 1 <= size <= self._max_packet_bytes:
                raise RuntimeError("direct RTSP RTP packet violated the byte bound")
            info = _GstMapInfo()
            if not self._core.gst_buffer_map(buffer_ptr, ctypes.byref(info), _GST_MAP_READ):
                raise RuntimeError("direct RTSP RTP packet mapping failed")
            try:
                if not info.data or int(info.size) != size:
                    raise RuntimeError("direct RTSP RTP packet mapping failed")
                return ctypes.string_at(info.data, size)
            finally:
                self._core.gst_buffer_unmap(buffer_ptr, ctypes.byref(info))
        finally:
            if self._core is not None:
                self._core.gst_mini_object_unref(sample)

    def close(self) -> None:
        pipeline = getattr(self, "_pipeline", ctypes.c_void_p())
        sink = getattr(self, "_sink", ctypes.c_void_p())
        core = getattr(self, "_core", None)
        self._pipeline = ctypes.c_void_p()
        self._sink = ctypes.c_void_p()
        if pipeline and core is not None:
            try:
                core.gst_element_set_state(pipeline, _GST_STATE_NULL)
            except Exception:
                pass
        if sink and core is not None:
            try:
                core.gst_object_unref(sink)
            except Exception:
                pass
        if pipeline and core is not None:
            try:
                core.gst_object_unref(pipeline)
            except Exception:
                pass
        handle = getattr(self, "_dll_directory", None)
        self._dll_directory = None
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass


def _default_backend_factory(
    source_uri: str,
    max_packet_bytes: int,
    startup_probe_ms: int,
) -> _RtpPullBackend:
    return _CtypesRtpPullBackend(source_uri, max_packet_bytes, startup_probe_ms)


class GStreamerDirectRtpDelivery:
    """Pull bounded RTP packets directly from RTSP/TCP and feed K5 without UDP relay."""

    def __init__(
        self,
        *,
        packet_goal: int = 2048,
        delivery_timeout_seconds: float = 30.0,
        consumer_timeout_seconds: float = 4.0,
        max_packet_bytes: int = 65_535,
        pull_poll_ms: int = 100,
        startup_probe_ms: int = 500,
        backend_factory: BackendFactory | None = None,
    ) -> None:
        if not 1 <= packet_goal <= 4096:
            raise ValueError("packet_goal must be between 1 and 4096")
        if not 0 < delivery_timeout_seconds <= 60:
            raise ValueError("delivery_timeout_seconds must be between zero and 60")
        if not 0 < consumer_timeout_seconds <= 10:
            raise ValueError("consumer_timeout_seconds must be between zero and 10")
        if not 512 <= max_packet_bytes <= 65_535:
            raise ValueError("max_packet_bytes must be between 512 and 65535")
        if not 1 <= pull_poll_ms <= 5_000:
            raise ValueError("pull_poll_ms must be between 1 and 5000")
        if not 1 <= startup_probe_ms <= 30_000:
            raise ValueError("startup_probe_ms must be between 1 and 30000")
        self._packet_goal = packet_goal
        self._delivery_timeout_seconds = delivery_timeout_seconds
        self._consumer_timeout_seconds = consumer_timeout_seconds
        self._max_packet_bytes = max_packet_bytes
        self._pull_poll_ms = pull_poll_ms
        self._startup_probe_ms = startup_probe_ms
        self._backend_factory = backend_factory or _default_backend_factory

    async def deliver(self, source_uri: str, consumer: RtpConsumer) -> RtpDeliveryResult:
        """Deliver transient RTP packets without using a loopback UDP socket."""
        started = time.monotonic()
        try:
            backend = await asyncio.to_thread(
                self._backend_factory,
                source_uri,
                self._max_packet_bytes,
                self._startup_probe_ms,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            raise RtpDeliveryError(
                RtpDeliveryErrorCode.RUNTIME_FAILURE,
                "direct RTP reader failed to start",
            ) from None

        valid_packets = 0
        invalid_packets = 0
        delivered_bytes = 0
        try:
            deadline = started + self._delivery_timeout_seconds
            while valid_packets < self._packet_goal:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RtpDeliveryError(
                        RtpDeliveryErrorCode.TIMEOUT,
                        "RTP delivery timed out",
                    )
                timeout_ms = max(
                    1,
                    min(self._pull_poll_ms, int(remaining * 1000)),
                )
                try:
                    packet = await asyncio.to_thread(backend.pull, timeout_ms)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    raise RtpDeliveryError(
                        RtpDeliveryErrorCode.RUNTIME_FAILURE,
                        "direct RTP reader failed",
                    ) from None
                if packet is None:
                    continue
                if not is_rtp_v2(packet):
                    invalid_packets += 1
                    continue
                try:
                    await asyncio.wait_for(
                        consumer(memoryview(packet)),
                        timeout=self._consumer_timeout_seconds,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    raise RtpDeliveryError(
                        RtpDeliveryErrorCode.CONSUMER_FAILURE,
                        "RTP consumer failed",
                    ) from None
                valid_packets += 1
                delivered_bytes += len(packet)

            return RtpDeliveryResult(
                valid_packets=valid_packets,
                invalid_packets=invalid_packets,
                delivered_bytes=delivered_bytes,
                elapsed_ms=max(0, int((time.monotonic() - started) * 1000)),
            )
        finally:
            await asyncio.to_thread(backend.close)
