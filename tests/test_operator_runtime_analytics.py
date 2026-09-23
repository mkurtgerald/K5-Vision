from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import k5vision.operator_runtime as operator_runtime_module
from k5vision.media.analytics_overlay_delivery import BoundedAnalyticsOverlayDelivery
from k5vision.media.presentation_frame import PresentationVideoFrame
from k5vision.media.windows_operator_runtime import (
    WindowsOperatorRuntimeSnapshot,
    WindowsOperatorRuntimeState,
)
from k5vision.operator_launch import ResolvedLiveSource
from k5vision.operator_runtime import (
    PrivateStageOneSourceResolver,
    WindowsSingleLiveOperatorLauncher,
    build_environment_operator_runtime,
)


class _BaseDelivery:
    async def run(self, _source_uri: str, _consumer: object) -> object:
        raise AssertionError("fake Windows runtime owns execution")


class _CapturingRuntime:
    def __init__(self) -> None:
        self.delivery: object | None = None
        self.closed = False
        self._snapshot = WindowsOperatorRuntimeSnapshot(
            state=WindowsOperatorRuntimeState.READY,
            viewport_count=1,
            open_surface_count=0,
            stream_count=0,
            delivered_frames=0,
            presentations=0,
        )

    @property
    def snapshot(self) -> WindowsOperatorRuntimeSnapshot:
        return self._snapshot

    async def start(self, streams: object) -> WindowsOperatorRuntimeSnapshot:
        selected = tuple(streams)  # type: ignore[arg-type]
        self.delivery = selected[0].delivery
        self._snapshot = WindowsOperatorRuntimeSnapshot(
            state=WindowsOperatorRuntimeState.RUNNING,
            viewport_count=1,
            open_surface_count=1,
            stream_count=1,
            delivered_frames=0,
            presentations=0,
        )
        return self._snapshot

    async def wait(self) -> WindowsOperatorRuntimeSnapshot:
        self._snapshot = WindowsOperatorRuntimeSnapshot(
            state=WindowsOperatorRuntimeState.COMPLETE,
            viewport_count=1,
            open_surface_count=1,
            stream_count=1,
            delivered_frames=3,
            presentations=3,
        )
        return self._snapshot

    async def close(self) -> WindowsOperatorRuntimeSnapshot:
        self.closed = True
        self._snapshot = WindowsOperatorRuntimeSnapshot(
            state=WindowsOperatorRuntimeState.CLOSED,
            viewport_count=1,
            open_surface_count=0,
            stream_count=1,
            delivered_frames=3,
            presentations=3,
        )
        return self._snapshot


def test_launcher_wraps_live_delivery_only_when_detection_provider_is_present() -> None:
    runtime = _CapturingRuntime()

    async def provider(_frame: PresentationVideoFrame) -> tuple[object, ...]:
        return ()

    launcher = WindowsSingleLiveOperatorLauncher(
        delivery_factory=lambda _payload_type: _BaseDelivery(),
        runtime_factory=lambda _layout: runtime,
        detection_provider=provider,
    )

    metrics = asyncio.run(
        launcher.run(
            ResolvedLiveSource("rtsp://private-source/live", 96),
            width=1280,
            height=720,
        )
    )

    assert metrics.delivered_frames == 3
    assert metrics.analytics_enabled is True
    assert metrics.analytics_provider_submissions == 0
    assert metrics.analytics_provider_completions == 0
    assert metrics.analytics_failures == 0
    assert metrics.analytics_rendered_boxes == 0
    assert isinstance(runtime.delivery, BoundedAnalyticsOverlayDelivery)
    assert runtime.closed is True


def test_launcher_returns_source_free_analytics_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _CapturingRuntime()

    async def provider(_frame: PresentationVideoFrame) -> tuple[object, ...]:
        return ()

    class AggregateAnalyticsDelivery:
        def __init__(self, _runner: object, _provider: object) -> None:
            self.snapshot = SimpleNamespace(
                provider_submissions=9,
                provider_completions=7,
                analytics_failures=2,
                rendered_boxes=11,
            )

        async def run(self, _source_uri: str, _consumer: object) -> object:
            raise AssertionError("fake Windows runtime owns execution")

    monkeypatch.setattr(
        operator_runtime_module,
        "BoundedAnalyticsOverlayDelivery",
        AggregateAnalyticsDelivery,
    )
    launcher = WindowsSingleLiveOperatorLauncher(
        delivery_factory=lambda _payload_type: _BaseDelivery(),
        runtime_factory=lambda _layout: runtime,
        detection_provider=provider,
    )

    metrics = asyncio.run(
        launcher.run(
            ResolvedLiveSource("rtsp://private-source/live", 96),
            width=1280,
            height=720,
        )
    )

    assert metrics.analytics_enabled is True
    assert metrics.analytics_provider_submissions == 9
    assert metrics.analytics_provider_completions == 7
    assert metrics.analytics_failures == 2
    assert metrics.analytics_rendered_boxes == 11


def test_environment_builder_accepts_bounded_detection_provider() -> None:
    async def provider(_frame: PresentationVideoFrame) -> tuple[object, ...]:
        return ()

    resolver, launcher = build_environment_operator_runtime(
        {
            "K5_STAGE03_SOURCE": "rtsp://192.0.2.10/live",
            "K5_STAGE03_CAM_CRED": "operator\nsecret\n",
            "K5_OPERATOR_RTP_PAYLOAD_TYPE": "96",
        },
        credential_probe=lambda _source, _credentials: 0,
        detection_provider=provider,
    )

    assert isinstance(resolver, PrivateStageOneSourceResolver)
    assert isinstance(launcher, WindowsSingleLiveOperatorLauncher)


def test_environment_builder_rejects_invalid_detection_provider() -> None:
    resolver, launcher = build_environment_operator_runtime(
        {
            "K5_STAGE03_SOURCE": "rtsp://192.0.2.10/live",
            "K5_STAGE03_CAM_CRED": "operator\nsecret\n",
            "K5_OPERATOR_RTP_PAYLOAD_TYPE": "96",
        },
        credential_probe=lambda _source, _credentials: 0,
        detection_provider=object(),  # type: ignore[arg-type]
    )

    assert resolver is None
    assert launcher is None
