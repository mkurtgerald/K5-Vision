"""Fail closed when the reviewed GStreamer parser finds a missing codec.

No camera source, network, samples or media retention is involved.
"""

from __future__ import annotations

import ctypes
import ctypes.util
from unittest.mock import Mock

import pytest

from k5vision.media.gstreamer_playback_decoder import (
    NativePlaybackDecoderError,
    _CtypesGStreamerBackend,
)
from k5vision.media.gstreamer_rtp_pull import _CtypesRtpPullBackend


def test_decoder_rejects_partial_pipeline_before_playing() -> None:
    backend = _CtypesGStreamerBackend.__new__(_CtypesGStreamerBackend)
    backend._core = Mock()
    backend._core.gst_init_check.return_value = 1
    backend._core.gst_parse_launch_full.return_value = None
    with pytest.raises(NativePlaybackDecoderError, match="pipeline could not be created"):
        backend._build_pipeline(96)
    assert backend._core.gst_parse_launch_full.call_args.args[1:] == (None, 1, None)
    backend._core.gst_element_set_state.assert_not_called()


def test_rtp_rejects_partial_pipeline_without_exposing_source() -> None:
    backend = _CtypesRtpPullBackend.__new__(_CtypesRtpPullBackend)
    backend._core = Mock()
    backend._core.gst_init_check.return_value = 1
    backend._core.gst_parse_launch_full.return_value = None
    backend._pipeline = ctypes.c_void_p()
    with pytest.raises(RuntimeError, match="pipeline could not be created") as result:
        backend._start("rtsp://user:secret@example.invalid/live", 100)
    assert backend._core.gst_parse_launch_full.call_args.args[1:] == (None, 1, None)
    backend._core.gst_element_set_state.assert_not_called()
    assert "secret" not in str(result.value)


def test_native_parser_rejects_incomplete_pipeline() -> None:
    library = ctypes.util.find_library("gstreamer-1.0")
    if not library:
        pytest.skip("Native GStreamer not installed")
    core = ctypes.CDLL(library)
    core.gst_init_check.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    core.gst_init_check.restype = ctypes.c_int
    core.gst_parse_launch.argtypes = [ctypes.c_char_p, ctypes.c_void_p]
    core.gst_parse_launch.restype = ctypes.c_void_p
    core.gst_parse_launch_full.argtypes = [
        ctypes.c_char_p,
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
    ]
    core.gst_parse_launch_full.restype = ctypes.c_void_p
    core.gst_object_unref.argtypes = [ctypes.c_void_p]
    core.gst_object_unref.restype = None
    assert core.gst_init_check(None, None, None)
    desc = b"videotestsrc ! k5_missing_decoder_for_regression ! fakesink"
    incomplete = core.gst_parse_launch(desc, None)
    strict = core.gst_parse_launch_full(desc, None, 1, None)
    try:
        assert incomplete
        assert not strict
    finally:
        if incomplete:
            core.gst_object_unref(incomplete)
        if strict:
            core.gst_object_unref(strict)
