import asyncio
from pathlib import Path

import pytest

from k5vision.domain.devices import Device, DeviceCreate, DeviceKind, DeviceProtocol
from k5vision.domain.users import UserAccount, UserRole
from k5vision.media.recording_descriptor import parse_recording_descriptor
from k5vision.operator_launch import ResolvedLiveSource
from k5vision.operator_recording import (
    BoundedOperatorRecordingCoordinator,
    OperatorRecordingError,
    OperatorRecordingErrorCode,
    OperatorRecordingRequest,
)
from k5vision.services.device_registry import DeviceRegistry


def _packet(sequence: int, timestamp: int, payload_type: int = 96) -> memoryview:
    raw = bytearray(12)
    raw[0] = 0x80
    raw[1] = payload_type
    raw[2:4] = sequence.to_bytes(2, "big")
    raw[4:8] = timestamp.to_bytes(4, "big")
    raw[8:12] = (1).to_bytes(4, "big")
    return memoryview(raw)


class _StaticResolver:
    def __init__(self, source: ResolvedLiveSource) -> None:
        self.source = source

    async def resolve(self, device: Device, stream_token: str) -> ResolvedLiveSource:
        assert stream_token == "main"
        return self.source


class _FakeDelivery:
    def __init__(self, packets: tuple[memoryview, ...], expected_uri: str) -> None:
        self._packets = packets
        self._expected_uri = expected_uri

    async def deliver(self, source_uri: str, consumer) -> object:
        assert source_uri == self._expected_uri
        for packet in self._packets:
            await consumer(packet)
        return object()


def _registry(tmp_path: Path) -> tuple[DeviceRegistry, Device]:
    registry = DeviceRegistry(
        database_path=tmp_path / "devices.sqlite3",
        site_id="recording-test",
    )
    enrolled = registry.enroll(
        DeviceCreate(
            name="Synthetic Camera",
            host="192.0.2.10",
            kind=DeviceKind.CAMERA,
            protocols={DeviceProtocol.RTSP},
        )
    ).device
    return registry, enrolled


def _principal(role: UserRole = UserRole.OPERATOR) -> UserAccount:
    return UserAccount(
        username="operator-user",
        display_name="Operator User",
        role=role,
        enabled=True,
    )


def test_operator_recording_persists_replay_ready_framed_recording(tmp_path: Path) -> None:
    async def exercise() -> None:
        registry, device = _registry(tmp_path)
        source_uri = "rtsp://192.0.2.10/private-live"
        packets = (
            _packet(1, 0),
            _packet(2, 9_000),
            _packet(3, 18_000),
        )
        coordinator = BoundedOperatorRecordingCoordinator(
            registry,
            _StaticResolver(ResolvedLiveSource(source_uri, 96)),
            tmp_path / "recordings",
            delivery_factory=lambda: _FakeDelivery(packets, source_uri),
            packet_goal=3,
            max_recording_bytes=1024 * 1024,
        )

        try:
            receipt = await coordinator.record(
                _principal(),
                OperatorRecordingRequest(device_id=device.id, stream_token="main"),
            )

            recording_path = tmp_path / "recordings" / f"{receipt.recording_id}.k5r"
            descriptor_path = tmp_path / "recordings" / f"{receipt.recording_id}.k5d"
            assert recording_path.is_file()
            assert descriptor_path.is_file()
            descriptor = parse_recording_descriptor(descriptor_path.read_bytes())

            assert receipt.packet_count == 3
            assert receipt.duration_ms == 200
            assert descriptor.recording_id == receipt.recording_id
            assert descriptor.source_id == device.id
            assert descriptor.packet_count == receipt.packet_count
            assert descriptor.payload_bytes == receipt.payload_bytes
            assert descriptor.duration_ms == receipt.duration_ms
            assert coordinator.active_recordings == 0

            public_payload = receipt.model_dump_json()
            assert source_uri not in public_payload
            assert str(tmp_path) not in public_payload
            assert "credential" not in public_payload.casefold()
        finally:
            registry.close()

    asyncio.run(exercise())


def test_operator_recording_rejects_viewer_without_creating_media(tmp_path: Path) -> None:
    async def exercise() -> None:
        registry, device = _registry(tmp_path)
        root = tmp_path / "recordings"
        coordinator = BoundedOperatorRecordingCoordinator(
            registry,
            _StaticResolver(ResolvedLiveSource("rtsp://192.0.2.10/live", 96)),
            root,
            delivery_factory=lambda: _FakeDelivery((), "rtsp://192.0.2.10/live"),
            packet_goal=1,
        )

        try:
            with pytest.raises(OperatorRecordingError) as raised:
                await coordinator.record(
                    _principal(UserRole.VIEWER),
                    OperatorRecordingRequest(device_id=device.id, stream_token="main"),
                )
            assert raised.value.code is OperatorRecordingErrorCode.INSUFFICIENT_PERMISSION
            assert not root.exists()
            assert coordinator.active_recordings == 0
        finally:
            registry.close()

    asyncio.run(exercise())


def test_operator_recording_rejects_source_outside_enrolled_device_scope(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        registry, device = _registry(tmp_path)
        root = tmp_path / "recordings"
        coordinator = BoundedOperatorRecordingCoordinator(
            registry,
            _StaticResolver(ResolvedLiveSource("rtsp://192.0.2.99/live", 96)),
            root,
            delivery_factory=lambda: _FakeDelivery((), "rtsp://192.0.2.99/live"),
            packet_goal=1,
        )

        try:
            with pytest.raises(OperatorRecordingError) as raised:
                await coordinator.record(
                    _principal(),
                    OperatorRecordingRequest(device_id=device.id, stream_token="main"),
                )
            assert raised.value.code is OperatorRecordingErrorCode.SOURCE_SCOPE_MISMATCH
            assert not root.exists()
            assert coordinator.active_recordings == 0
        finally:
            registry.close()

    asyncio.run(exercise())
