"""Hosted, generated-fixture regressions for the opt-in RTSP joined witness."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import subprocess
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
    monkeypatch.setattr(module, "_wait_for_loopback_listener", lambda _port: None)
    monkeypatch.setattr(module, "_stop_owned_process", stopped.append)
    monkeypatch.setattr(module, "time", SimpleNamespace(sleep=lambda _seconds: None))
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
            **kwargs, backend_factory=lambda *_args: Backend()
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
