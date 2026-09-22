from __future__ import annotations

import asyncio

import pytest

from k5vision.media.gstreamer_udp import GStreamerUdpRuntime, build_udp_pipeline


class FakePipeline:
    def __init__(self) -> None:
        self.close_calls: list[float] = []

    def close(self, *, timeout_seconds: float = 1.0) -> None:
        self.close_calls.append(timeout_seconds)


def run(coro):  # type: ignore[no-untyped-def]
    return asyncio.run(coro)


def test_build_udp_pipeline_uses_only_reviewed_surface() -> None:
    source = "rtsp://username:password@example.invalid/stream?token=secret"
    description = build_udp_pipeline(source)

    assert "protocols=udp" in description
    assert "rtspsrc" in description
    assert "queue" in description
    assert "fakesink" in description
    assert "identity" not in description
    assert source in description


def test_build_udp_pipeline_rejects_empty_or_control_inputs() -> None:
    with pytest.raises(ValueError):
        build_udp_pipeline("   ")
    with pytest.raises(ValueError):
        build_udp_pipeline("rtsp://example.invalid/stream\nsecret")


def test_runtime_validates_time_bounds() -> None:
    with pytest.raises(ValueError):
        GStreamerUdpRuntime(startup_probe_seconds=0)
    with pytest.raises(ValueError):
        GStreamerUdpRuntime(stop_timeout_seconds=0)
    with pytest.raises(ValueError):
        GStreamerUdpRuntime(executable_name="")


def test_runtime_declares_pinned_review_surface() -> None:
    assert GStreamerUdpRuntime.reviewed_version == "1.28.7"
    assert GStreamerUdpRuntime.reviewed_elements == frozenset({"rtspsrc", "queue", "fakesink"})


def test_start_and_stop_keep_private_source_out_of_child_argv() -> None:
    source = "rtsp://username:password@example.invalid/stream?token=secret"
    pipelines: list[tuple[str, float, FakePipeline]] = []

    def factory(description: str, probe_seconds: float) -> FakePipeline:
        pipeline = FakePipeline()
        pipelines.append((description, probe_seconds, pipeline))
        return pipeline

    async def scenario() -> None:
        runtime = GStreamerUdpRuntime(
            startup_probe_seconds=0.01,
            stop_timeout_seconds=0.25,
            pipeline_factory=factory,
        )
        await runtime.start(source)
        await runtime.start(source)
        await runtime.stop()
        await runtime.stop()
        await runtime.close()

    run(scenario())

    assert len(pipelines) == 1
    description, probe_seconds, pipeline = pipelines[0]
    assert source in description
    assert probe_seconds == 0.01
    assert pipeline.close_calls == [0.25]


def test_start_failure_is_sanitized() -> None:
    source = "rtsp://username:password@example.invalid/stream?token=secret"

    def factory(description: str, probe_seconds: float) -> FakePipeline:
        raise RuntimeError(f"failed {description} after {probe_seconds}")

    async def scenario() -> None:
        runtime = GStreamerUdpRuntime(pipeline_factory=factory)
        with pytest.raises(RuntimeError) as caught:
            await runtime.start(source)
        message = str(caught.value)
        assert "username" not in message
        assert "password" not in message
        assert "token" not in message

    run(scenario())
