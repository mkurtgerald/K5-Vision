import subprocess

import pytest

from k5vision import stage03_gst_candidate


def test_build_gst_argv_keeps_source_in_one_shell_free_argument() -> None:
    source = "rtsp://user:secret@example/live"

    argv = stage03_gst_candidate.build_gst_argv("tcp", source)

    assert argv[0] == "gst-launch-1.0"
    assert f"location={source}" in argv
    assert "protocols=tcp" in argv
    assert argv.count("!") >= 1


def test_build_gst_argv_rejects_invalid_transport_and_empty_source() -> None:
    with pytest.raises(ValueError):
        stage03_gst_candidate.build_gst_argv("invalid", "rtsp://example/live")

    with pytest.raises(ValueError):
        stage03_gst_candidate.build_gst_argv("tcp", " ")


def test_run_candidate_suppresses_child_output_and_never_uses_shell(monkeypatch) -> None:
    captured: dict[str, object] = {}

    monkeypatch.setattr(stage03_gst_candidate.shutil, "which", lambda _: "gst-launch-1.0")

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured.update(kwargs)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(stage03_gst_candidate.subprocess, "run", fake_run)

    assert stage03_gst_candidate.run_candidate("udp", "rtsp://example/live") == 0
    assert captured["shell"] is False
    assert captured["stdout"] is subprocess.DEVNULL
    assert captured["stderr"] is subprocess.DEVNULL
    assert "protocols=udp" in captured["argv"]


def test_run_candidate_returns_sanitized_missing_runtime_status(monkeypatch) -> None:
    monkeypatch.setattr(stage03_gst_candidate.shutil, "which", lambda _: None)

    assert stage03_gst_candidate.run_candidate("tcp", "rtsp://user:secret@example/live") == 127
