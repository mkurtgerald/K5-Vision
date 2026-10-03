"""Hosted, generated-fixture regressions for the opt-in RTSP joined witness."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from k5vision.media.gstreamer_direct_frame_delivery import GStreamerDirectFrameDelivery
from k5vision.media.presentation_decoder import _PresentationPayload
from k5vision.media.windows_operator_runtime import WindowsOperatorRuntimeState
from k5vision.operator_runtime import WindowsSingleLiveOperatorLauncher

_TEST = "tests/integration/test_stage_one_rtsp_joined_analytics_physical.py"
_REVISION = "a" * 40
_ANALYTICS_REVISION = "b" * 40


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("rtsp_joined_witness", _TEST)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _metrics(**changes: object) -> SimpleNamespace:
    return SimpleNamespace(
        **{
            "delivered_frames": 8,
            "presentations": 8,
            "analytics_enabled": True,
            "analytics_provider_submissions": 2,
            "analytics_provider_completions": 1,
            "analytics_rendered_boxes": 1,
            "analytics_failures": 0,
            **changes,
        }
    )


def test_rtsp_witness_collects_with_bare_pytest_without_pythonpath() -> None:
    """Match CI's console entry point, which does not add the checkout to sys.path."""
    pytest_executable = Path(sys.executable).with_name(
        "pytest.exe" if os.name == "nt" else "pytest"
    )
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    result = subprocess.run(
        [
            str(pytest_executable),
            _TEST,
            "--collect-only",
            "--no-cov",
            "-o",
            "addopts=",
            "-q",
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 test collected" in result.stdout


@pytest.fixture
def witness(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SimpleNamespace:
    module = _module()
    output = tmp_path / "receipt.json"
    output.write_text("stale receipt", encoding="utf-8")
    video = tmp_path / "generated-fixture.bin"
    video.write_bytes(b"generated fixture; never decoded by hosted tests")
    (tmp_path / "validation-manifest.json").write_text(
        json.dumps({"samples": [{"video_path": video.name}]}), encoding="utf-8"
    )
    gst = tmp_path / "gstreamer"
    (gst / "bin").mkdir(parents=True)
    (gst / "bin" / "gst-launch-1.0.exe").touch()
    mtx = tmp_path / "K5RunnerTools" / "mediamtx" / module._MEDIA_MTX_VERSION
    mtx.mkdir(parents=True)
    (mtx / "mediamtx.exe").touch()
    for name, value in {
        "K5_STAGE_ONE_RTSP_JOINED_ANALYTICS_OUTPUT": str(output),
        "K5_ANALYTICS_EVIDENCE_ROOT": str(tmp_path),
        "K5_GSTREAMER_ROOT": str(gst),
        "LOCALAPPDATA": str(tmp_path),
        "K5_STAGE_ONE_REVISION": _REVISION,
        "ANALYTICS_LAB_SHA": _ANALYTICS_REVISION,
    }.items():
        monkeypatch.setenv(name, value)

    processes: list[SimpleNamespace] = []
    stopped: list[object] = []

    def popen(command: list[str], **options: object) -> SimpleNamespace:
        assert options == dict.fromkeys(("stdin", "stdout", "stderr"), subprocess.DEVNULL)
        process = SimpleNamespace(command=command, poll=lambda: None)
        processes.append(process)
        return process

    monkeypatch.setattr(module.subprocess, "Popen", popen)
    monkeypatch.setattr(module, "_free_loopback_port", lambda: 43123)
    monkeypatch.setattr(module, "_wait_for_loopback_listener", lambda _port, **_kw: None)
    monkeypatch.setattr(module, "_wait_for_reviewed_rtsp_publication", lambda *_a, **_kw: None)
    monkeypatch.setattr(module, "_stop_owned_process", stopped.append)
    monkeypatch.setattr(
        module, "time", SimpleNamespace(sleep=lambda _seconds: None, monotonic=lambda: 0.0)
    )
    provider = SimpleNamespace(provider_calls=2, tracked_detections=1)
    monkeypatch.setattr(module, "_JoinedAnalyticsProvider", lambda _root: provider)

    class Launcher:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def run(self, *_args: object, **_kwargs: object) -> SimpleNamespace:
            return _metrics()

    monkeypatch.setattr(module, "WindowsSingleLiveOperatorLauncher", Launcher)
    return SimpleNamespace(
        module=module, output=output, root=tmp_path, processes=processes, stopped=stopped
    )


@pytest.mark.parametrize("value, skipped", [("", True), ("0", True), ("true", True), ("1", False)])
def test_rtsp_joined_witness_requires_its_own_opt_in(
    monkeypatch: pytest.MonkeyPatch, value: str, skipped: bool
) -> None:
    monkeypatch.setenv("K5_STAGE_ONE_RTSP_JOINED_ANALYTICS_PHYSICAL", value)
    assert _module().pytestmark.args == (skipped,)


@pytest.mark.parametrize("accepted", [True, False])
def test_rtsp_receipt_requires_current_rendered_boxes(
    monkeypatch: pytest.MonkeyPatch, witness: SimpleNamespace, accepted: bool
) -> None:
    class Launcher:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def run(self, *_args: object, **_kwargs: object) -> SimpleNamespace:
            return _metrics(analytics_rendered_boxes=int(accepted))

    monkeypatch.setattr(witness.module, "WindowsSingleLiveOperatorLauncher", Launcher)
    if not accepted:
        with pytest.raises(ValueError, match="positive bounded counters"):
            witness.module.test_rtsp_detector_tracker_overlay_reaches_windows_operator(witness.root)
        assert not witness.output.exists()
        return
    witness.module.test_rtsp_detector_tracker_overlay_reaches_windows_operator(witness.root)
    receipt = json.loads(witness.output.read_text(encoding="utf-8"))
    assert receipt["rtsp_tcp_joined"] is True
    assert receipt["execution_context"] == "reviewed-video-loopback-rtsp-windows-x64"
    assert receipt["revision"] == _REVISION
    assert receipt["analytics_revision"] == _ANALYTICS_REVISION
    assert "rtsp://" not in json.dumps(receipt)
    assert str(witness.root) not in json.dumps(receipt)
    assert witness.stopped == list(reversed(witness.processes))


def test_server_cleanup_is_attempted_after_publisher_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch, witness: SimpleNamespace
) -> None:
    def stop(process: object) -> None:
        witness.stopped.append(process)
        if process is witness.processes[1]:
            raise subprocess.TimeoutExpired("generated process", 5)

    monkeypatch.setattr(witness.module, "_stop_owned_process", stop)
    with pytest.raises(subprocess.TimeoutExpired):
        witness.module.test_rtsp_detector_tracker_overlay_reaches_windows_operator(witness.root)
    assert witness.stopped == list(reversed(witness.processes))
    assert not witness.output.exists()


def test_rtsp_sample_allows_in_budget_detector_to_reach_a_later_frame(
    monkeypatch: pytest.MonkeyPatch, witness: SimpleNamespace
) -> None:
    """Use real delivery, overlay, and launcher with generated frames at 15 fps."""
    closed: list[bool] = []
    results: list[object] = []

    class Backend:
        def pull(self, _timeout_ms: int) -> _PresentationPayload:
            time.sleep(1 / 15)
            return _PresentationPayload(
                payload=bytes(12 * 8 * 4), width=12, height=8, stride_bytes=48
            )

        def close(self) -> None:
            closed.append(True)

    class Provider:
        provider_calls = 0
        tracked_detections = 0

        async def __call__(self, _frame: object) -> tuple[object, ...]:
            self.provider_calls += 1
            # Valid within the unchanged 2-second production provider deadline,
            # but longer than the old eight-frame witness at 15 fps.
            await asyncio.sleep(1.0)
            self.tracked_detections += 1
            return (
                SimpleNamespace(
                    track_id="generated-track",
                    category="person",
                    confidence=0.95,
                    model_class_id=1,
                    box=SimpleNamespace(x_min=0.2, y_min=0.2, x_max=0.8, y_max=0.8),
                ),
            )

    class Runtime:
        async def start(self, streams: object) -> None:
            self.stream = tuple(streams)[0]

        async def wait(self) -> SimpleNamespace:
            presentations = 0

            async def consume(_frame: object) -> None:
                nonlocal presentations
                presentations += 1

            result = await self.stream.delivery.run(self.stream.source_uri, consume)
            return SimpleNamespace(
                state=WindowsOperatorRuntimeState.COMPLETE,
                delivered_frames=result.delivered_frames,
                presentations=presentations,
            )

        async def close(self) -> None:
            pass

    class Launcher(WindowsSingleLiveOperatorLauncher):
        async def run(self, *args: object, **kwargs: object) -> object:
            result = await super().run(*args, **kwargs)
            results.append(result)
            return result

    monkeypatch.setattr(witness.module, "_JoinedAnalyticsProvider", lambda _root: Provider())
    monkeypatch.setattr(
        witness.module,
        "GStreamerDirectFrameDelivery",
        lambda **kwargs: GStreamerDirectFrameDelivery(
            **{**kwargs, "backend_factory": lambda *_args: Backend()}
        ),
    )
    monkeypatch.setattr(
        witness.module,
        "WindowsSingleLiveOperatorLauncher",
        lambda **kwargs: Launcher(**kwargs, runtime_factory=lambda _layout: Runtime()),
    )
    witness.module.test_rtsp_detector_tracker_overlay_reaches_windows_operator(witness.root)
    receipt = json.loads(witness.output.read_text(encoding="utf-8"))
    assert receipt["analytics_rendered_boxes"] > 0
    assert receipt["analytics_provider_completions"] > 0
    assert receipt["analytics_failures"] == 0
    assert receipt["delivered_frames"] == 60
    assert len(results) == 1
    assert closed == [True]


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _ready_sdp() -> bytes:
    return b"v=0\r\nm=video 0 RTP/AVP 96\r\na=rtpmap:96 H264/90000\r\n"


def _response(*, status: int = 200, body: bytes | None = None) -> bytes:
    if body is None:
        body = _ready_sdp() if status == 200 else b""
    return (
        f"RTSP/1.0 {status} Generated\r\nCSeq: 1\r\nContent-Type: application/sdp\r\n"
        f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        + body
    )


class _Peer:
    def __init__(self, response: bytes, *, chunk_size: int = 1024) -> None:
        self.response = response
        self.chunk_size = chunk_size
        self.sent = b""
        self.closed = False
        self.timeouts: list[float] = []

    def __enter__(self) -> _Peer:
        return self

    def __exit__(self, *_args: object) -> None:
        self.closed = True

    def sendall(self, value: bytes) -> None:
        self.sent += value

    def settimeout(self, seconds: float) -> None:
        self.timeouts.append(seconds)

    def recv(self, maximum: int) -> bytes:
        count = min(maximum, self.chunk_size)
        result, self.response = self.response[:count], self.response[count:]
        return result


def _probe_environment(monkeypatch: pytest.MonkeyPatch, peer: _Peer) -> tuple[ModuleType, _Clock]:
    module = _module()
    clock = _Clock()
    monkeypatch.setattr(module, "time", clock)

    def connect(address: tuple[str, int], *, timeout: float) -> _Peer:
        assert address == ("127.0.0.1", 43123)
        assert 0 < timeout <= 0.25
        return peer

    monkeypatch.setattr(module, "socket", SimpleNamespace(create_connection=connect))
    return module, clock


@pytest.mark.parametrize("chunk_size", [1, 7, 1024])
def test_publication_probe_requires_bounded_h264_sdp_and_closes_socket(
    monkeypatch: pytest.MonkeyPatch, chunk_size: int
) -> None:
    peer = _Peer(_response(), chunk_size=chunk_size)
    module, _clock = _probe_environment(monkeypatch, peer)
    assert module._probe_reviewed_rtsp_publication(43123, deadline=8.0)
    assert peer.closed
    assert peer.sent == (
        b"DESCRIBE rtsp://127.0.0.1:43123/k5reviewed RTSP/1.0\r\n"
        b"CSeq: 1\r\nAccept: application/sdp\r\n\r\n"
    )
    assert all(0 < timeout <= 0.25 for timeout in peer.timeouts)


@pytest.mark.parametrize("status", [404, 503])
def test_unpublished_path_is_not_ready(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    peer = _Peer(_response(status=status))
    module, _clock = _probe_environment(monkeypatch, peer)
    assert module._probe_reviewed_rtsp_publication(43123, deadline=8.0) is False
    assert peer.closed


@pytest.mark.parametrize(
    "response",
    [
        _response(status=401),
        _response().replace(b"CSeq: 1", b"CSeq: 2"),
        _response().replace(b"CSeq: 1", b"CSeq: 1\r\nCSeq: 1"),
        _response().replace(b"application/sdp", b"text/plain"),
        _response().replace(b"Content-Length: 51", b"Content-Length: 9999"),
        _response(body=b"v=0\r\n"),
        _response(body=_ready_sdp().replace(b"H264", b"H265")),
        _response(body=_ready_sdp().replace(b"96", b"97")),
        _response(body=b"\xff"),
        b"RTSP/1.0 200 Generated\r\nX-Generated: " + b"x" * 4096,
        b"not RTSP\r\n\r\n",
        _response() + b"unframed data",
    ],
)
def test_publication_probe_rejects_malformed_or_unsupported_response_without_echo(
    monkeypatch: pytest.MonkeyPatch, response: bytes
) -> None:
    peer = _Peer(response)
    module, _clock = _probe_environment(monkeypatch, peer)
    with pytest.raises(RuntimeError) as failure:
        module._probe_reviewed_rtsp_publication(43123, deadline=8.0)
    assert "rtsp://" not in str(failure.value)
    assert "Generated" not in str(failure.value)
    assert peer.closed


def test_partial_or_trickling_publication_cannot_extend_absolute_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    peer = _Peer(_response(), chunk_size=1)
    module, clock = _probe_environment(monkeypatch, peer)
    original = peer.recv

    def trickle(maximum: int) -> bytes:
        clock.sleep(0.2)
        return original(maximum)

    monkeypatch.setattr(peer, "recv", trickle)
    with pytest.raises(TimeoutError):
        module._probe_reviewed_rtsp_publication(43123, deadline=0.5)
    assert clock.now <= 0.7
    assert peer.closed


def test_readiness_wait_covers_publication_race_beyond_old_fixed_sleep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    clock = _Clock()
    monkeypatch.setattr(module, "time", clock)
    attempts: list[float] = []

    def probe(_port: int, *, deadline: float) -> bool:
        assert deadline == 8.0
        attempts.append(clock.now)
        return clock.now >= 1.2

    monkeypatch.setattr(module, "_probe_reviewed_rtsp_publication", probe)
    running = SimpleNamespace(poll=lambda: None)
    module._wait_for_reviewed_rtsp_publication(43123, running, running, deadline=8.0)
    assert attempts[0] == 0
    assert 1.2 <= clock.now < 1.4
    assert len(attempts) > 1


@pytest.mark.parametrize("exits_after_probe", [False, True])
def test_readiness_wait_rejects_early_publisher_exit(
    monkeypatch: pytest.MonkeyPatch, exits_after_probe: bool
) -> None:
    module = _module()
    monkeypatch.setattr(module, "time", _Clock())
    server = SimpleNamespace(poll=lambda: None)
    publisher = SimpleNamespace(poll=lambda: None if exits_after_probe else 0)

    def probe(_port: int, *, deadline: float) -> bool:
        assert deadline == 8.0
        publisher.poll = lambda: 0
        return True

    monkeypatch.setattr(module, "_probe_reviewed_rtsp_publication", probe)
    with pytest.raises(RuntimeError, match="process exited before readiness"):
        module._wait_for_reviewed_rtsp_publication(43123, server, publisher, deadline=8.0)


def test_unpublished_or_unreachable_fixture_exhausts_one_fixed_startup_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    clock = _Clock()
    monkeypatch.setattr(module, "time", clock)
    attempts: list[float] = []

    def probe(_port: int, *, deadline: float) -> bool:
        assert deadline == 8.0
        attempts.append(clock.now)
        raise ConnectionError("must never be printed")

    monkeypatch.setattr(module, "_probe_reviewed_rtsp_publication", probe)
    running = SimpleNamespace(poll=lambda: None)
    with pytest.raises(RuntimeError, match="readiness deadline expired"):
        module._wait_for_reviewed_rtsp_publication(43123, running, running, deadline=8.0)
    assert clock.now == 8.0
    assert 1 < len(attempts) <= 82


def test_readiness_failure_preserves_cleanup_and_never_writes_success_receipt(
    monkeypatch: pytest.MonkeyPatch, witness: SimpleNamespace
) -> None:
    def unavailable(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("reviewed RTSP publication readiness deadline expired")

    monkeypatch.setattr(witness.module, "_wait_for_reviewed_rtsp_publication", unavailable)
    with pytest.raises(RuntimeError, match="readiness deadline expired"):
        witness.module.test_rtsp_detector_tracker_overlay_reaches_windows_operator(witness.root)
    assert witness.stopped == list(reversed(witness.processes))
    assert not witness.output.exists()


@pytest.mark.parametrize(
    "message, expected",
    [
        ("native decoder initialization failed", "native_initialization_failed"),
        ("direct live decoder pipeline could not be created", "pipeline_creation_failed"),
        ("direct live decoder sink is unavailable", "pipeline_sink_unavailable"),
        ("direct live decoder pipeline failed to start", "pipeline_start_failed"),
        ("direct live decoder pipeline failed during startup", "pipeline_startup_failed"),
        ("rtsp://secret:protected@127.0.0.1/path", "native_initialization_failure"),
    ],
)
def test_native_init_diagnostic_has_only_fixed_allowlisted_categories(
    message: str,
    expected: str,
) -> None:
    module = _module()
    error = module.NativePlaybackDecoderError(
        module.NativePlaybackDecoderErrorCode.NATIVE_FAILURE, message
    )
    assert module._decoder_init_failure_category(error) == expected
    assert (
        module._decoder_init_failure_category(OSError(message))
        == "unexpected_initialization_failure"
    )
    error = module.NativePlaybackDecoderError(
        module.NativePlaybackDecoderErrorCode.RUNTIME_UNAVAILABLE, message
    )
    assert module._decoder_init_failure_category(error) == "runtime_unavailable"


def test_native_init_diagnostic_survives_sanitized_production_boundary(
    monkeypatch: pytest.MonkeyPatch, witness: SimpleNamespace
) -> None:
    module = witness.module

    def unavailable(*_args: object) -> None:
        raise module.NativePlaybackDecoderError(
            module.NativePlaybackDecoderErrorCode.NATIVE_FAILURE,
            "direct live decoder pipeline failed during startup",
        )

    class Launcher:
        def __init__(self, *, delivery_factory: object, **_kwargs: object) -> None:
            self.delivery = delivery_factory(96)

        async def run(self, source: object, **_kwargs: object) -> None:
            await self.delivery.run(source.source_uri, lambda _frame: None)

    monkeypatch.setattr(module, "_DirectRtspFrameBackend", unavailable)
    monkeypatch.setattr(module, "WindowsSingleLiveOperatorLauncher", Launcher)
    with pytest.raises(
        RuntimeError, match="^RTSP joined decoder initialization failed: pipeline_startup_failed$"
    ):
        module.test_rtsp_detector_tracker_overlay_reaches_windows_operator(witness.root)
    assert witness.stopped == list(reversed(witness.processes))
    assert not witness.output.exists()


@pytest.mark.parametrize("connect_finishes_at", [0.49, 0.5, 0.7])
def test_connect_cannot_grant_another_timeout_beyond_startup_deadline(
    monkeypatch: pytest.MonkeyPatch, connect_finishes_at: float
) -> None:
    peer = _Peer(_response())
    module, clock = _probe_environment(monkeypatch, peer)

    def connect(_address: object, *, timeout: float) -> _Peer:
        assert timeout == 0.25
        clock.now = connect_finishes_at
        return peer

    monkeypatch.setattr(module.socket, "create_connection", connect)
    if connect_finishes_at >= 0.5:
        with pytest.raises(TimeoutError):
            module._probe_reviewed_rtsp_publication(43123, deadline=0.5)
        assert not peer.sent
    else:
        assert module._probe_reviewed_rtsp_publication(43123, deadline=0.5)
        assert all(0 < timeout <= 0.011 for timeout in peer.timeouts)
    assert peer.closed


def test_complete_sdp_arriving_at_deadline_is_not_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    peer = _Peer(_response())
    module, clock = _probe_environment(monkeypatch, peer)
    receive = peer.recv

    def late_receive(maximum: int) -> bytes:
        clock.now = 0.5
        return receive(maximum)

    monkeypatch.setattr(peer, "recv", late_receive)
    with pytest.raises(TimeoutError):
        module._probe_reviewed_rtsp_publication(43123, deadline=0.5)
    assert peer.closed


def test_process_recheck_cannot_accept_readiness_after_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    clock = _Clock()
    monkeypatch.setattr(module, "time", clock)
    running = SimpleNamespace(poll=lambda: None)

    def probe(_port: int, *, deadline: float) -> bool:
        clock.now = deadline
        return True

    monkeypatch.setattr(module, "_probe_reviewed_rtsp_publication", probe)
    with pytest.raises(RuntimeError, match="readiness deadline expired"):
        module._wait_for_reviewed_rtsp_publication(43123, running, running, deadline=8.0)
