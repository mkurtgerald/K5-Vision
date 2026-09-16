import subprocess

import pytest

from k5vision import stage03_gst_candidate


def test_build_gst_argv_keeps_source_in_one_shell_free_argument() -> None:
    source = "rtsp://user:secret@example/live"

    argv = stage03_gst_candidate.build_gst_argv("tcp", source)

    assert argv[0] == "gst-launch-1.0"
    assert f"location={source}" in argv
    assert "protocols=tcp" in argv
    assert "tcp-timeout=5000000" in argv
    assert "teardown-timeout=0" in argv
    assert "decodebin" not in argv
    assert "application/x-rtp,media=video" not in argv
    assert "identity" in argv
    assert "eos-after=60" in argv
    assert "fakesink" in argv
    assert argv.count("!") >= 1


def test_build_gst_argv_rejects_invalid_transport_and_empty_source() -> None:
    with pytest.raises(ValueError):
        stage03_gst_candidate.build_gst_argv("invalid", "rtsp://example/live")

    with pytest.raises(ValueError):
        stage03_gst_candidate.build_gst_argv("tcp", " ")


def test_run_candidate_captures_child_error_privately_and_never_uses_shell(monkeypatch) -> None:
    captured: dict[str, object] = {}
    resolved = r"C:\\GStreamer\\bin\\gst-launch-1.0.exe"

    monkeypatch.setattr(stage03_gst_candidate.shutil, "which", lambda _: resolved)

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, stdout=None, stderr=b"")

    monkeypatch.setattr(stage03_gst_candidate.subprocess, "run", fake_run)

    assert stage03_gst_candidate.run_candidate("udp", "rtsp://example/live") == 0
    assert captured["shell"] is False
    assert captured["stdout"] is subprocess.DEVNULL
    assert captured["stderr"] is subprocess.PIPE
    assert captured["argv"][0] == resolved
    assert "protocols=udp" in captured["argv"]


def test_run_candidate_returns_sanitized_missing_runtime_status(monkeypatch) -> None:
    monkeypatch.setattr(stage03_gst_candidate.shutil, "which", lambda _: None)

    assert stage03_gst_candidate.run_candidate("tcp", "rtsp://user:secret@example/live") == 127


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


def test_run_candidate_never_returns_raw_diagnostic_content(monkeypatch) -> None:
    source = "rtsp://user:secret@example/live"
    monkeypatch.setattr(
        stage03_gst_candidate.shutil,
        "which",
        lambda _: r"C:\\GStreamer\\bin\\gst-launch-1.0.exe",
    )
    monkeypatch.setattr(
        stage03_gst_candidate.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv,
            1,
            stdout=None,
            stderr=f"401 Unauthorized while opening {source}".encode(),
        ),
    )

    result = stage03_gst_candidate.run_candidate("tcp", source)

    assert result == 41
    assert isinstance(result, int)


def test_run_candidate_maps_wrapper_failures_without_exception_text(monkeypatch) -> None:
    monkeypatch.setattr(
        stage03_gst_candidate.shutil,
        "which",
        lambda _: r"C:\\GStreamer\\bin\\gst-launch-1.0.exe",
    )
    monkeypatch.setattr(
        stage03_gst_candidate.subprocess,
        "run",
        lambda argv, **kwargs: (_ for _ in ()).throw(OSError("rtsp://user:secret@example/live")),
    )

    assert stage03_gst_candidate.run_candidate("tcp", "rtsp://user:secret@example/live") == 47
