"""The real native live/playback builders must consume the admitted decoder."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from k5vision.media.gstreamer_direct_frame_delivery import _DirectRtspFrameBackend
from k5vision.media.gstreamer_playback_decoder import (
    NativePlaybackDecoderError,
    _CtypesGStreamerBackend,
)


@pytest.mark.parametrize("element", ["d3d11h264dec", "avdec_h264"])
@pytest.mark.parametrize("backend_type", [_DirectRtspFrameBackend, _CtypesGStreamerBackend])
def test_admitted_decoder_used_by_actual_pipeline_builder(backend_type, element):
    backend = backend_type.__new__(backend_type)
    backend._source_uri = "rtsp://fixture.invalid/nonexistent"
    backend._decoder_element = element
    backend._core = Mock()
    backend._core.gst_init_check.return_value = 1
    backend._core.gst_parse_launch_full.return_value = None
    with pytest.raises(NativePlaybackDecoderError, match="pipeline could not be created"):
        backend._build_pipeline(96)
    arguments = backend._core.gst_parse_launch_full.call_args.args
    assert f"! {element} !".encode() in arguments[0]
    other = "d3d11h264dec" if element == "avdec_h264" else "avdec_h264"
    assert other.encode() not in arguments[0]
    assert arguments[1:] == (None, 1, None)
    backend._core.gst_element_set_state.assert_not_called()
