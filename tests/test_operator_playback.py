from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from k5vision.domain.devices import Device, DeviceCreate, DeviceKind, DeviceProtocol
from k5vision.domain.users import UserAccount, UserRole
from k5vision.main import create_app
from k5vision.media.framed_recording import FramedAtomicRecordingSink
from k5vision.media.mixed_presentation import MixedLiveStream, MixedPlaybackStream
from k5vision.media.playback_schedule import PlaybackRate
from k5vision.media.recording_descriptor import RecordingStreamDescriptor, VideoCodec
from k5vision.media.windows_operator_runtime import (
    WindowsOperatorRuntimeSnapshot,
    WindowsOperatorRuntimeState,
)
from k5vision.operator_launch import ResolvedLiveSource
from k5vision.operator_playback import (
    BoundedOperatorPlaybackCoordinator,
    OperatorPlaybackError,
    OperatorPlaybackErrorCode,
    OperatorPlaybackMetrics,
    OperatorPlaybackRequest,
    WindowsMixedOperatorPlaybackLauncher,
)
from k5vision.services.device_registry import DeviceRegistry
from k5vision.user_admin_api import USER_ADMIN_TOKEN_ENV, USER_DB_PATH_ENV

_ADMIN_TOKEN = "synthetic-admin-service-token"
_PASSWORD = "synthetic-password-12345"
_SITE_ID = "synthetic-playback-site"


def _packet(sequence: int, timestamp: int, payload_type: int = 96) -> memoryview:
    raw = bytearray(12)
    raw[0] = 0x80
    raw[1] = payload_type
    raw[2:4] = sequence.to_bytes(2, "big")
    raw[4:8] = timestamp.to_bytes(4, "big")
    raw[8:12] = (1).to_bytes(4, "big")
    return memoryview(raw)


async def _write_recording_pair(root: Path, recording_id: UUID, source_id: UUID) -> None:
    sink = FramedAtomicRecordingSink(
        root,
        str(recording_id),
        max_packets=2,
        max_payload_bytes=4096,
    )
    await sink.open()
    await sink.write(_packet(1, 0))
    await sink.write(_packet(2, 9_000))
    await sink.finalize()
    counters = sink.snapshot
    started = datetime(2026, 1, 1, tzinfo=UTC)
    descriptor = RecordingStreamDescriptor(
        recording_id=recording_id,
        source_id=source_id,
        codec=VideoCodec.H264,
        payload_type=96,
        clock_rate_hz=90_000,
        started_at_utc=started,
        ended_at_utc=started + timedelta(milliseconds=100),
        duration_ms=100,
        rtp_timestamp_origin=0,
        packet_count=counters.packets,
        payload_bytes=counters.payload_bytes,
        file_bytes=counters.file_bytes,
    )
    (root / f"{recording_id}.k5d").write_bytes(descriptor.to_json_bytes())


class _Resolver:
    def __init__(self) -> None:
        self.calls = 0

    async def resolve(self, device: Device, stream_token: str) -> ResolvedLiveSource:
        self.calls += 1
        assert stream_token == "main"
        return ResolvedLiveSource(f"rtsp://synthetic@{device.host}/live", 96)


class _Launcher:
    def __init__(self) -> None:
        self.calls = 0
        self.last_recording: Path | None = None

    async def run(
        self,
        source: ResolvedLiveSource,
        recording_path: Path,
        descriptor: RecordingStreamDescriptor,
        *,
        start_ms: int,
        end_ms: int,
        rate: PlaybackRate,
        width: int,
        height: int,
    ) -> OperatorPlaybackMetrics:
        self.calls += 1
        self.last_recording = recording_path
        assert source.payload_type == descriptor.payload_type == 96
        assert recording_path.is_file()
        assert start_ms == 0
        assert end_ms == 100
        assert rate is PlaybackRate.NORMAL
        assert (width, height) == (1280, 720)
        return OperatorPlaybackMetrics(
            delivered_frames=9,
            presentations=9,
            descriptor_verified=True,
        )


def _registry(tmp_path: Path) -> tuple[DeviceRegistry, Device]:
    registry = DeviceRegistry(
        database_path=tmp_path / "devices.sqlite3",
        site_id="playback-test",
    )
    device = registry.enroll(
        DeviceCreate(
            name="Synthetic Camera",
            host="192.0.2.20",
            kind=DeviceKind.CAMERA,
            protocols={DeviceProtocol.RTSP},
        )
    ).device
    return registry, device


def _principal(role: UserRole = UserRole.VIEWER) -> UserAccount:
    return UserAccount(
        username="playback-user",
        display_name="Playback User",
        role=role,
        enabled=True,
    )


def test_recording_uuid_binds_descriptor_device_and_operator_playback(tmp_path: Path) -> None:
    async def exercise() -> None:
        registry, device = _registry(tmp_path)
        root = tmp_path / "recordings"
        recording_id = uuid4()
        await _write_recording_pair(root, recording_id, device.id)
        resolver = _Resolver()
        launcher = _Launcher()
        coordinator = BoundedOperatorPlaybackCoordinator(
            registry,
            resolver,
            root,
            launcher,
        )
        try:
            receipt = await coordinator.play(
                _principal(),
                OperatorPlaybackRequest(recording_id=recording_id),
            )
        finally:
            registry.close()

        assert receipt.recording_id == recording_id
        assert receipt.completed is True
        assert receipt.descriptor_verified is True
        assert receipt.delivered_frames == 9
        assert resolver.calls == 1
        assert launcher.calls == 1
        assert coordinator.active_playbacks == 0
        retained = receipt.model_dump_json().casefold()
        assert "rtsp://" not in retained
        assert str(tmp_path).casefold() not in retained
        assert "stream_token" not in retained

    asyncio.run(exercise())


def test_missing_or_corrupt_recording_fails_closed_before_source_resolution(tmp_path: Path) -> None:
    async def exercise() -> None:
        registry, device = _registry(tmp_path)
        root = tmp_path / "recordings"
        resolver = _Resolver()
        launcher = _Launcher()
        coordinator = BoundedOperatorPlaybackCoordinator(registry, resolver, root, launcher)
        missing_id = uuid4()
        try:
            with pytest.raises(OperatorPlaybackError) as missing:
                await coordinator.play(
                    _principal(),
                    OperatorPlaybackRequest(recording_id=missing_id),
                )
            assert missing.value.code is OperatorPlaybackErrorCode.RECORDING_NOT_FOUND

            corrupt_id = uuid4()
            await _write_recording_pair(root, corrupt_id, device.id)
            (root / f"{corrupt_id}.k5d").write_text("{}", encoding="utf-8")
            with pytest.raises(OperatorPlaybackError) as corrupt:
                await coordinator.play(
                    _principal(),
                    OperatorPlaybackRequest(recording_id=corrupt_id),
                )
            assert corrupt.value.code is OperatorPlaybackErrorCode.RECORDING_INVALID
        finally:
            registry.close()

        assert resolver.calls == 0
        assert launcher.calls == 0
        assert coordinator.active_playbacks == 0

    asyncio.run(exercise())


class _PresentationDelivery:
    async def run(self, *args, **kwargs):
        return object()


class _Runtime:
    def __init__(self) -> None:
        self.streams = None
        self.closed = False

    async def start(self, streams):
        self.streams = tuple(streams)
        return object()

    async def wait(self) -> WindowsOperatorRuntimeSnapshot:
        return WindowsOperatorRuntimeSnapshot(
            state=WindowsOperatorRuntimeState.COMPLETE,
            viewport_count=2,
            open_surface_count=2,
            stream_count=2,
            delivered_frames=14,
            presentations=14,
        )

    async def close(self) -> WindowsOperatorRuntimeSnapshot:
        self.closed = True
        return WindowsOperatorRuntimeSnapshot(
            state=WindowsOperatorRuntimeState.CLOSED,
            viewport_count=2,
            open_surface_count=0,
            stream_count=2,
            delivered_frames=14,
            presentations=14,
        )


def test_windows_playback_launcher_composes_live_and_recorded_streams(tmp_path: Path) -> None:
    async def exercise() -> None:
        recording_id = uuid4()
        source_id = uuid4()
        root = tmp_path / "recordings"
        await _write_recording_pair(root, recording_id, source_id)
        descriptor = RecordingStreamDescriptor.model_validate_json(
            (root / f"{recording_id}.k5d").read_text(encoding="utf-8")
        )
        runtime = _Runtime()
        captured_layouts = []

        launcher = WindowsMixedOperatorPlaybackLauncher(
            runtime_factory=lambda layout: captured_layouts.append(layout) or runtime,
            live_delivery_factory=lambda payload_type: _PresentationDelivery(),
            playback_factory=lambda *args: _PresentationDelivery(),
        )
        metrics = await launcher.run(
            ResolvedLiveSource("rtsp://192.0.2.20/live", 96),
            root / f"{recording_id}.k5r",
            descriptor,
            start_ms=0,
            end_ms=100,
            rate=PlaybackRate.NORMAL,
            width=1280,
            height=720,
        )

        assert metrics.descriptor_verified is True
        assert metrics.delivered_frames == 14
        assert len(captured_layouts) == 1
        assert runtime.closed is True
        assert len(runtime.streams) == 2
        assert isinstance(runtime.streams[0], MixedLiveStream)
        assert isinstance(runtime.streams[1], MixedPlaybackStream)
        assert {item.slot for item in runtime.streams} == {0, 1}

    asyncio.run(exercise())


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _configure_user_state(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("K5_CONTROL_PLANE_TOKEN", raising=False)
    monkeypatch.delenv("K5_CONTROL_PLANE_READ_TOKEN", raising=False)
    monkeypatch.setenv(USER_ADMIN_TOKEN_ENV, _ADMIN_TOKEN)
    monkeypatch.setenv(USER_DB_PATH_ENV, str(tmp_path / "users.sqlite3"))


def _issue_session(client: TestClient, username: str, role: str) -> str:
    created = client.post(
        "/api/v1/users",
        json={"username": username, "display_name": username, "role": role, "enabled": True},
        headers=_headers(_ADMIN_TOKEN),
    )
    assert created.status_code == 201
    changed = client.post(
        "/api/v1/auth/bootstrap-password",
        json={
            "username": username,
            "temporary_credential": created.json()["temporary_credential"],
            "new_password": _PASSWORD,
        },
    )
    assert changed.status_code == 204
    login = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": _PASSWORD},
    )
    assert login.status_code == 200
    return login.json()["session_token"]


def test_authenticated_viewer_can_request_persisted_playback_without_exposing_paths(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_user_state(monkeypatch, tmp_path)
    root = tmp_path / "recordings"
    resolver = _Resolver()
    application = create_app(
        control_plane_site_id=_SITE_ID,
        device_db_path=tmp_path / "api-devices.sqlite3",
        operator_source_resolver=resolver,
        operator_recording_root=root,
    )

    with TestClient(application) as client:
        viewer = _issue_session(client, "viewer-user", "viewer")
        administrator = _issue_session(client, "administrator-user", "administrator")
        enrolled = client.post(
            "/api/v1/devices",
            json={
                "name": "Synthetic Camera",
                "host": "192.0.2.20",
                "kind": "camera",
                "protocols": ["rtsp"],
            },
            headers=_headers(administrator),
        )
        assert enrolled.status_code == 201
        recording_id = uuid4()
        asyncio.run(_write_recording_pair(root, recording_id, UUID(enrolled.json()["id"])))
        launcher = _Launcher()
        application.state.operator_playback_coordinator._playback_launcher = launcher

        response = client.post(
            f"/api/v1/operator/recordings/{recording_id}/playback",
            params={"stream_token": "main"},
            headers=_headers(viewer),
        )

    assert response.status_code == 200
    assert response.json()["recording_id"] == str(recording_id)
    assert response.json()["descriptor_verified"] is True
    assert launcher.calls == 1
    retained = response.text.casefold()
    assert "rtsp://" not in retained
    assert str(tmp_path).casefold() not in retained


def test_playback_route_requires_human_session(monkeypatch, tmp_path: Path) -> None:
    _configure_user_state(monkeypatch, tmp_path)
    application = create_app(
        control_plane_site_id=_SITE_ID,
        device_db_path=tmp_path / "unauthorized-devices.sqlite3",
        operator_source_resolver=_Resolver(),
        operator_recording_root=tmp_path / "recordings",
    )

    with TestClient(application) as client:
        response = client.post(f"/api/v1/operator/recordings/{uuid4()}/playback")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
