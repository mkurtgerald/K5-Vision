"""The recorded-person worker decodes growing private MP4 snapshots without false UTC."""

from __future__ import annotations

import io
import json
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


def test_fragment_decoder_reopens_unchanged_snapshot_after_unusable_rewind(monkeypatch):
    class Pixels:
        def tobytes(self, *, order):
            assert order == "C"
            return b"frame-pixels"

    captures = []

    class Capture:
        def __init__(self):
            self.index = len(captures)
            self.released = False
            self.seek_positions = []
            self.reads = 0
            captures.append(self)

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
                return 750
            raise AssertionError("Unexpected video property")

        def set(self, key, value):
            assert key == 0
            self.seek_positions.append(value)
            return False

        def read(self):
            self.reads += 1
            if self.index == 1 and self.reads == 1:
                return True, object()
            return False, None

        def release(self):
            self.released = True

    def open_capture(path, backend, options):
        assert path == "private-snapshot.mp4"
        assert backend == 1900
        assert options == [53, 1500, 54, 1500]
        return Capture()

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
        VideoCapture=open_capture,
        resize=lambda frame, size: frame,
        cvtColor=lambda frame, conversion: Pixels(),
    )
    monkeypatch.setitem(sys.modules, "cv2", cv2)
    assert decoder.decode("private-snapshot.mp4") == (0.75, b"frame-pixels")
    assert len(captures) == 2
    assert captures[0].seek_positions == [8000, 0]
    assert captures[1].seek_positions == []
    assert all(capture.released for capture in captures)


def test_decoder_emits_only_fixed_failure_phase_without_paths(monkeypatch):
    request = json.dumps({"path": "private-recording.mp4", "artifact_root": None}).encode()
    output = io.BytesIO()
    monkeypatch.setattr(decoder.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(request + b"\n")))
    monkeypatch.setattr(decoder.sys, "stdout", SimpleNamespace(buffer=output))

    def invalid_recording(_path):
        raise RuntimeError("rtsp://private-recording-credential")

    monkeypatch.setattr(decoder, "decode", invalid_recording)
    assert decoder.main() == 1
    assert output.getvalue() == (
        b'{"status":"phase","phase":"decode"}\n{"status": "failed", "phase": "decode"}\n'
    )


def test_decoder_progress_reports_only_fixed_stage_and_never_source(monkeypatch):
    request = json.dumps({"path": "private-recording.mp4", "artifact_root": None}).encode()
    output = io.BytesIO()
    monkeypatch.setattr(decoder.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(request + b"\n")))
    monkeypatch.setattr(decoder.sys, "stdout", SimpleNamespace(buffer=output))
    monkeypatch.setattr(decoder, "decode", lambda path: None)
    assert decoder.main() == 0
    assert output.getvalue() == (b'{"status":"phase","phase":"decode"}\n{"status":"empty"}\n')
    assert b"private-recording" not in output.getvalue()


def test_person_model_can_prime_without_decoding_or_retaining_camera_media(monkeypatch):
    from k5vision import operator_analytics_person as person

    initialized = []
    warmed = []

    class Detector:
        def infer(self, pixels, width, height, stride, elapsed):
            assert type(pixels) is bytes and len(pixels) == 544 * 320 * 4
            assert not any(pixels)
            assert (width, height, stride, elapsed) == (544, 320, 544 * 4, 0)
            warmed.append(True)
            return (object(),)

    monkeypatch.setattr(person, "OperatorPersonConfiguration", lambda root: root)
    monkeypatch.setattr(
        person, "_PersonDetector", lambda config: initialized.append(config) or Detector()
    )
    request = json.dumps({"path": "", "artifact_root": "private-model"}).encode()
    output = io.BytesIO()
    monkeypatch.setattr(decoder.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(request + b"\n")))
    monkeypatch.setattr(decoder.sys, "stdout", SimpleNamespace(buffer=output))
    assert decoder.main() == 0
    assert len(initialized) == 1 and warmed == [True]
    assert output.getvalue() == (
        b'{"status":"phase","phase":"model"}\n'
        b'{"status":"phase","phase":"inference"}\n'
        b'{"status":"primed"}\n'
    )
    assert b"private-model" not in output.getvalue()


def test_person_model_prime_cannot_run_without_an_admitted_configuration(monkeypatch):
    request = json.dumps({"path": "", "artifact_root": None}).encode()
    output = io.BytesIO()
    monkeypatch.setattr(decoder.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(request + b"\n")))
    monkeypatch.setattr(decoder.sys, "stdout", SimpleNamespace(buffer=output))
    assert decoder.main() == 1
    assert output.getvalue() == b""


def test_model_prime_failure_reports_finite_stage_without_native_details(monkeypatch):
    from k5vision import operator_analytics_person as person

    def broken_model(_config):
        raise RuntimeError("rtsp://private-source-with-credentials")

    monkeypatch.setattr(person, "OperatorPersonConfiguration", lambda root: root)
    monkeypatch.setattr(person, "_PersonDetector", broken_model)
    request = json.dumps({"path": "", "artifact_root": "private-model"}).encode()
    output = io.BytesIO()
    monkeypatch.setattr(decoder.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(request + b"\n")))
    monkeypatch.setattr(decoder.sys, "stdout", SimpleNamespace(buffer=output))
    assert decoder.main() == 1
    assert output.getvalue() == (
        b'{"status":"phase","phase":"model"}\n{"status": "failed", "phase": "model"}\n'
    )
    assert b"private-model" not in output.getvalue()
    assert b"private-source" not in output.getvalue()
