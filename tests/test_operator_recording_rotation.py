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
from k5vision.operator_recording_catalog import BoundedRecordingCatalog
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
        site_id="rotation-test",
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


def _principal() -> UserAccount:
    return UserAccount(
        username="rotation-operator",
        display_name="Rotation Operator",
        role=UserRole.OPERATOR,
        enabled=True,
    )


def test_continuous_recording_rotates_finalized_segments_and_recovers_catalog(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        registry, device = _registry(tmp_path)
        source_uri = "rtsp://192.0.2.10/private-live"
        packets = tuple(_packet(index + 1, index * 9_000) for index in range(6))
        root = tmp_path / "recordings"
        coordinator = BoundedOperatorRecordingCoordinator(
            registry,
            _StaticResolver(ResolvedLiveSource(source_uri, 96)),
            root,
            delivery_factory=lambda: _FakeDelivery(packets, source_uri),
            packet_goal=6,
            segment_packet_goal=2,
            max_segments=3,
            max_recording_bytes=1024 * 1024,
        )

        try:
            receipt = await coordinator.record_continuous(
                _principal(),
                OperatorRecordingRequest(device_id=device.id, stream_token="main"),
            )

            assert receipt.segment_count == 3
            assert receipt.packet_count == 6
            assert len(receipt.segments) == 3
            assert [segment.packet_count for segment in receipt.segments] == [2, 2, 2]
            assert coordinator.active_recordings == 0

            descriptors = []
            for segment in receipt.segments:
                recording_path = root / f"{segment.recording_id}.k5r"
                descriptor_path = root / f"{segment.recording_id}.k5d"
                assert recording_path.is_file()
                assert descriptor_path.is_file()
                descriptor = parse_recording_descriptor(descriptor_path.read_bytes())
                descriptors.append(descriptor)
                assert descriptor.source_id == device.id
                assert descriptor.packet_count == 2
                assert recording_path.stat().st_size == descriptor.file_bytes

            assert descriptors[0].ended_at_utc <= descriptors[1].started_at_utc
            assert descriptors[1].ended_at_utc <= descriptors[2].started_at_utc

            first_catalog = BoundedRecordingCatalog(root)
            first_snapshot = first_catalog.recover()
            assert first_snapshot.recovered_entries == 3
            assert first_snapshot.rejected_entries == 0

            restarted_catalog = BoundedRecordingCatalog(root)
            page = restarted_catalog.page(source_id=device.id, limit=10)
            assert page.recovered_entries == 3
            assert len(page.entries) == 3
            assert {entry.recording_id for entry in page.entries} == {
                segment.recording_id for segment in receipt.segments
            }

            public_payload = page.model_dump_json()
            assert source_uri not in public_payload
            assert str(tmp_path) not in public_payload
            assert "credential" not in public_payload.casefold()
        finally:
            registry.close()

    asyncio.run(exercise())


def test_rotation_capacity_failure_preserves_only_already_finalized_segments(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        registry, device = _registry(tmp_path)
        source_uri = "rtsp://192.0.2.10/private-live"
        packets = tuple(_packet(index + 1, index * 9_000) for index in range(6))
        root = tmp_path / "recordings"
        coordinator = BoundedOperatorRecordingCoordinator(
            registry,
            _StaticResolver(ResolvedLiveSource(source_uri, 96)),
            root,
            delivery_factory=lambda: _FakeDelivery(packets, source_uri),
            packet_goal=6,
            segment_packet_goal=2,
            max_segments=2,
            max_recording_bytes=1024 * 1024,
        )

        try:
            with pytest.raises(OperatorRecordingError) as raised:
                await coordinator.record_continuous(
                    _principal(),
                    OperatorRecordingRequest(device_id=device.id, stream_token="main"),
                )
            assert raised.value.code is OperatorRecordingErrorCode.RECORDING_FAILURE
            assert coordinator.active_recordings == 0

            catalog = BoundedRecordingCatalog(root)
            snapshot = catalog.recover()
            assert snapshot.recovered_entries == 2
            assert snapshot.rejected_entries == 0
            assert len(catalog.page(limit=10).entries) == 2
            assert not list(root.glob("*.stage"))
        finally:
            registry.close()

    asyncio.run(exercise())


def test_catalog_rejects_incomplete_or_mismatched_pairs_after_restart(tmp_path: Path) -> None:
    root = tmp_path / "recordings"
    root.mkdir()
    recording_id = "11111111-1111-4111-8111-111111111111"
    (root / f"{recording_id}.k5d").write_text("{}", encoding="utf-8")
    (root / "orphan.k5d.stage").write_text("private", encoding="utf-8")
    (root / ".stale.k5r.part").write_text("owner", encoding="utf-8")

    catalog = BoundedRecordingCatalog(root)
    snapshot = catalog.recover()
    page = catalog.page(limit=10)

    assert snapshot.recovered_entries == 0
    assert snapshot.rejected_entries == 1
    assert page.entries == ()
    assert "orphan" not in page.model_dump_json()
    assert "stale" not in page.model_dump_json()


def test_continuous_recording_rejects_invalid_segment_configuration(tmp_path: Path) -> None:
    registry, _ = _registry(tmp_path)
    source_uri = "rtsp://192.0.2.10/private-live"
    resolver = _StaticResolver(ResolvedLiveSource(source_uri, 96))
    root = tmp_path / "recordings"

    try:
        with pytest.raises(ValueError, match="max_segments"):
            BoundedOperatorRecordingCoordinator(
                registry,
                resolver,
                root,
                packet_goal=2,
                max_segments=0,
            )
        with pytest.raises(ValueError, match="segment_packet_goal"):
            BoundedOperatorRecordingCoordinator(
                registry,
                resolver,
                root,
                packet_goal=2,
                segment_packet_goal=3,
            )
    finally:
        registry.close()


def test_continuous_recording_payload_mismatch_cleans_active_segment(tmp_path: Path) -> None:
    async def exercise() -> None:
        registry, device = _registry(tmp_path)
        source_uri = "rtsp://192.0.2.10/private-live"
        packets = (_packet(1, 0, payload_type=97),)
        root = tmp_path / "recordings"
        coordinator = BoundedOperatorRecordingCoordinator(
            registry,
            _StaticResolver(ResolvedLiveSource(source_uri, 96)),
            root,
            delivery_factory=lambda: _FakeDelivery(packets, source_uri),
            packet_goal=1,
            segment_packet_goal=1,
            max_segments=1,
        )

        try:
            with pytest.raises(OperatorRecordingError) as raised:
                await coordinator.record_continuous(
                    _principal(),
                    OperatorRecordingRequest(device_id=device.id, stream_token="main"),
                )
            assert raised.value.code is OperatorRecordingErrorCode.RECORDING_FAILURE
            assert coordinator.active_recordings == 0
            assert not list(root.glob("*.k5r"))
            assert not list(root.glob("*.k5d"))
            assert not list(root.glob("*.stage"))
        finally:
            registry.close()

    asyncio.run(exercise())


def test_continuous_recording_rejects_empty_delivery_without_publishing_media(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        registry, device = _registry(tmp_path)
        source_uri = "rtsp://192.0.2.10/private-live"
        root = tmp_path / "recordings"
        coordinator = BoundedOperatorRecordingCoordinator(
            registry,
            _StaticResolver(ResolvedLiveSource(source_uri, 96)),
            root,
            delivery_factory=lambda: _FakeDelivery((), source_uri),
            packet_goal=1,
            segment_packet_goal=1,
            max_segments=1,
        )

        try:
            with pytest.raises(OperatorRecordingError) as raised:
                await coordinator.record_continuous(
                    _principal(),
                    OperatorRecordingRequest(device_id=device.id, stream_token="main"),
                )
            assert raised.value.code is OperatorRecordingErrorCode.RECORDING_FAILURE
            assert coordinator.active_recordings == 0
            assert not list(root.glob("*.k5r"))
            assert not list(root.glob("*.k5d"))
        finally:
            registry.close()

    asyncio.run(exercise())
