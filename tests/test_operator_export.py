from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from k5vision.domain.devices import DeviceCreate, DeviceKind, DeviceProtocol
from k5vision.domain.users import UserAccount, UserRole
from k5vision.main import create_app
from k5vision.media.framed_recording import FramedAtomicRecordingSink
from k5vision.media.recording_descriptor import RecordingStreamDescriptor, VideoCodec
from k5vision.operator_export import (
    BoundedOperatorExportCoordinator,
    OperatorExportError,
    OperatorExportErrorCode,
)
from k5vision.services.device_registry import DeviceRegistry
from k5vision.user_admin_api import USER_ADMIN_TOKEN_ENV, USER_DB_PATH_ENV

_ADMIN_TOKEN = "synthetic-admin-service-token"
_PASSWORD = "synthetic-password-12345"
_SITE_ID = "synthetic-export-site"


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


def _principal() -> UserAccount:
    return UserAccount(
        username="export-viewer",
        display_name="Export Viewer",
        role=UserRole.VIEWER,
        enabled=True,
    )


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


def test_export_streams_validated_pair_without_live_runtime(monkeypatch, tmp_path: Path) -> None:
    _configure_user_state(monkeypatch, tmp_path)
    root = tmp_path / "recordings"
    application = create_app(
        control_plane_site_id=_SITE_ID,
        device_db_path=tmp_path / "devices.sqlite3",
        operator_recording_root=root,
    )

    with TestClient(application) as client:
        viewer = _issue_session(client, "viewer-user", "viewer")
        administrator = _issue_session(client, "administrator-user", "administrator")
        enrolled = client.post(
            "/api/v1/devices",
            json={
                "name": "Synthetic Camera",
                "host": "192.0.2.30",
                "kind": "camera",
                "protocols": ["rtsp"],
            },
            headers=_headers(administrator),
        )
        assert enrolled.status_code == 201
        recording_id = uuid4()
        asyncio.run(_write_recording_pair(root, recording_id, UUID(enrolled.json()["id"])))

        response = client.get(
            f"/api/v1/operator/recordings/{recording_id}/export",
            headers=_headers(viewer),
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("multipart/mixed; boundary=k5-")
    assert int(response.headers["content-length"]) == len(response.content)
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-k5-recording-id"] == str(recording_id)
    assert response.headers["x-k5-descriptor-validated"] == "true"
    assert b"application/json" in response.content
    assert b"application/vnd.k5.recording" in response.content
    assert b"K5RTPF\x00\x01" in response.content
    assert str(recording_id).encode("ascii") in response.content
    assert b"192.0.2.30" not in response.content
    assert b"rtsp://" not in response.content.lower()
    assert str(tmp_path).encode().lower() not in response.content.lower()
    assert application.state.operator_export_coordinator.active_exports == 0


def test_export_rejects_corrupted_recording_before_media_response(
    monkeypatch, tmp_path: Path
) -> None:
    _configure_user_state(monkeypatch, tmp_path)
    root = tmp_path / "recordings"
    application = create_app(
        control_plane_site_id=_SITE_ID,
        device_db_path=tmp_path / "corrupt-devices.sqlite3",
        operator_recording_root=root,
    )

    with TestClient(application) as client:
        viewer = _issue_session(client, "corrupt-viewer", "viewer")
        administrator = _issue_session(client, "corrupt-administrator", "administrator")
        enrolled = client.post(
            "/api/v1/devices",
            json={
                "name": "Synthetic Camera",
                "host": "192.0.2.31",
                "kind": "camera",
                "protocols": ["rtsp"],
            },
            headers=_headers(administrator),
        )
        assert enrolled.status_code == 201
        recording_id = uuid4()
        asyncio.run(_write_recording_pair(root, recording_id, UUID(enrolled.json()["id"])))
        recording_path = root / f"{recording_id}.k5r"
        corrupted = bytearray(recording_path.read_bytes())
        corrupted[-1] ^= 0x01
        recording_path.write_bytes(corrupted)

        response = client.get(
            f"/api/v1/operator/recordings/{recording_id}/export",
            headers=_headers(viewer),
        )

    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/json")
    assert b"K5RTPF\x00\x01" not in response.content
    assert application.state.operator_export_coordinator.active_exports == 0


def test_export_route_requires_human_session(monkeypatch, tmp_path: Path) -> None:
    _configure_user_state(monkeypatch, tmp_path)
    application = create_app(
        control_plane_site_id=_SITE_ID,
        device_db_path=tmp_path / "unauthorized-devices.sqlite3",
        operator_recording_root=tmp_path / "recordings",
    )

    with TestClient(application) as client:
        response = client.get(f"/api/v1/operator/recordings/{uuid4()}/export")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_export_enforces_byte_and_concurrency_limits(tmp_path: Path) -> None:
    async def exercise() -> None:
        registry = DeviceRegistry(
            database_path=tmp_path / "coordinator.sqlite3",
            site_id="export-coordinator",
        )
        device = registry.enroll(
            DeviceCreate(
                name="Synthetic Camera",
                host="192.0.2.32",
                kind=DeviceKind.CAMERA,
                protocols={DeviceProtocol.RTSP},
            )
        ).device
        root = tmp_path / "recordings"
        recording_id = uuid4()
        await _write_recording_pair(root, recording_id, device.id)
        tiny = BoundedOperatorExportCoordinator(registry, root, max_export_bytes=32)
        single = BoundedOperatorExportCoordinator(
            registry,
            root,
            max_export_bytes=4096,
            max_active_exports=1,
        )
        try:
            with pytest.raises(OperatorExportError) as too_large:
                await tiny.begin_export(_principal(), recording_id)
            assert too_large.value.code == OperatorExportErrorCode.EXPORT_TOO_LARGE
            assert tiny.active_exports == 0

            first = await single.begin_export(_principal(), recording_id)
            assert single.active_exports == 1
            with pytest.raises(OperatorExportError) as busy:
                await single.begin_export(_principal(), recording_id)
            assert busy.value.code == OperatorExportErrorCode.EXPORT_BUSY
            assert single.active_exports == 1
            await single.release()
            assert single.active_exports == 0
            assert first.recording_id == recording_id
        finally:
            registry.close()

    asyncio.run(exercise())
