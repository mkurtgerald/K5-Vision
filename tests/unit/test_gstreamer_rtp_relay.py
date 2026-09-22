from __future__ import annotations

import asyncio

import pytest

from k5vision.media.gstreamer_rtp_relay import (
    GStreamerRtpRelayRuntime,
    build_rtp_relay_pipeline,
)


class FakePipeline:
    def __init__(self) -> None:
        self.close_calls: list[float] = []

    def close(self, *, timeout_seconds: float = 1.0) -> None:
        self.close_calls.append(timeout_seconds)


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


def test_relay_pipeline_is_video_rtp_loopback_only() -> None:
    source = "rtsp://user:secret@192.0.2.10/live?token=a&b=c"
    description = build_rtp_relay_pipeline(source, 50000)

    assert source in description
    assert "protocols=udp" in description
    assert "application/x-rtp,media=video" in description
    assert "queue" in description
    assert "max-size-buffers=8" in description
    assert "leaky=downstream" in description
    assert "udpsink" in description
    assert "host=127.0.0.1" in description
    assert "port=50000" in description
    assert "fakesink" not in description


def test_relay_pipeline_rejects_invalid_inputs() -> None:
    with pytest.raises(ValueError):
        build_rtp_relay_pipeline("", 50000)
    with pytest.raises(ValueError):
        build_rtp_relay_pipeline("rtsp://example.invalid/live", 0)
    with pytest.raises(ValueError):
        build_rtp_relay_pipeline("rtsp://example.invalid/live", 65536)


def test_relay_runtime_uses_in_process_pipeline_and_sanitizes_failure() -> None:
    source = "rtsp://user:secret@192.0.2.10/live?token=a"
    pipelines: list[tuple[str, float, FakePipeline]] = []

    def factory(description: str, probe_seconds: float) -> FakePipeline:
        pipeline = FakePipeline()
        pipelines.append((description, probe_seconds, pipeline))
        return pipeline

    async def scenario() -> None:
        runtime = GStreamerRtpRelayRuntime(
            50000,
            startup_probe_seconds=0.02,
            stop_timeout_seconds=0.3,
            pipeline_factory=factory,
        )
        await runtime.start(source)
        await runtime.start(source)
        await runtime.close()

    run(scenario())

    assert len(pipelines) == 1
    description, probe_seconds, pipeline = pipelines[0]
    assert source in description
    assert probe_seconds == 0.02
    assert pipeline.close_calls == [0.3]

    def failing_factory(description: str, probe_seconds: float) -> FakePipeline:
        raise RuntimeError(f"failed {description} after {probe_seconds}")

    async def failed() -> None:
        runtime = GStreamerRtpRelayRuntime(50000, pipeline_factory=failing_factory)
        with pytest.raises(RuntimeError) as caught:
            await runtime.start(source)
        assert "user" not in str(caught.value)
        assert "secret" not in str(caught.value)
        assert "token" not in str(caught.value)

    run(failed())
