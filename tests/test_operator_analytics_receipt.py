from __future__ import annotations

import asyncio

from k5vision.domain.devices import DeviceCreate, DeviceKind, DeviceProtocol
from k5vision.domain.users import UserAccount, UserRole
from k5vision.operator_launch import (
    BoundedOperatorLaunchCoordinator,
    OperatorLaunchMetrics,
    OperatorLaunchRequest,
    ResolvedLiveSource,
)
from k5vision.services.device_registry import DeviceRegistry


class _Resolver:
    async def resolve(self, _device: object, _stream_token: str) -> ResolvedLiveSource:
        return ResolvedLiveSource("rtsp://192.0.2.10/live", 96)


class _Launcher:
    async def run(
        self,
        _source: ResolvedLiveSource,
        *,
        width: int,
        height: int,
    ) -> OperatorLaunchMetrics:
        assert width == 1280
        assert height == 720
        return OperatorLaunchMetrics(
            delivered_frames=23,
            presentations=23,
            analytics_enabled=True,
            analytics_provider_submissions=8,
            analytics_provider_completions=6,
            analytics_failures=2,
            analytics_rendered_boxes=14,
        )


def test_operator_receipt_carries_only_aggregate_analytics_outcome(tmp_path: object) -> None:
    registry = DeviceRegistry(
        database_path=tmp_path / "devices.db",  # type: ignore[operator]
        site_id="stage-one",
    )
    device = registry.register(
        DeviceCreate(
            name="Synthetic Camera",
            host="192.0.2.10",
            kind=DeviceKind.CAMERA,
            protocols={DeviceProtocol.RTSP},
        )
    )
    principal = UserAccount(
        username="operator.one",
        display_name="Operator One",
        role=UserRole.OPERATOR,
        enabled=True,
    )
    coordinator = BoundedOperatorLaunchCoordinator(registry, _Resolver(), _Launcher())

    async def scenario() -> None:
        receipt = await coordinator.launch(
            principal,
            OperatorLaunchRequest(
                device_id=device.id,  # type: ignore[attr-defined]
                stream_token="main",
            ),
        )
        assert receipt.schema_version == "2"
        assert receipt.completed is True
        assert receipt.analytics_enabled is True
        assert receipt.analytics_provider_submissions == 8
        assert receipt.analytics_provider_completions == 6
        assert receipt.analytics_failures == 2
        assert receipt.analytics_rendered_boxes == 14

        retained = receipt.model_dump_json().casefold()
        for forbidden in (
            "rtsp://",
            "192.0.2.10",
            "source_uri",
            "credential",
            "password",
            "device_id",
            "stream_token",
        ):
            assert forbidden not in retained

    asyncio.run(scenario())
    registry.close()
