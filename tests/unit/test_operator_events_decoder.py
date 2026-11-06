"""The recorded-person worker decodes growing private MP4 snapshots without false UTC."""

from __future__ import annotations

import sys
from types import SimpleNamespace

from k5vision import operator_events_decoder as decoder


def test_nonseekable_fragmented_recording_falls_back_to_real_timestamp(monkeypatch):
    class Pixels:
        def tobytes(self, *, order):
            assert order == "C"
            return b"frame-pixels"

    class Capture:
        def __init__(self):
            self.position = -1
            self.read_attempts = 0
            self.seek_positions = []
            self.released = False

        def isOpened(self):
            return True

        def get(self, key):
            if key == 3:
                return 320
            if key == 4:
                return 240
            if key == 7:
                return 150
            if key == 5:
                return 15
            if key == 0:
                return 500
            raise AssertionError("Unexpected video property")

        def set(self, key, value):
            assert key == 0
            self.position = value
            self.seek_positions.append(value)
            return value == 0

        def read(self):
            self.read_attempts += 1
            if self.position != 0 or self.read_attempts > 2:
                return False, None
            return True, object()

        def release(self):
            self.released = True

    capture = Capture()
    cv2 = SimpleNamespace(
        CAP_FFMPEG=1900,
        CAP_PROP_OPEN_TIMEOUT_MSEC=53,
        CAP_PROP_READ_TIMEOUT_MSEC=54,
        CAP_PROP_FRAME_WIDTH=3,
        CAP_PROP_FRAME_HEIGHT=4,
        CAP_PROP_FRAME_COUNT=7,
        CAP_PROP_FPS=5,
        CAP_PROP_POS_MSEC=0,
        COLOR_BGR2BGRA=123,
        VideoCapture=lambda path, backend, options: capture,
        resize=lambda frame, size: frame,
        cvtColor=lambda frame, conversion: Pixels(),
    )
    monkeypatch.setitem(sys.modules, "cv2", cv2)
    assert decoder.decode("private-snapshot.mp4") == (0.5, b"frame-pixels")
    assert capture.seek_positions == [8000, 0]
    assert capture.released


def test_unreadable_fragment_never_fabricates_a_frame_or_timestamp(monkeypatch):
    class Capture:
        def isOpened(self):
            return True

        def get(self, key):
            return {3: 320, 4: 240, 7: 150, 5: 15}[key]

        def set(self, key, value):
            return False

        def read(self):
            return False, None

        def release(self):
            pass

    cv2 = SimpleNamespace(
        CAP_FFMPEG=1900,
        CAP_PROP_OPEN_TIMEOUT_MSEC=53,
        CAP_PROP_READ_TIMEOUT_MSEC=54,
        CAP_PROP_FRAME_WIDTH=3,
        CAP_PROP_FRAME_HEIGHT=4,
        CAP_PROP_FRAME_COUNT=7,
        CAP_PROP_FPS=5,
        CAP_PROP_POS_MSEC=0,
        VideoCapture=lambda path, backend, options: Capture(),
    )
    monkeypatch.setitem(sys.modules, "cv2", cv2)
    assert decoder.decode("unreadable-snapshot.mp4") is None
