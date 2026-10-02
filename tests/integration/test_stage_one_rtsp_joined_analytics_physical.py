"""Physical Stage-One witness joining real RTSP/TCP ingest to analytics and Windows overlay.

This opt-in witness republishes only the rights-reviewed Analytics-lab validation clip
through a loopback-only MediaMTX endpoint. Frames remain transient: no screenshots,
recordings, source URI, paths, credentials, or frame payloads are retained.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import time
from pathlib import Path

import pytest

from k5vision.media.gstreamer_direct_frame_delivery import GStreamerDirectFrameDelivery
from k5vision.operator_launch import ResolvedLiveSource
from k5vision.operator_runtime import WindowsSingleLiveOperatorLauncher
from tests.integration.test_stage_one_joined_analytics_physical import (
    _JoinedAnalyticsProvider,
    _source_free_evidence,
)

pytestmark = pytest.mark.skipif(
    os.getenv("K5_STAGE_ONE_RTSP_JOINED_ANALYTICS_PHYSICAL") != "1",
    reason="Stage One RTSP-joined analytics/Windows qualification is opt-in",
)

_MEDIA_MTX_VERSION = "1.21.1"


def _reviewed_video_path(evidence_root: Path) -> Path:
    manifest_path = (evidence_root / "validation-manifest.json").resolve(strict=True)
    manifest_path.relative_to(evidence_root)
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    samples = document.get("samples")
    if not isinstance(samples, list) or not samples or not isinstance(samples[0], dict):
        raise ValueError("reviewed validation sample is unavailable")
    relative_video = samples[0].get("video_path")
    if not isinstance(relative_video, str) or not relative_video:
        raise ValueError("reviewed validation video is unavailable")
    video = (evidence_root / relative_video).resolve(strict=True)
    video.relative_to(evidence_root)
    return video


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_for_loopback_listener(port: int, *, timeout_seconds: float = 8.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("loopback RTSP server did not become ready")


def _stop_owned_process(process: subprocess.Popen[bytes] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def test_rtsp_detector_tracker_overlay_reaches_windows_operator(tmp_path: Path) -> None:
    output_value = os.getenv("K5_STAGE_ONE_RTSP_JOINED_ANALYTICS_OUTPUT")
    assert output_value
    output = Path(output_value)
    output.unlink(missing_ok=True)

    root_value = os.getenv("K5_ANALYTICS_EVIDENCE_ROOT")
    assert root_value
    evidence_root = Path(root_value).resolve(strict=True)
    video_path = _reviewed_video_path(evidence_root)

    gstreamer_root_value = os.getenv("K5_GSTREAMER_ROOT")
    assert gstreamer_root_value
    gstreamer_root = Path(gstreamer_root_value).resolve(strict=True)
    gst_launch = gstreamer_root / "bin" / "gst-launch-1.0.exe"
    if not gst_launch.is_file():
        raise RuntimeError("reviewed GStreamer launcher is unavailable")

    local_app_data_value = os.getenv("LOCALAPPDATA")
    assert local_app_data_value
    mediamtx = (
        Path(local_app_data_value)
        / "K5RunnerTools"
        / "mediamtx"
        / _MEDIA_MTX_VERSION
        / "mediamtx.exe"
    )
    if not mediamtx.is_file():
        raise RuntimeError("pinned MediaMTX runtime is unavailable")

    port = _free_loopback_port()
    config_path = tmp_path / "mediamtx.yml"
    config_path.write_text(
        "\n".join(
            (
                "logLevel: warn",
                "api: false",
                "metrics: false",
                "pprof: false",
                "playback: false",
                "rtsp: true",
                "rtspTransports: [tcp]",
                f"rtspAddress: 127.0.0.1:{port}",
                "rtmp: false",
                "hls: false",
                "webrtc: false",
                "srt: false",
                "moq: false",
                "paths:",
                "  k5reviewed:",
                "",
            )
        ),
        encoding="utf-8",
    )

    source_uri = f"rtsp://127.0.0.1:{port}/k5reviewed"
    server: subprocess.Popen[bytes] | None = None
    publisher: subprocess.Popen[bytes] | None = None
    provider = _JoinedAnalyticsProvider(evidence_root)
    try:
        server = subprocess.Popen(
            [str(mediamtx), str(config_path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _wait_for_loopback_listener(port)
        if server.poll() is not None:
            raise RuntimeError("loopback RTSP server exited during startup")

        publisher = subprocess.Popen(
            [
                str(gst_launch),
                "-q",
                "filesrc",
                f"location={video_path.as_posix()}",
                "!",
                "decodebin",
                "!",
                "videoconvert",
                "!",
                "videorate",
                "!",
                "video/x-raw,format=I420,framerate=15/1",
                "!",
                "x264enc",
                "speed-preset=ultrafast",
                "tune=zerolatency",
                "bitrate=2000",
                "key-int-max=30",
                "!",
                "video/x-h264,profile=baseline",
                "!",
                "h264parse",
                "config-interval=1",
                "!",
                "rtspclientsink",
                "protocols=tcp",
                f"location={source_uri}",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(0.75)
        if publisher.poll() is not None or server.poll() is not None:
            raise RuntimeError("loopback reviewed-video RTSP publisher failed to remain available")

        def delivery_factory(payload_type: int) -> GStreamerDirectFrameDelivery:
            assert 96 <= payload_type <= 127
            return GStreamerDirectFrameDelivery(
                # At 15 fps, eight frames can end before an in-budget detector
                # finishes. Keep later frames available for the real overlay.
                frame_goal=60,
                delivery_timeout_seconds=60.0,
                consumer_timeout_seconds=10.0,
                pull_poll_ms=100,
                startup_probe_ms=5_000,
            )

        launcher = WindowsSingleLiveOperatorLauncher(
            delivery_factory=delivery_factory,
            detection_provider=provider,
        )
        metrics = asyncio.run(
            launcher.run(
                ResolvedLiveSource(source_uri, 96),
                width=1280,
                height=720,
            )
        )
    finally:
        try:
            _stop_owned_process(publisher)
        finally:
            _stop_owned_process(server)

    evidence = _source_free_evidence(
        metrics,
        provider_calls=provider.provider_calls,
        tracked_detections=provider.tracked_detections,
        revision=os.getenv("K5_STAGE_ONE_REVISION", ""),
        analytics_revision=os.getenv("ANALYTICS_LAB_SHA", ""),
    )
    evidence["execution_context"] = "reviewed-video-loopback-rtsp-windows-x64"
    evidence["rtsp_tcp_joined"] = True
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
