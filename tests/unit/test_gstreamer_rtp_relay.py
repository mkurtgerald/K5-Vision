from __future__ import annotations

import pytest

from k5vision.media.gstreamer_rtp_relay import build_rtp_relay_argv


def test_relay_argv_is_video_rtp_loopback_only_and_shell_free() -> None:
    source = "rtsp://user:secret@192.0.2.10/live?token=a&b=c"
    argv = build_rtp_relay_argv("gst-launch-1.0", source, 50000)

    assert argv[0] == "gst-launch-1.0"
    assert f"location={source}" in argv
    assert "protocols=udp" in argv
    assert "application/x-rtp,media=video" in argv
    assert "queue" in argv
    assert "max-size-buffers=8" in argv
    assert "leaky=downstream" in argv
    assert "udpsink" in argv
    assert "host=127.0.0.1" in argv
    assert "port=50000" in argv
    assert "fakesink" not in argv


def test_relay_argv_rejects_invalid_inputs() -> None:
    with pytest.raises(ValueError):
        build_rtp_relay_argv("", "rtsp://example.invalid/live", 50000)
    with pytest.raises(ValueError):
        build_rtp_relay_argv("gst-launch-1.0", "", 50000)
    with pytest.raises(ValueError):
        build_rtp_relay_argv("gst-launch-1.0", "rtsp://example.invalid/live", 0)
    with pytest.raises(ValueError):
        build_rtp_relay_argv("gst-launch-1.0", "rtsp://example.invalid/live", 65536)
