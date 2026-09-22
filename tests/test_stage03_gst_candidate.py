import pytest

from k5vision import stage03_gst_candidate
from k5vision.media.native_rtsp_pipeline import (
    NativeRtspPipelineError,
    NativeRtspPipelineUnavailable,
)


class FakePipeline:
    terminal = "eos"
    descriptions: list[str] = []

    def __init__(self, description: str, *, startup_probe_seconds: float) -> None:
        self.descriptions.append(description)
        self.startup_probe_seconds = startup_probe_seconds
        self.close_calls: list[float] = []

    def wait_for_terminal(self, *, timeout_seconds: float) -> str:
        assert timeout_seconds > 0
        return self.terminal

    def close(self, *, timeout_seconds: float = 1.0) -> None:
        self.close_calls.append(timeout_seconds)


def test_build_gst_pipeline_keeps_source_in_process_only() -> None:
    source = "rtsp://user:secret@example/live"

    description = stage03_gst_candidate.build_gst_pipeline("tcp", source)

    assert source in description
    assert "protocols=tcp" in description
    assert "tcp-timeout=5000000" in description
    assert "teardown-timeout=0" in description
    assert "decodebin" not in description
    assert "application/x-rtp,media=video" not in description
    assert "identity" in description
    assert "eos-after=60" in description
    assert "fakesink" in description


def test_build_gst_pipeline_rejects_invalid_transport_and_empty_source() -> None:
    with pytest.raises(ValueError):
        stage03_gst_candidate.build_gst_pipeline("invalid", "rtsp://example/live")

    with pytest.raises(ValueError):
        stage03_gst_candidate.build_gst_pipeline("tcp", " ")


def test_run_candidate_uses_native_pipeline_without_child_argv(monkeypatch) -> None:
    FakePipeline.terminal = "eos"
    FakePipeline.descriptions = []
    monkeypatch.setattr(stage03_gst_candidate, "NativeRtspPipeline", FakePipeline)

    assert stage03_gst_candidate.run_candidate("udp", "rtsp://example/live") == 0
    assert len(FakePipeline.descriptions) == 1
    assert "protocols=udp" in FakePipeline.descriptions[0]


def test_run_candidate_returns_sanitized_missing_runtime_status(monkeypatch) -> None:
    def unavailable(*args, **kwargs):
        raise NativeRtspPipelineUnavailable("rtsp://user:secret@example/live")

    monkeypatch.setattr(stage03_gst_candidate, "NativeRtspPipeline", unavailable)

    assert stage03_gst_candidate.run_candidate("tcp", "rtsp://user:secret@example/live") == 127


def test_run_candidate_maps_terminal_failure_without_raw_diagnostics(monkeypatch) -> None:
    FakePipeline.terminal = "error"
    FakePipeline.descriptions = []
    monkeypatch.setattr(stage03_gst_candidate, "NativeRtspPipeline", FakePipeline)

    result = stage03_gst_candidate.run_candidate(
        "tcp",
        "rtsp://user:secret@example/live",
    )

    assert result == 46
    assert isinstance(result, int)


def test_run_candidate_maps_timeout_to_connect_failure(monkeypatch) -> None:
    FakePipeline.terminal = "timeout"
    monkeypatch.setattr(stage03_gst_candidate, "NativeRtspPipeline", FakePipeline)

    assert stage03_gst_candidate.run_candidate("tcp", "rtsp://example/live") == 42


def test_run_candidate_maps_native_failure_without_exception_text(monkeypatch) -> None:
    def failed(*args, **kwargs):
        raise NativeRtspPipelineError("rtsp://user:secret@example/live")

    monkeypatch.setattr(stage03_gst_candidate, "NativeRtspPipeline", failed)

    assert stage03_gst_candidate.run_candidate("tcp", "rtsp://user:secret@example/live") == 46


@pytest.mark.parametrize(
    ("stderr", "expected"),
    [
        ("RTSP error: 401 Unauthorized for rtsp://user:secret@example/live", 41),
        ("RTSP error: 403 Forbidden", 41),
        ("failed to connect: connection refused", 42),
        ("streaming stopped, reason not-negotiated", 43),
        ("streaming stopped, reason not-linked (-1)", 43),
        ("Internal data stream error", 43),
        ("failed delayed linking some pad of GstRTSPSrc", 43),
        ("RTSP error: 404 Not Found", 44),
        ("WARNING: erroneous pipeline: no property foo", 45),
        ("opaque GStreamer failure rtsp://user:secret@example/live", 46),
    ],
)
def test_classify_gst_failure_returns_only_coarse_status(stderr: str, expected: int) -> None:
    assert stage03_gst_candidate.classify_gst_failure(stderr) == expected
