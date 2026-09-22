"""Regression coverage for the Stage-One authenticated operator launch seam."""

from __future__ import annotations

import asyncio

import pytest

from k5vision.domain.devices import DeviceCreate, DeviceKind, DeviceProtocol
from k5vision.domain.users import UserAccount, UserRole
from k5vision.operator_launch import (
    BoundedOperatorLaunchCoordinator,
    OperatorLaunchError,
    OperatorLaunchErrorCode,
    OperatorLaunchMetrics,
    OperatorLaunchRequest,
    ResolvedLiveSource,
)
from k5vision.services.device_registry import DeviceRegistry


class _Resolver:
    def __init__(self, source: ResolvedLiveSource | None = None) -> None:
        self.source = source
        self.calls = 0
        self.stream_tokens: list[str] = []

    async def resolve(self, _device: object, stream_token: str) -> ResolvedLiveSource:
        self.calls += 1
        self.stream_tokens.append(stream_token)
        if self.source is None:
            raise RuntimeError("rtsp://operator:super-secret@192.0.2.10/private")
        return self.source


class _Launcher:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0
        self.sources: list[ResolvedLiveSource] = []

    async def run(
        self,
        source: ResolvedLiveSource,
        *,
        width: int,
        height: int,
    ) -> OperatorLaunchMetrics:
        self.calls += 1
        self.sources.append(source)
        assert width == 1280
        assert height == 720
        if self.fail:
            raise RuntimeError("password=super-secret rtsp://192.0.2.10/private")
        return OperatorLaunchMetrics(
            delivered_frames=37,
            presentations=37,
            processed_controls=2,
        )


def _registry(tmp_path: object) -> tuple[DeviceRegistry, object]:
    database_path = tmp_path / "devices.db"  # type: ignore[operator]
    registry = DeviceRegistry(database_path=database_path, site_id="stage-one")
    device = registry.register(
        DeviceCreate(
            name="Loading Dock",
            host="192.0.2.10",
            kind=DeviceKind.CAMERA,
            protocols={DeviceProtocol.ONVIF, DeviceProtocol.RTSP},
        )
    )
    return registry, device


def _principal(*, enabled: bool = True) -> UserAccount:
    return UserAccount(
        username="viewer.one",
        display_name="Viewer One",
        role=UserRole.VIEWER,
        enabled=enabled,
    )


def test_authorized_enrolled_source_reaches_private_launcher_without_receipt_leak(
    tmp_path: object,
) -> None:
    registry, device = _registry(tmp_path)
    private_uri = "rtsp://operator:super-secret@192.0.2.10/live?transport=tcp"
    resolver = _Resolver(ResolvedLiveSource(private_uri, 96))
    launcher = _Launcher()
    coordinator = BoundedOperatorLaunchCoordinator(registry, resolver, launcher)

    async def scenario() -> None:
        receipt = await coordinator.launch(
            _principal(),
            OperatorLaunchRequest(device_id=device.id, stream_token="main-profile"),  # type: ignore[attr-defined]
        )

        assert receipt.delivered_frames == 37
        assert receipt.presentations == 37
        assert receipt.processed_controls == 2
        assert resolver.stream_tokens == ["main-profile"]
        assert launcher.sources == [ResolvedLiveSource(private_uri, 96)]
        assert coordinator.active_launches == 0

        retained = receipt.model_dump_json().casefold()
        for forbidden in (
            "rtsp://",
            "192.0.2.10",
            "super-secret",
            "operator",
            "main-profile",
            "password",
            "credential",
            "source_uri",
        ):
            assert forbidden not in retained

    asyncio.run(scenario())
    registry.close()


def test_disabled_human_principal_fails_before_private_resolution(tmp_path: object) -> None:
    registry, device = _registry(tmp_path)
    resolver = _Resolver(ResolvedLiveSource("rtsp://192.0.2.10/live", 96))
    launcher = _Launcher()
    coordinator = BoundedOperatorLaunchCoordinator(registry, resolver, launcher)

    async def scenario() -> None:
        with pytest.raises(OperatorLaunchError) as caught:
            await coordinator.launch(
                _principal(enabled=False),
                OperatorLaunchRequest(
                    device_id=device.id,  # type: ignore[attr-defined]
                    stream_token="main",
                ),
            )
        assert caught.value.code == OperatorLaunchErrorCode.UNAUTHORIZED
        assert resolver.calls == 0
        assert launcher.calls == 0

    asyncio.run(scenario())
    registry.close()


def test_resolved_source_must_remain_bound_to_enrolled_device(tmp_path: object) -> None:
    registry, device = _registry(tmp_path)
    secret = "rtsp://operator:super-secret@198.51.100.77/live"
    resolver = _Resolver(ResolvedLiveSource(secret, 96))
    launcher = _Launcher()
    coordinator = BoundedOperatorLaunchCoordinator(registry, resolver, launcher)

    async def scenario() -> None:
        with pytest.raises(OperatorLaunchError) as caught:
            await coordinator.launch(
                _principal(),
                OperatorLaunchRequest(
                    device_id=device.id,  # type: ignore[attr-defined]
                    stream_token="main",
                ),
            )
        assert caught.value.code == OperatorLaunchErrorCode.SOURCE_SCOPE_MISMATCH
        assert launcher.calls == 0
        message = str(caught.value).casefold()
        assert "rtsp://" not in message
        assert "super-secret" not in message
        assert "198.51.100.77" not in message
        assert coordinator.active_launches == 0

    asyncio.run(scenario())
    registry.close()


def test_private_resolver_and_launcher_failures_are_sanitized(tmp_path: object) -> None:
    registry, device = _registry(tmp_path)

    async def resolver_failure() -> None:
        coordinator = BoundedOperatorLaunchCoordinator(
            registry,
            _Resolver(),
            _Launcher(),
        )
        with pytest.raises(OperatorLaunchError) as caught:
            await coordinator.launch(
                _principal(),
                OperatorLaunchRequest(
                    device_id=device.id,  # type: ignore[attr-defined]
                    stream_token="main",
                ),
            )
        assert caught.value.code == OperatorLaunchErrorCode.SOURCE_UNAVAILABLE
        assert "super-secret" not in str(caught.value).casefold()
        assert "rtsp://" not in str(caught.value).casefold()
        assert coordinator.active_launches == 0

    async def launcher_failure() -> None:
        coordinator = BoundedOperatorLaunchCoordinator(
            registry,
            _Resolver(ResolvedLiveSource("rtsp://operator:super-secret@192.0.2.10/live", 96)),
            _Launcher(fail=True),
        )
        with pytest.raises(OperatorLaunchError) as caught:
            await coordinator.launch(
                _principal(),
                OperatorLaunchRequest(
                    device_id=device.id,  # type: ignore[attr-defined]
                    stream_token="main",
                ),
            )
        assert caught.value.code == OperatorLaunchErrorCode.LAUNCH_FAILURE
        assert "super-secret" not in str(caught.value).casefold()
        assert "rtsp://" not in str(caught.value).casefold()
        assert coordinator.active_launches == 0

    asyncio.run(resolver_failure())
    asyncio.run(launcher_failure())
    registry.close()


def test_unsupported_device_fails_before_private_resolution(tmp_path: object) -> None:
    database_path = tmp_path / "unsupported.db"  # type: ignore[operator]
    registry = DeviceRegistry(database_path=database_path, site_id="stage-one")
    device = registry.register(
        DeviceCreate(
            name="Environmental Sensor",
            host="192.0.2.20",
            kind=DeviceKind.SENSOR,
            protocols={DeviceProtocol.HTTP},
        )
    )
    resolver = _Resolver(ResolvedLiveSource("rtsp://192.0.2.20/live", 96))
    launcher = _Launcher()
    coordinator = BoundedOperatorLaunchCoordinator(registry, resolver, launcher)

    async def scenario() -> None:
        with pytest.raises(OperatorLaunchError) as caught:
            await coordinator.launch(
                _principal(),
                OperatorLaunchRequest(device_id=device.id, stream_token="main"),
            )
        assert caught.value.code == OperatorLaunchErrorCode.UNSUPPORTED_DEVICE
        assert resolver.calls == 0
        assert launcher.calls == 0

    asyncio.run(scenario())
    registry.close()


def test_concurrency_budget_rejects_excess_without_resolving_second_source(
    tmp_path: object,
) -> None:
    registry, device = _registry(tmp_path)
    entered = asyncio.Event()
    release = asyncio.Event()

    class BlockingResolver:
        calls = 0

        async def resolve(self, _device: object, _stream_token: str) -> ResolvedLiveSource:
            self.calls += 1
            entered.set()
            await release.wait()
            return ResolvedLiveSource("rtsp://192.0.2.10/live", 96)

    resolver = BlockingResolver()
    coordinator = BoundedOperatorLaunchCoordinator(
        registry,
        resolver,
        _Launcher(),
        max_active_launches=1,
    )
    request = OperatorLaunchRequest(
        device_id=device.id,  # type: ignore[attr-defined]
        stream_token="main",
    )

    async def scenario() -> None:
        first = asyncio.create_task(coordinator.launch(_principal(), request))
        await entered.wait()
        assert coordinator.active_launches == 1

        with pytest.raises(OperatorLaunchError) as caught:
            await coordinator.launch(_principal(), request)
        assert caught.value.code == OperatorLaunchErrorCode.LAUNCH_BUSY
        assert resolver.calls == 1

        release.set()
        receipt = await first
        assert receipt.completed is True
        assert coordinator.active_launches == 0

    asyncio.run(scenario())
    registry.close()
