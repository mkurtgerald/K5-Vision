from __future__ import annotations

import asyncio
import subprocess

import pytest

from k5vision.media import gstreamer_udp
from k5vision.media.gstreamer_udp import GStreamerUdpRuntime, build_udp_argv


class FakeProcess:
    def __init__(self, returncode: int | None = None) -> None:
        self.pid = 12345
        self.returncode = returncode
        self.terminated = 0
        self.killed = 0
        self.waits = 0

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated += 1
        self.returncode = 0

    def kill(self) -> None:
        self.killed += 1
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        self.waits += 1
        if self.returncode is None:
            raise subprocess.TimeoutExpired("fake", timeout)
        return self.returncode


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


def test_build_udp_argv_uses_only_reviewed_surface() -> None:
    source = "rtsp://username:password@example.invalid/stream"
    argv = build_udp_argv("gst-launch-1.0", source)
    assert argv[0] == "gst-launch-1.0"
    assert "protocols=udp" in argv
    assert "rtspsrc" in argv
    assert "queue" in argv
    assert "fakesink" in argv
    assert all("identity" not in arg for arg in argv)
    assert source not in {"rtspsrc", "queue", "fakesink"}


def test_build_udp_argv_rejects_empty_inputs() -> None:
    with pytest.raises(ValueError):
        build_udp_argv("", "rtsp://example.invalid/stream")
    with pytest.raises(ValueError):
        build_udp_argv("gst-launch-1.0", "   ")


def test_runtime_validates_time_bounds() -> None:
    with pytest.raises(ValueError):
        GStreamerUdpRuntime(startup_probe_seconds=0)
    with pytest.raises(ValueError):
        GStreamerUdpRuntime(stop_timeout_seconds=0)


def test_runtime_declares_pinned_review_surface() -> None:
    assert GStreamerUdpRuntime.reviewed_version == "1.28.7"
    assert GStreamerUdpRuntime.reviewed_elements == frozenset({"rtspsrc", "queue", "fakesink"})


def test_start_and_stop_are_bounded_and_shell_free(monkeypatch: pytest.MonkeyPatch) -> None:
    process = FakeProcess()
    popen_calls: list[tuple[list[str], dict[str, object]]] = []
    stop_calls: list[tuple[FakeProcess, float]] = []

    monkeypatch.setattr(gstreamer_udp.shutil, "which", lambda _: "C:/k5/gst-launch-1.0.exe")
    monkeypatch.setattr(gstreamer_udp.time, "sleep", lambda _: None)

    def fake_popen(argv: list[str], **kwargs: object) -> FakeProcess:
        popen_calls.append((argv, kwargs))
        return process

    def fake_stop(target: FakeProcess, *, timeout_seconds: float) -> None:
        stop_calls.append((target, timeout_seconds))
        target.returncode = 0

    monkeypatch.setattr(gstreamer_udp.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(gstreamer_udp, "stop_process_tree", fake_stop)

    async def scenario() -> None:
        runtime = GStreamerUdpRuntime(startup_probe_seconds=0.01, stop_timeout_seconds=0.25)
        await runtime.start("rtsp://example.invalid/stream")
        await runtime.start("rtsp://example.invalid/stream")
        await runtime.stop()
        await runtime.stop()
        await runtime.close()

    run(scenario())

    assert len(popen_calls) == 1
    argv, kwargs = popen_calls[0]
    assert argv[0] == "C:/k5/gst-launch-1.0.exe"
    assert kwargs["shell"] is False
    assert kwargs["stdout"] == subprocess.DEVNULL
    assert kwargs["stderr"] == subprocess.DEVNULL
    assert stop_calls == [(process, 0.25)]


def test_start_fails_safely_when_runtime_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gstreamer_udp.shutil, "which", lambda _: None)

    async def scenario() -> None:
        runtime = GStreamerUdpRuntime()
        with pytest.raises(RuntimeError) as caught:
            await runtime.start("rtsp://username:password@example.invalid/stream")
        message = str(caught.value)
        assert "username" not in message
        assert "password" not in message

    run(scenario())


def test_start_fails_safely_on_early_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    process = FakeProcess(returncode=17)
    monkeypatch.setattr(gstreamer_udp.shutil, "which", lambda _: "gst-launch-1.0")
    monkeypatch.setattr(gstreamer_udp.time, "sleep", lambda _: None)
    monkeypatch.setattr(gstreamer_udp.subprocess, "Popen", lambda *args, **kwargs: process)

    async def scenario() -> None:
        runtime = GStreamerUdpRuntime(startup_probe_seconds=0.01)
        with pytest.raises(RuntimeError) as caught:
            await runtime.start("rtsp://username:password@example.invalid/stream")
        assert "username" not in str(caught.value)
        assert "password" not in str(caught.value)

    run(scenario())
