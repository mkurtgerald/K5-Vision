from __future__ import annotations

import asyncio
import hashlib
import json
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
from k5vision.operator_analytics import OperatorAnalyticsResult
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
        self.max_recording_bytes = 20 * 1024**3
        self.storage = None

    def status(self):
        return self.worker_status

    def diagnostics(self):
        return {
            "worker_status": self.status(),
            "max_recording_bytes": self.max_recording_bytes,
            "storage": self.storage,
        }

    async def apply_sources(self, sources):
        self.applied.append(sources)


class _Analytics:
    def __init__(self):
        self.submissions = []
        self.closed = []
        self.callback = None

    async def infer(self, **submission):
        self.submissions.append({**submission, "bytes": bytes(submission["frame"].payload)})
        if self.callback is not None:
            await self.callback(submission)
        return OperatorAnalyticsResult("active", submission["frame_id"])

    async def close_owner(self, owner_id):
        self.closed.append(owner_id)


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
    analytics: _Analytics | None = None

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
def product(tmp_path, monkeypatch, request):
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
    options = getattr(request, "param", 8080)
    public_port = options if isinstance(options, int) else options.get("public_port", 8080)
    analytics = _Analytics() if isinstance(options, dict) and options.get("analytics") else None
    install_operator_product_api(
        app,
        cameras=cameras,
        service=service,
        public_port=public_port,
        analytics=analytics,
        analytics_status=options.get("analytics_status", "disabled")
        if isinstance(options, dict)
        else "disabled",
    )
    with TestClient(app, base_url=f"http://127.0.0.1:{public_port}") as client:
        yield _Harness(
            client, app, cameras, service, registry, users, sessions, admin, token, analytics
        )
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


@pytest.mark.parametrize("product,scheme", [(80, "http"), (443, "https")], indirect=["product"])
def test_media_session_accepts_omitted_default_origin_port_with_human_auth(product, scheme):
    device_id = product.enroll()
    route = f"/api/v1/operator/cameras/{device_id}/media-session"
    origin = f"{scheme}://127.0.0.1"
    denied = product.client.post(route, headers={"Origin": origin})
    assert denied.status_code == 401
    issued = product.client.post(route, headers={**product.headers, "Origin": origin})
    assert issued.status_code == 200
    assert "HttpOnly" in issued.headers["set-cookie"]
    for port in (0, 8080):
        wrong_port = product.client.post(
            route, headers={**product.headers, "Origin": origin + f":{port}"}
        )
        assert wrong_port.status_code == 403
        assert "set-cookie" not in wrong_port.headers


def test_logout_revokes_cookie_and_durable_disable_and_role_changes_take_effect(product):
    replacement, credential, _ = product.users.create_with_bootstrap(
        UserCreate(
            username="replacement-admin",
            display_name="Replacement administrator",
            role=UserRole.ADMINISTRATOR,
        ),
        actor="test",
    )
    product.users.activate_bootstrap_password(
        username=replacement.username,
        temporary_credential=credential,
        new_password="replacement administrator test password",
        actor="bootstrap-user",
    )
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


def test_status_requires_a_current_human_session_and_returns_only_safe_sample(product):
    route = "/api/v1/operator/status"
    assert product.client.get(route).status_code == 401
    product.enroll()
    product.service.storage = {
        "recording_bytes": 512,
        "free_bytes": 2 * 1024**3,
        "recording_allowed": True,
        "reason": "ready",
    }
    response = product.client.get(route, headers=product.headers)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "worker_status": "ready",
        "max_recording_bytes": 20 * 1024**3,
        "storage": product.service.storage,
        "analytics_status": "disabled",
    }
    assert "private-test-value" not in response.text
    assert "127.0.0.1" not in response.text
    assert product.token not in response.text
    alternate, temporary, _ = product.users.create_with_bootstrap(
        UserCreate(
            username="second-storage-admin", display_name="Admin", role=UserRole.ADMINISTRATOR
        ),
        actor="test",
    )
    assert product.users.activate_bootstrap_password(
        username=alternate.username,
        temporary_credential=temporary,
        new_password="second-storage-test-password",
        actor="test",
    )
    product.users.update(product.admin.id, UserPatch(enabled=False), actor="test")
    assert product.client.get(route, headers=product.headers).status_code == 401


def test_status_supports_viewer_and_reports_unknown_storage_without_guessing(product):
    viewer = product.users.create(
        UserCreate(username="storage-viewer", display_name="Viewer", role=UserRole.VIEWER),
        actor="test",
    )
    token, _ = asyncio.run(product.sessions.issue(viewer))
    product.service.worker_status = "unavailable"
    response = product.client.get(
        "/api/v1/operator/status", headers={"Authorization": "Bearer " + token}
    )
    assert response.status_code == 200
    assert response.json()["storage"] is None
    assert response.json()["worker_status"] == "unavailable"


def test_removal_revokes_media_and_history_but_preserves_endpoint_identity_for_readd(product):
    device_id = product.enroll()
    binding = MediaMTXBinding(product.service.site_id, UUID(device_id))
    media_headers = product.media_headers(f"/media/live/{binding.path_name}/index.m3u8")
    assert (
        product.client.get("/api/v1/operator/media-auth", headers=media_headers).status_code == 200
    )
    response = product.client.delete(
        f"/api/v1/operator/cameras/{device_id}", headers=product.headers
    )
    assert response.status_code == 204
    assert product.cameras.list_sources() == ()
    assert product.service.applied[-1] == ()
    assert product.registry.get(UUID(device_id)) is not None
    assert (
        product.client.get("/api/v1/operator/media-auth", headers=media_headers).status_code == 401
    )
    assert (
        product.client.get(
            f"/api/v1/operator/cameras/{device_id}/history", headers=product.headers
        ).status_code
        == 404
    )
    assert product.enroll() == device_id
    assert (
        product.client.get("/api/v1/operator/media-auth", headers=media_headers).status_code == 200
    )


def _person_headers(product, **extra):
    return {
        **product.headers,
        "Origin": "http://127.0.0.1:8080",
        "Content-Type": "application/octet-stream",
        **extra,
    }


def _person_url(device_id, *, width=2, height=2, frame_id=17):
    return (
        f"/api/v1/operator/cameras/{device_id}/person-view"
        f"?width={width}&height={height}&frame_id={frame_id}"
    )


@pytest.mark.parametrize("product", [{"analytics": True}], indirect=True)
def test_person_view_preserves_correlation_and_isolates_session_camera_identity(product):
    first = product.enroll()
    second = product.client.post(
        "/api/v1/operator/cameras",
        headers=product.headers,
        json={"name": "Other", "host": "192.0.2.10", "source_uri": "rtsp://192.0.2.10/live"},
    ).json()["device_id"]
    payload = bytes(range(16))
    for device_id in [first, second]:
        response = product.client.post(
            _person_url(device_id), headers=_person_headers(product), content=payload
        )
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json() == {"status": "active", "frame_id": 17, "boxes": []}
        assert product.token not in response.text
    submissions = product.analytics.submissions
    assert [str(item["camera_id"]) for item in submissions] == [first, second]
    assert submissions[0]["owner_id"] == UUID(
        bytes=hashlib.sha256(product.token.encode("ascii")).digest()[:16]
    )
    assert submissions[0]["owner_id"] != product.admin.id
    assert submissions[0]["bytes"] == payload
    assert submissions[0]["frame"].metadata.source_elapsed_ms == 0
    assert submissions[0]["frame"].metadata.pixel_format == "BGRx"
    assert bytes(submissions[0]["frame"].payload) == bytes(16)
    assert (
        product.client.get("/api/v1/operator/cameras", headers=product.headers).json()["cameras"][
            0
        ]["analytics_status"]
        == "configured"
    )


@pytest.mark.parametrize("product", [{"analytics": True}], indirect=True)
def test_person_view_requires_current_session_origin_and_enrolled_camera(product):
    device_id = product.enroll()
    route = _person_url(device_id)
    cases = [
        ({"Content-Type": "application/octet-stream", "Origin": "http://127.0.0.1:8080"}, 401),
        ({**product.headers, "Content-Type": "application/octet-stream"}, 403),
        (_person_headers(product, Origin="http://external.test:8080"), 403),
        (_person_headers(product, Origin="http://localhost:8080"), 403),
        (_person_headers(product, Origin="http://127.0.0.1:8090"), 403),
    ]
    for headers, expected in cases:
        assert (
            product.client.post(route, headers=headers, content=bytes(16)).status_code == expected
        )
    assert (
        product.client.post(
            _person_url(uuid4()), headers=_person_headers(product), content=bytes(16)
        ).status_code
        == 404
    )
    assert product.analytics.submissions == []


@pytest.mark.parametrize("product", [{"analytics": True}], indirect=True)
def test_person_view_rejects_geometry_types_body_lengths_and_encodings(product):
    device_id = product.enroll()
    route = _person_url(device_id)
    for suffix in [
        "width=0&height=2&frame_id=1",
        "width=961&height=2&frame_id=1",
        "width=960&height=960&frame_id=1",
        "width=2&height=2&frame_id=-1",
        "width=2&height=2&frame_id=2147483648",
        "width=2&height=2&frame_id=1&width=2",
        "width=2&height=2&frame_id=1&source=private-value",
        "width=2.0&height=2&frame_id=1",
    ]:
        response = product.client.post(
            route.split("?", 1)[0] + "?" + suffix,
            headers=_person_headers(product),
            content=bytes(16),
        )
        assert response.status_code == 422
        assert "private-value" not in response.text
    for headers, body, expected in [
        (_person_headers(product, **{"Content-Type": "image/jpeg"}), bytes(16), 415),
        (_person_headers(product, **{"Content-Encoding": "gzip"}), bytes(16), 415),
        (_person_headers(product), bytes(15), 400),
        (_person_headers(product, **{"Content-Length": "1638401"}), bytes(16), 413),
        (_person_headers(product, **{"Content-Length": "bad"}), bytes(16), 400),
    ]:
        assert product.client.post(route, headers=headers, content=body).status_code == expected
    assert product.analytics.submissions == []
    # A valid larger frame passes the special route, whereas arbitrary camera
    # mutation paths continue to use the original 16 KiB credential body gate.
    assert (
        product.client.post(
            _person_url(device_id, width=100, height=100),
            headers=_person_headers(product),
            content=bytes(40000),
        ).status_code
        == 200
    )
    assert (
        product.client.post(
            f"/api/v1/operator/cameras/{device_id}/unknown",
            headers=product.headers,
            content=bytes(40000),
        ).status_code
        == 413
    )


@pytest.mark.parametrize("product", [{"analytics": True}], indirect=True)
@pytest.mark.parametrize("revocation", ["session", "camera"])
def test_person_view_reauthorizes_after_inference_before_returning_boxes(product, revocation):
    device_id = product.enroll()

    async def revoke(_submission):
        if revocation == "session":
            await product.sessions.revoke(product.token)
        else:
            product.cameras.remove(UUID(device_id))

    product.analytics.callback = revoke
    response = product.client.post(
        _person_url(device_id), headers=_person_headers(product), content=bytes(16)
    )
    assert response.status_code == (401 if revocation == "session" else 404)
    assert "boxes" not in response.text
    assert bytes(product.analytics.submissions[0]["frame"].payload) == bytes(16)


@pytest.mark.parametrize("product", [{"analytics": True}], indirect=True)
def test_cookie_clear_closes_revoked_analytics_owner_without_admitting_media(product):
    product.enroll()
    asyncio.run(product.sessions.revoke(product.token))
    response = product.client.delete("/api/v1/operator/media-session", headers=product.headers)
    assert response.status_code == 204
    assert product.analytics.closed == [
        UUID(bytes=hashlib.sha256(product.token.encode("ascii")).digest()[:16])
    ]
    assert (
        product.client.get("/api/v1/operator/cameras", headers=product.headers).status_code == 401
    )
    product.client.delete(
        "/api/v1/operator/media-session", headers={"Authorization": "Bearer invalid"}
    )
    assert len(product.analytics.closed) == 1


@pytest.mark.parametrize("product", [{"analytics_status": "unavailable"}], indirect=True)
def test_person_configuration_failure_leaves_recording_product_available(product):
    device_id = product.enroll()
    response = product.client.get("/api/v1/operator/cameras", headers=product.headers)
    assert response.status_code == 200
    assert response.json()["cameras"][0]["recording"] is True
    assert response.json()["cameras"][0]["analytics_status"] == "unavailable"
    assert (
        product.client.post(
            _person_url(device_id), headers=_person_headers(product), content=bytes(16)
        ).status_code
        == 503
    )


async def _person_asgi(product, device_id, *, receive_frame=None, headers=None):
    reads = 0
    sent = []

    async def receive():
        nonlocal reads
        reads += 1
        if receive_frame is not None:
            return await receive_frame()
        return {"type": "http.request", "body": bytes(16), "more_body": False}

    async def send(message):
        sent.append(message)

    path, query = _person_url(device_id).split("?", 1)
    values = _person_headers(product) if headers is None else headers
    await product.app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": query.encode("ascii"),
            "headers": [
                (key.lower().encode("ascii"), value.encode("ascii"))
                for key, value in values.items()
            ],
            "server": ("127.0.0.1", 8080),
            "client": ("127.0.0.1", 12345),
            "root_path": "",
        },
        receive,
        send,
    )
    status = next(item["status"] for item in sent if item["type"] == "http.response.start")
    raw = b"".join(item.get("body", b"") for item in sent if item["type"] == "http.response.body")
    return status, reads, json.loads(raw)


@pytest.mark.parametrize("product", [{"analytics": True}], indirect=True)
def test_person_view_rejects_fifth_request_before_reading_its_body(product):
    device_id = product.enroll()

    async def concurrent_views():
        gate = asyncio.Event()

        async def wait(_submission):
            await gate.wait()

        product.analytics.callback = wait
        tasks = [asyncio.create_task(_person_asgi(product, device_id)) for _ in range(5)]
        try:
            async with asyncio.timeout(2):
                while len(product.analytics.submissions) < 4:
                    await asyncio.sleep(0)
                rejected = await tasks[4]
            assert rejected[0:2] == (429, 0)
        finally:
            gate.set()
        values = await asyncio.gather(*tasks)
        assert [value[0] for value in values] == [200, 200, 200, 200, 429]
        assert [value[1] for value in values] == [1, 1, 1, 1, 0]

    product.client.portal.call(concurrent_views)


@pytest.mark.parametrize("product", [{"analytics": True}], indirect=True)
def test_person_view_stream_bound_timeout_and_auth_precede_pixel_admission(product):
    device_id = product.enroll()

    async def streams():
        rejected = await _person_asgi(
            product, device_id, headers={"Origin": "http://127.0.0.1:8080"}
        )
        assert rejected[0:2] == (401, 0)

        async def oversize():
            return {"type": "http.request", "body": bytes(17), "more_body": True}

        async def short():
            return {"type": "http.request", "body": bytes(8), "more_body": False}

        async def delayed():
            await asyncio.sleep(10)
            return {"type": "http.request", "body": bytes(16), "more_body": False}

        assert (await _person_asgi(product, device_id, receive_frame=oversize))[0:2] == (413, 1)
        assert (await _person_asgi(product, device_id, receive_frame=short))[0:2] == (400, 1)
        assert (await _person_asgi(product, device_id, receive_frame=delayed))[0:2] == (408, 1)
        assert product.analytics.submissions == []
        # Every rejection releases its admission slot.
        assert (await _person_asgi(product, device_id))[0] == 200

    product.client.portal.call(streams)
