from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlencode
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from k5vision.auth_sessions import UserSessionManager
from k5vision.domain.devices import DeviceCreate, DeviceProtocol
from k5vision.domain.users import UserAccount, UserCreate, UserPatch, UserRole
from k5vision.identity_state import initialize_identity_state
from k5vision.media.mediamtx_provider import (
    MediaMTXBinding,
    MediaPathConfiguration,
    MediaPathState,
    MediaPlaybackSpan,
    MediaWorkerError,
    MediaWorkerErrorCode,
)
from k5vision.operator_product_api import install_operator_product_api
from k5vision.operator_product_state import OperatorCameraStore
from k5vision.services.device_registry import DeviceRegistry
from k5vision.services.user_registry import UserRegistry


class _Provider:
    online = True
    available = True
    recording = True
    error = None

    def __init__(self):
        self.history_queries = []

    async def path_state(self, binding):
        if self.error:
            raise MediaWorkerError(self.error)
        return MediaPathState(
            binding.device_id, "main", self.available, self.online, (), 1, 123, 456
        )

    async def path_configuration(self, binding):
        return MediaPathConfiguration(binding.device_id, "main", self.recording, False, "fmp4")

    async def playback_spans(self, binding, *, start, end):
        self.history_queries.append((binding, start, end))
        if self.error:
            raise MediaWorkerError(self.error)
        return (MediaPlaybackSpan("record-1", binding.device_id, "main", start, 60.0),)


class _Service:
    def __init__(self, site_id):
        self.site_id = site_id
        self.provider = _Provider()
        self.applied = []
        self.worker_status = "ready"

    def status(self):
        return self.worker_status

    async def apply_sources(self, sources):
        self.applied.append(sources)


@dataclass
class _Harness:
    client: TestClient
    app: FastAPI
    cameras: OperatorCameraStore
    service: _Service
    registry: DeviceRegistry
    users: UserRegistry
    sessions: UserSessionManager
    admin: UserAccount
    token: str

    @property
    def headers(self):
        return {"Authorization": "Bearer " + self.token}

    def enroll(self):
        result = self.client.post(
            "/api/v1/operator/cameras",
            headers=self.headers,
            json={
                "name": "Front door",
                "host": "127.0.0.1",
                "source_uri": "rtsp://camera:private-test-value@127.0.0.1/live",
            },
        )
        assert result.status_code == 201
        return result.json()["device_id"]

    def media_headers(self, uri, *, token=None, method="GET"):
        return {
            "Cookie": "k5_operator_media=" + (self.token if token is None else token),
            "X-Forwarded-Method": method,
            "X-Forwarded-Uri": uri,
        }


@pytest.fixture
def product(tmp_path, monkeypatch):
    # Managed execution presents the two system ancestors as nobody-owned;
    # retain actual ownership and privacy checks for every fixture component.
    if os.name == "posix":
        original_lstat = Path.lstat

        def trusted_system_ancestor(path, *args, **kwargs):
            info = original_lstat(path, *args, **kwargs)
            if path in (Path("/"), Path("/tmp")) and info.st_uid == 65534:
                values = list(info)
                values[4] = 0
                return os.stat_result(values)
            return info

        monkeypatch.setattr(Path, "lstat", trusted_system_ancestor)
    state = initialize_identity_state(
        tmp_path / "identity", username="pilot-admin", password="local-test-password"
    )
    registry = DeviceRegistry(
        site_id=state.site_id, database_path=state.directory / "devices.sqlite3"
    )
    users = UserRegistry(site_id=state.site_id, database_path=state.database_path)
    sessions = UserSessionManager()
    admin = users.list()[0]
    token, _ = asyncio.run(sessions.issue(admin))
    cameras = OperatorCameraStore(state.directory, state.site_id, registry=registry)
    service = _Service(state.site_id)
    app = FastAPI()
    app.state.device_registry = registry
    app.state.user_registry = users
    app.state.user_session_manager = sessions
    install_operator_product_api(app, cameras=cameras, service=service, public_port=8080)
    with TestClient(app, base_url="http://127.0.0.1:8080") as client:
        yield _Harness(client, app, cameras, service, registry, users, sessions, admin, token)
    cameras.close()
    registry.close()
    users.close()


def test_enrollment_persists_private_source_and_public_rows_hide_execution_material(product):
    device_id = product.enroll()
    sources = product.cameras.list_sources()
    assert len(sources) == 1
    assert sources[0].binding.device_id == UUID(device_id)
    assert sources[0].source_uri == "rtsp://camera:private-test-value@127.0.0.1/live"
    assert product.registry.get(UUID(device_id)).protocols == {DeviceProtocol.RTSP}
    assert product.service.applied == [sources]
    response = product.client.get("/api/v1/operator/cameras", headers=product.headers)
    assert response.status_code == 200
    assert response.json()["worker_status"] == "ready"
    row = response.json()["cameras"][0]
    assert row == {
        "device_id": device_id,
        "name": "Front door",
        "host": "127.0.0.1",
        "status": "live",
        "recording": True,
        "analytics_status": "disabled",
    }
    assert "private-test-value" not in response.text
    assert "rtsp://" not in response.text
    assert product.cameras.get(UUID(device_id)).binding == sources[0].binding


@pytest.mark.parametrize(
    "payload",
    [
        {"name": "Camera", "host": "127.0.0.1", "source_uri": "rtsp://u:private%ZZ@127.0.0.1/live"},
        {"name": "Camera", "host": "127.0.0.1", "source_uri": "rtsp://u:private@192.0.2.1/live"},
        {"name": "Camera", "host": "private-source-host", "source_uri": "private-source-uri"},
        {"name": "Camera", "host": "127.0.0.1", "source_uri": {"private": "secret"}},
        {"name": "Camera", "host": "127.0.0.1", "source_uri": "private", "password": "private"},
    ],
)
def test_invalid_enrollment_is_generic_and_does_not_enroll_device(product, payload):
    response = product.client.post(
        "/api/v1/operator/cameras", headers=product.headers, json=payload
    )
    assert response.status_code == 422
    assert "private" not in response.text
    assert not product.registry.list()
    assert not product.cameras.list_sources()
    assert not product.service.applied


def test_oversized_and_malformed_json_bodies_do_not_echo_credentials(product):
    response = product.client.post(
        "/api/v1/operator/cameras",
        headers={**product.headers, "Content-Type": "application/json"},
        content=b'{"source_uri":"private-source",' + b"x" * (16 * 1024),
    )
    assert response.status_code == 413
    assert "private" not in response.text
    response = product.client.post(
        "/api/v1/operator/cameras",
        headers={**product.headers, "Content-Type": "application/json"},
        content=b'{"source_uri":"private-source"',
    )
    assert response.status_code == 422
    assert "private" not in response.text


@pytest.mark.parametrize("role", [UserRole.VIEWER, UserRole.OPERATOR])
def test_read_roles_cannot_mutate_camera_sources(product, role):
    account, _, _ = product.users.create_with_bootstrap(
        UserCreate(username="read-user", display_name="Reader", role=role), actor="test"
    )
    token, _ = asyncio.run(product.sessions.issue(account))
    headers = {"Authorization": "Bearer " + token}
    assert product.client.get("/api/v1/operator/cameras", headers=headers).status_code == 200
    response = product.client.post(
        "/api/v1/operator/cameras",
        headers=headers,
        json={"name": "Door", "host": "127.0.0.1", "source_uri": "rtsp://127.0.0.1/live"},
    )
    assert response.status_code == 403
    assert not product.registry.list()


def test_media_cookie_is_host_only_strict_http_only_and_origin_checked(product):
    device_id = product.enroll()
    route = f"/api/v1/operator/cameras/{device_id}/media-session"
    rejected = product.client.post(
        route, headers={**product.headers, "Origin": "https://outside.invalid"}
    )
    assert rejected.status_code == 403
    assert "set-cookie" not in rejected.headers
    response = product.client.post(
        route, headers={**product.headers, "Origin": "http://127.0.0.1:8080"}
    )
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie and "Path=/media" in cookie
    assert "Max-Age=28800" in cookie and "Domain=" not in cookie
    assert response.json()["analytics_url"] is None
    assert response.json()["live_url"].startswith("/media/live/k5_")
    assert response.json()["playback_url"].startswith("/media/playback?path=k5_")
    assert "private" not in response.text
    assert product.client.delete("/api/v1/operator/media-session").status_code == 204
    cleared = product.client.delete("/api/v1/operator/media-session", headers=product.headers)
    assert cleared.status_code == 204
    assert "Max-Age=0" in cleared.headers["set-cookie"]


def test_logout_can_clear_revoked_media_cookie_but_cross_origin_cannot(product):
    device_id = product.enroll()
    issued = product.client.post(
        f"/api/v1/operator/cameras/{device_id}/media-session", headers=product.headers
    )
    assert issued.status_code == 200
    asyncio.run(product.sessions.revoke(product.token))
    rejected = product.client.delete(
        "/api/v1/operator/media-session",
        headers={**product.headers, "Origin": "https://outside.invalid"},
    )
    assert rejected.status_code == 403
    assert "set-cookie" not in rejected.headers
    cleared = product.client.delete(
        "/api/v1/operator/media-session",
        headers={**product.headers, "Origin": "http://127.0.0.1:8080"},
    )
    assert cleared.status_code == 204
    assert "Max-Age=0" in cleared.headers["set-cookie"]
    assert "Path=/media" in cleared.headers["set-cookie"]
    path = MediaMTXBinding(product.service.site_id, UUID(device_id)).path_name
    denied = product.client.get(
        "/api/v1/operator/media-auth",
        headers=product.media_headers(f"/media/live/{path}/index.m3u8"),
    )
    assert denied.status_code == 401


def test_logout_revokes_cookie_and_durable_disable_and_role_changes_take_effect(product):
    device_id = product.enroll()
    binding = MediaMTXBinding(product.service.site_id, UUID(device_id))
    headers = product.media_headers(f"/media/live/{binding.path_name}/index.m3u8")
    assert product.client.get("/api/v1/operator/media-auth", headers=headers).status_code == 200
    product.users.update(product.admin.id, UserPatch(enabled=False), actor="test")
    assert product.client.get("/api/v1/operator/media-auth", headers=headers).status_code == 401
    product.users.update(
        product.admin.id, UserPatch(enabled=True, role=UserRole.VIEWER), actor="test"
    )
    assert product.client.get("/api/v1/operator/media-auth", headers=headers).status_code == 200
    response = product.client.delete(
        f"/api/v1/operator/cameras/{device_id}", headers=product.headers
    )
    assert response.status_code == 403
    asyncio.run(product.sessions.revoke(product.token))
    assert product.client.get("/api/v1/operator/media-auth", headers=headers).status_code == 401


def test_duplicate_cookie_and_authorization_headers_fail_closed(product):
    device_id = product.enroll()
    binding = MediaMTXBinding(product.service.site_id, UUID(device_id))
    uri = f"/media/live/{binding.path_name}/index.m3u8"
    for cookies in [
        [("Cookie", "k5_operator_media=" + product.token)] * 2,
        [("Cookie", "k5_operator_media=" + product.token + "; k5_operator_media=" + product.token)],
    ]:
        response = product.client.get(
            "/api/v1/operator/media-auth",
            headers=cookies + [("X-Forwarded-Method", "GET"), ("X-Forwarded-Uri", uri)],
        )
        assert response.status_code == 401
    response = product.client.get(
        "/api/v1/operator/cameras", headers=[("Authorization", "Bearer " + product.token)] * 2
    )
    assert response.status_code == 401


@pytest.mark.parametrize(
    "suffix",
    [
        "index.m3u8",
        "main.m3u8?_HLS_msn=123&_HLS_part=2&_HLS_skip=YES",
        "init.mp4",
        "part123.mp4",
        "segment123.m4s",
    ],
)
def test_known_hls_reads_are_admitted(product, suffix):
    device_id = product.enroll()
    path = MediaMTXBinding(product.service.site_id, UUID(device_id)).path_name
    response = product.client.get(
        "/api/v1/operator/media-auth",
        headers=product.media_headers(f"/media/live/{path}/{suffix}"),
    )
    assert response.status_code == 200


@pytest.mark.parametrize(
    "suffix",
    [
        "../index.m3u8",
        "%2e%2e/index.m3u8",
        "%252e%252e/index.m3u8",
        "index.m3u8?unexpected=secret",
        "index.m3u8?_HLS_msn=1&_HLS_msn=2",
        "index.m3u8?_HLS_msn=-1",
        "index.m3u8?_HLS_part=99999999999999999999999",
        "index.m3u8?_HLS_skip=anything",
        "index.m3u8#fragment",
        "file.exe",
        "index.m3u8/another",
    ],
)
def test_unknown_or_traversal_hls_routes_fail_closed_without_uri_echo(product, suffix):
    device_id = product.enroll()
    path = MediaMTXBinding(product.service.site_id, UUID(device_id)).path_name
    response = product.client.get(
        "/api/v1/operator/media-auth",
        headers=product.media_headers(f"/media/live/{path}/{suffix}"),
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "Unauthorized"


def test_selected_playback_interval_is_bounded_and_foreign_path_is_denied(product):
    device_id = product.enroll()
    path = MediaMTXBinding(product.service.site_id, UUID(device_id)).path_name
    params = {"path": path, "start": "2026-10-11T12:00:00Z", "duration": "60", "format": "mp4"}
    response = product.client.get(
        "/api/v1/operator/media-auth",
        headers=product.media_headers("/media/playback?" + urlencode(params)),
    )
    assert response.status_code == 200
    cases = [
        {**params, "duration": "0"},
        {**params, "duration": "301"},
        {**params, "duration": "nan"},
        {**params, "duration": "inf"},
        {**params, "start": "2026-10-11T12:00:00"},
        {**params, "start": "9999-12-31T23:59:59Z"},
        {**params, "format": "mpegts"},
        {**params, "path": MediaMTXBinding("another-site", UUID(device_id)).path_name},
        {**params, "source_uri": "secret"},
    ]
    for case in cases:
        denied = product.client.get(
            "/api/v1/operator/media-auth",
            headers=product.media_headers("/media/playback?" + urlencode(case)),
        )
        assert denied.status_code == 401
        assert denied.json()["detail"] == "Unauthorized"
    denied = product.client.get(
        "/api/v1/operator/media-auth",
        headers=product.media_headers("/media/playback?" + urlencode(params), method="POST"),
    )
    assert denied.status_code == 401


def test_history_respects_aware_interval_and_hides_donor_errors(product):
    device_id = product.enroll()
    route = f"/api/v1/operator/cameras/{device_id}/history"
    params = {"start": "2026-10-11T12:00:00+02:00", "end": "2026-10-11T13:00:00+02:00"}
    response = product.client.get(route, headers=product.headers, params=params)
    assert response.status_code == 200
    assert response.json()["spans"] == [
        {
            "recording_id": "record-1",
            "started_at": "2026-10-11T10:00:00+00:00",
            "duration_seconds": 60,
        }
    ]
    query = product.service.provider.history_queries[0]
    assert query[1] == datetime(2026, 10, 11, 10, tzinfo=UTC)
    for values in [
        {"start": "2026-10-11T12:00:00", "end": "2026-10-11T13:00:00Z"},
        {"start": "2026-10-09T12:00:00Z", "end": "2026-10-11T13:00:00Z"},
        {"start": "2026-10-11T13:00:00Z", "end": "2026-10-11T12:00:00Z"},
    ]:
        assert product.client.get(route, headers=product.headers, params=values).status_code == 422
    product.service.provider.error = MediaWorkerErrorCode.UNAVAILABLE
    assert product.client.get(route, headers=product.headers, params=params).status_code == 503
    assert (
        product.client.get(
            f"/api/v1/operator/cameras/{uuid4()}/history", headers=product.headers
        ).status_code
        == 404
    )


def test_disk_pressure_keeps_live_and_playback_auth_but_reports_paused_recording(product):
    device_id = product.enroll()
    product.service.worker_status = "disk_full"
    product.service.provider.recording = False
    response = product.client.get("/api/v1/operator/cameras", headers=product.headers)
    assert response.json()["worker_status"] == "disk_full"
    row = response.json()["cameras"][0]
    assert row["status"] == "live" and row["recording"] is False
    path = MediaMTXBinding(product.service.site_id, UUID(device_id)).path_name
    assert (
        product.client.get(
            "/api/v1/operator/media-auth",
            headers=product.media_headers(f"/media/live/{path}/index.m3u8"),
        ).status_code
        == 200
    )
    product.service.provider.online = product.service.provider.available = False
    response = product.client.get("/api/v1/operator/cameras", headers=product.headers)
    assert response.json()["cameras"][0]["status"] == "disconnected"
    product.service.worker_status = "unavailable"
    response = product.client.get("/api/v1/operator/cameras", headers=product.headers)
    assert response.json()["cameras"][0]["status"] == "unavailable"


def test_durable_account_lookup_and_sessions_are_site_scoped(product):
    peer = UserRegistry(
        database_path=Path(product.users._database_path).with_name("peer-users.sqlite3"),
        site_id="other-site",
    )
    try:
        assert peer.get(product.admin.id) is None
        account = peer.create(
            UserCreate(username="peer-admin", display_name="Peer", role=UserRole.ADMINISTRATOR),
            actor="test",
        )
        peer_token, _ = asyncio.run(product.sessions.issue(account))
        response = product.client.get(
            "/api/v1/operator/cameras", headers={"Authorization": "Bearer " + peer_token}
        )
        assert response.status_code == 401
        assert product.users.get(product.admin.id) == product.admin
    finally:
        peer.close()


def test_unstored_enrolled_device_cannot_issue_media_cookie(product):
    device = product.registry.enroll(
        DeviceCreate(name="Other device", host="192.0.2.1", protocols={DeviceProtocol.RTSP})
    ).device
    response = product.client.post(
        f"/api/v1/operator/cameras/{device.id}/media-session", headers=product.headers
    )
    assert response.status_code == 404
    assert "set-cookie" not in response.headers
