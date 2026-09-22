"""Native GStreamer RTSP pipeline boundary that keeps private source material out of argv."""

from __future__ import annotations

import ctypes
import os
import pathlib

_GST_STATE_NULL = 1
_GST_STATE_PLAYING = 4
_GST_STATE_CHANGE_FAILURE = 0
_GST_MESSAGE_EOS = 1
_GST_MESSAGE_ERROR = 2
_CORE_FILENAMES = ("gstreamer-1.0-0.dll", "libgstreamer-1.0-0.dll")


class NativeRtspPipelineError(RuntimeError):
    """Sanitized native-pipeline failure."""


class NativeRtspPipelineUnavailable(NativeRtspPipelineError):
    """Reviewed native runtime could not be loaded."""


class _GstMiniObject(ctypes.Structure):
    _fields_ = [
        ("type_", ctypes.c_size_t),
        ("refcount", ctypes.c_int),
        ("lockstate", ctypes.c_int),
        ("flags", ctypes.c_uint),
        ("copy", ctypes.c_void_p),
        ("dispose", ctypes.c_void_p),
        ("free", ctypes.c_void_p),
        ("priv_uint", ctypes.c_uint),
        ("priv_pointer", ctypes.c_void_p),
    ]


class _GstMessage(ctypes.Structure):
    _fields_ = [
        ("mini_object", _GstMiniObject),
        ("type_", ctypes.c_int),
    ]


def quote_pipeline_value(value: str) -> str:
    """Quote one GStreamer parse value without logging or retaining it."""
    if not value.strip():
        raise ValueError("source_uri must not be empty")
    if "\x00" in value or "\r" in value or "\n" in value:
        raise ValueError("source_uri contains unsupported control characters")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _runtime_root() -> pathlib.Path:
    configured = os.environ.get("K5_GSTREAMER_ROOT", "").strip()
    if not configured:
        raise NativeRtspPipelineUnavailable("reviewed GStreamer runtime is unavailable")
    root = pathlib.Path(configured).expanduser().resolve(strict=False)
    if not root.is_dir() or not (root / "bin").is_dir():
        raise NativeRtspPipelineUnavailable("reviewed GStreamer runtime is unavailable")
    return root


def _find_core(bin_root: pathlib.Path) -> pathlib.Path:
    for name in _CORE_FILENAMES:
        candidate = bin_root / name
        if candidate.is_file():
            return candidate
    raise NativeRtspPipelineUnavailable("reviewed GStreamer runtime is unavailable")


class NativeRtspPipeline:
    """One in-process GStreamer pipeline with bounded state and terminal waits."""

    def __init__(self, description: str, *, startup_probe_seconds: float) -> None:
        if not description.strip():
            raise ValueError("pipeline description must not be empty")
        if not 0 < startup_probe_seconds <= 30:
            raise ValueError("startup_probe_seconds must be between zero and 30")

        root = _runtime_root()
        bin_root = root / "bin"
        loader = getattr(ctypes, "WinDLL", None)
        if loader is None:
            raise NativeRtspPipelineUnavailable("reviewed GStreamer runtime is unavailable")

        self._dll_directory = None
        self._pipeline = ctypes.c_void_p()
        self._core = None
        add_dll_directory = getattr(os, "add_dll_directory", None)
        try:
            if callable(add_dll_directory):
                self._dll_directory = add_dll_directory(str(bin_root))
            self._core = loader(str(_find_core(bin_root)))
            self._bind()
            self._start(description, startup_probe_seconds)
        except NativeRtspPipelineError:
            self.close()
            raise
        except (AttributeError, OSError, TypeError, ValueError):
            self.close()
            raise NativeRtspPipelineError("native GStreamer pipeline failed") from None

    def _bind(self) -> None:
        core = self._core
        if core is None:
            raise NativeRtspPipelineUnavailable("reviewed GStreamer runtime is unavailable")
        core.gst_init_check.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        core.gst_init_check.restype = ctypes.c_int
        core.gst_parse_launch.argtypes = [ctypes.c_char_p, ctypes.c_void_p]
        core.gst_parse_launch.restype = ctypes.c_void_p
        core.gst_element_set_state.argtypes = [ctypes.c_void_p, ctypes.c_int]
        core.gst_element_set_state.restype = ctypes.c_int
        core.gst_element_get_state.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
            ctypes.c_uint64,
        ]
        core.gst_element_get_state.restype = ctypes.c_int
        core.gst_element_get_bus.argtypes = [ctypes.c_void_p]
        core.gst_element_get_bus.restype = ctypes.c_void_p
        core.gst_bus_timed_pop_filtered.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint64,
            ctypes.c_int,
        ]
        core.gst_bus_timed_pop_filtered.restype = ctypes.c_void_p
        core.gst_mini_object_unref.argtypes = [ctypes.c_void_p]
        core.gst_mini_object_unref.restype = None
        core.gst_object_unref.argtypes = [ctypes.c_void_p]
        core.gst_object_unref.restype = None

    def _await_state(self, timeout_seconds: float) -> None:
        core = self._core
        if core is None or not self._pipeline:
            raise NativeRtspPipelineError("native GStreamer pipeline failed")
        current = ctypes.c_int()
        pending = ctypes.c_int()
        result = core.gst_element_get_state(
            self._pipeline,
            ctypes.byref(current),
            ctypes.byref(pending),
            int(timeout_seconds * 1_000_000_000),
        )
        if result == _GST_STATE_CHANGE_FAILURE:
            raise NativeRtspPipelineError("native GStreamer pipeline failed")

    def _start(self, description: str, startup_probe_seconds: float) -> None:
        core = self._core
        if core is None or not core.gst_init_check(None, None, None):
            raise NativeRtspPipelineError("native GStreamer initialization failed")
        pipeline = core.gst_parse_launch(description.encode("utf-8"), None)
        if not pipeline:
            raise NativeRtspPipelineError("native GStreamer pipeline could not be created")
        self._pipeline = ctypes.c_void_p(pipeline)
        state_result = core.gst_element_set_state(self._pipeline, _GST_STATE_PLAYING)
        if state_result == _GST_STATE_CHANGE_FAILURE:
            raise NativeRtspPipelineError("native GStreamer pipeline failed to start")
        self._await_state(startup_probe_seconds)

    def wait_for_terminal(self, *, timeout_seconds: float) -> str:
        """Wait for EOS or ERROR without exposing native diagnostics."""
        if not 0 < timeout_seconds <= 120:
            raise ValueError("timeout_seconds must be between zero and 120")
        core = self._core
        if core is None or not self._pipeline:
            raise NativeRtspPipelineError("native GStreamer pipeline is unavailable")
        bus = core.gst_element_get_bus(self._pipeline)
        if not bus:
            raise NativeRtspPipelineError("native GStreamer bus is unavailable")
        message = None
        try:
            message = core.gst_bus_timed_pop_filtered(
                bus,
                int(timeout_seconds * 1_000_000_000),
                _GST_MESSAGE_EOS | _GST_MESSAGE_ERROR,
            )
            if not message:
                return "timeout"
            message_type = ctypes.cast(message, ctypes.POINTER(_GstMessage)).contents.type_
            if message_type == _GST_MESSAGE_EOS:
                return "eos"
            if message_type == _GST_MESSAGE_ERROR:
                return "error"
            raise NativeRtspPipelineError("native GStreamer returned an unexpected message")
        finally:
            if message:
                core.gst_mini_object_unref(message)
            core.gst_object_unref(bus)

    def close(self, *, timeout_seconds: float = 1.0) -> None:
        pipeline = getattr(self, "_pipeline", ctypes.c_void_p())
        core = getattr(self, "_core", None)
        self._pipeline = ctypes.c_void_p()
        if pipeline and core is not None:
            try:
                core.gst_element_set_state(pipeline, _GST_STATE_NULL)
                current = ctypes.c_int()
                pending = ctypes.c_int()
                core.gst_element_get_state(
                    pipeline,
                    ctypes.byref(current),
                    ctypes.byref(pending),
                    int(max(timeout_seconds, 0.001) * 1_000_000_000),
                )
            except (AttributeError, OSError, TypeError, ValueError):
                pass
            try:
                core.gst_object_unref(pipeline)
            except (AttributeError, OSError, TypeError, ValueError):
                pass
        handle = getattr(self, "_dll_directory", None)
        self._dll_directory = None
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass
