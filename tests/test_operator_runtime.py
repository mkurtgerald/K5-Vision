"""Regression coverage for the private Stage-One physical live runtime bridge."""

from __future__ import annotations

import asyncio

import pytest

from k5vision.domain.devices import Device, DeviceCreate, DeviceKind, DeviceProtocol
from k5vision.media.windows_operator_runtime import (
    WindowsOperatorRuntimeSnapshot,
    WindowsOperatorRuntimeState,
)
from k5vision.operator_launch import (
    OperatorLaunchError,
    OperatorLaunchErrorCode,
    ResolvedLiveSource,
)
from k5vision.operator_runtime import (
    PrivateStageOneSourceResolver,
    WindowsSingleLiveOperatorLauncher,
    build_environment_operator_runtime,
)


def _device(host: str = "192.0.2.10") -> Device:
    return Device(
        **DeviceCreate(
            name="Loading Dock",
            host=host,
            kind=DeviceKind.CAMERA,
            protocols={DeviceProtocol.ONVIF, DeviceProtocol.RTSP},
        ).model_dump()
    )


def test_private_resolver_selects_authenticated_uri_without_retaining_public_metadata() -> None:
    calls: list[tuple[str, str]] = []

    def probe(source_uri: str, credential_bundle: str) -> int | None:
        calls.append((source_uri, credential_bundle))
        return 1

    resolver = PrivateStageOneSourceResolver(
        "rtsp://192.0.2.10/live?transport=tcp",
        "operator\nfirst-secret\nsecond-secret\n",
        stream_token="main-profile",
        payload_type=96,
        credential_probe=probe,
    )

    async def scenario() -> None:
        resolved = await resolver.resolve(_device(), "main-profile")
        assert resolved.payload_type == 96
        assert resolved.source_uri == (
            "rtsp://operator:second-secret@192.0.2.10/live?transport=tcp"
        )
        assert len(calls) == 1

    asyncio.run(scenario())


def test_private_resolver_rejects_wrong_selection_before_credential_probe() -> None:
    calls = 0

    def probe(_source_uri: str, _credential_bundle: str) -> int | None:
        nonlocal calls
        calls += 1
        return 0

    resolver = PrivateStageOneSourceResolver(
        "rtsp://192.0.2.10/live",
        "operator\nfirst-secret\nsecond-secret\n",
        credential_probe=probe,
    )

    async def scenario() -> None:
        with pytest.raises(OperatorLaunchError) as caught:
            await resolver.resolve(_device(), "substream")
        assert caught.value.code == OperatorLaunchErrorCode.SOURCE_UNAVAILABLE
        assert calls == 0
        assert "rtsp://" not in str(caught.value).casefold()
        assert "secret" not in str(caught.value).casefold()

    asyncio.run(scenario())


def test_private_resolver_rejects_source_outside_enrolled_device_scope() -> None:
    calls = 0

    def probe(_source_uri: str, _credential_bundle: str) -> int | None:
        nonlocal calls
        calls += 1
        return 0

    resolver = PrivateStageOneSourceResolver(
        "rtsp://198.51.100.44/live",
        "operator\nfirst-secret\nsecond-secret\n",
        credential_probe=probe,
    )

    async def scenario() -> None:
        with pytest.raises(OperatorLaunchError) as caught:
            await resolver.resolve(_device(), "main")
        assert caught.value.code == OperatorLaunchErrorCode.SOURCE_SCOPE_MISMATCH
        assert calls == 0
        assert "198.51.100.44" not in str(caught.value)
        assert "rtsp://" not in str(caught.value).casefold()

    asyncio.run(scenario())


class _UnusedDelivery:
    async def run(self, _source_uri: str, _consumer: object) -> object:
        raise AssertionError("fake runtime owns execution in this unit regression")


class _Runtime:
    def __init__(self) -> None:
        self.closed = False
        self.source_uri = ""
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
        assert len(selected) == 1
        self.source_uri = selected[0].source_uri
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
            delivered_frames=7,
            presentations=7,
        )
        return self._snapshot

    async def close(self) -> WindowsOperatorRuntimeSnapshot:
        self.closed = True
        self._snapshot = WindowsOperatorRuntimeSnapshot(
            state=WindowsOperatorRuntimeState.CLOSED,
            viewport_count=1,
            open_surface_count=0,
            stream_count=1,
            delivered_frames=7,
            presentations=7,
        )
        return self._snapshot


def test_windows_launcher_drives_exactly_one_private_live_stream_and_closes() -> None:
    runtimes: list[_Runtime] = []
    observed_layouts: list[object] = []

    def runtime_factory(layout: object) -> _Runtime:
        observed_layouts.append(layout)
        runtime = _Runtime()
        runtimes.append(runtime)
        return runtime

    launcher = WindowsSingleLiveOperatorLauncher(
        delivery_factory=lambda _payload_type: _UnusedDelivery(),
        runtime_factory=runtime_factory,  # type: ignore[arg-type]
    )
    private_source = ResolvedLiveSource(
        "rtsp://operator:super-secret@192.0.2.10/live",
        96,
    )

    async def scenario() -> None:
        metrics = await launcher.run(private_source, width=1280, height=720)
        assert metrics.delivered_frames == 7
        assert metrics.presentations == 7
        assert metrics.processed_controls == 0
        assert len(observed_layouts) == 1
        assert len(runtimes) == 1
        assert runtimes[0].source_uri == private_source.source_uri
        assert runtimes[0].closed is True
        retained = metrics.model_dump_json().casefold()
        assert "rtsp://" not in retained
        assert "super-secret" not in retained
        assert "192.0.2.10" not in retained

    asyncio.run(scenario())


def test_environment_builder_is_fail_closed_for_partial_private_configuration() -> None:
    resolver, launcher = build_environment_operator_runtime({})
    assert resolver is None
    assert launcher is None

    resolver, launcher = build_environment_operator_runtime(
        {"K5_STAGE03_SOURCE": "rtsp://192.0.2.10/live"}
    )
    assert resolver is None
    assert launcher is None


def test_environment_builder_accepts_complete_private_configuration() -> None:
    resolver, launcher = build_environment_operator_runtime(
        {
            "K5_STAGE03_SOURCE": "rtsp://192.0.2.10/live",
            "K5_STAGE03_CAM_CRED": "operator\nfirst-secret\nsecond-secret\n",
            "K5_OPERATOR_STREAM_TOKEN": "main-profile",
            "K5_OPERATOR_RTP_PAYLOAD_TYPE": "96",
        },
        credential_probe=lambda _source, _credentials: 0,
    )
    assert isinstance(resolver, PrivateStageOneSourceResolver)
    assert isinstance(launcher, WindowsSingleLiveOperatorLauncher)
