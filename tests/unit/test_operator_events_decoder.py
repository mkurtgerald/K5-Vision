"""The recorded-person worker decodes growing private MP4 snapshots without false UTC."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
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
        person,
        "_PersonDetector",
        lambda config, *, inference_threads, fixed_view_input: (
            initialized.append((config, inference_threads, fixed_view_input)) or Detector()
        ),
    )
    request = json.dumps({"path": "", "artifact_root": "private-model"}).encode()
    output = io.BytesIO()
    monkeypatch.setattr(decoder.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(request + b"\n")))
    monkeypatch.setattr(decoder.sys, "stdout", SimpleNamespace(buffer=output))
    assert decoder.main() == 0
    assert initialized == [(Path("private-model"), 2, True)] and warmed == [True]
    assert output.getvalue() == (
        b'{"status":"phase","phase":"model"}\n'
        b'{"status":"phase","phase":"inference"}\n'
        b'{"status":"primed"}\n'
    )
    assert b"private-model" not in output.getvalue()


def test_recorded_person_uses_the_same_fixed_graph_after_model_only_prime(monkeypatch):
    """A real recorded snapshot reuses the admitted 544x320 two-thread graph."""
    from k5vision import operator_analytics_person as person

    models = []
    calls = []
    pixels = bytes(544 * 320 * 4)

    class Detector:
        def infer(self, raw, width, height, stride, elapsed):
            assert raw == pixels
            assert (width, height, stride) == (544, 320, 544 * 4)
            calls.append(elapsed)
            return (SimpleNamespace(category="person", confidence=0.81),) if elapsed else ()

    def factory(config, *, inference_threads, fixed_view_input):
        assert (config, inference_threads, fixed_view_input) == (Path("private-model"), 2, True)
        models.append(Detector())
        return models[-1]

    monkeypatch.setattr(person, "OperatorPersonConfiguration", lambda root: root)
    monkeypatch.setattr(person, "_PersonDetector", factory)
    monkeypatch.setattr(decoder, "decode", lambda path: (0.25, pixels))
    requests = (
        {"path": "", "artifact_root": "private-model"},
        {"path": "private-recording.mp4", "artifact_root": "private-model"},
    )
    incoming = b"".join(json.dumps(request).encode() + b"\n" for request in requests)
    out = io.BytesIO()
    monkeypatch.setattr(decoder.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(incoming)))
    monkeypatch.setattr(decoder.sys, "stdout", SimpleNamespace(buffer=out))
    assert decoder.main() == 0
    assert len(models) == 1
    assert calls == [0, 250]
    response = out.getvalue()
    assert response.count(b'"status":"phase","phase":"model"') == 1
    assert response.count(b'"status":"primed"') == 1
    assert b'"category": "person", "confidence": 0.81' in response
    assert response.endswith(pixels)
    assert b"private-recording" not in response and b"private-model" not in response


def test_person_model_prime_cannot_run_without_an_admitted_configuration(monkeypatch):
    request = json.dumps({"path": "", "artifact_root": None}).encode()
    output = io.BytesIO()
    monkeypatch.setattr(decoder.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(request + b"\n")))
    monkeypatch.setattr(decoder.sys, "stdout", SimpleNamespace(buffer=output))
    assert decoder.main() == 1
    assert output.getvalue() == b""


def test_model_prime_failure_reports_finite_stage_without_native_details(monkeypatch):
    from k5vision import operator_analytics_person as person

    def broken_model(_config, *, inference_threads, fixed_view_input):
        assert inference_threads == 2 and fixed_view_input is True
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
