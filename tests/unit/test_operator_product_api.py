from __future__ import annotations

import asyncio
import hashlib
import http.cookiejar
import json
import os
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode
from urllib.request import Request
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from k5vision.auth_sessions import UserSessionManager
from k5vision.domain.devices import DeviceCreate, DeviceProtocol
from k5vision.domain.users import UserAccount, UserCreate, UserPatch, UserRole
from k5vision.identity_state import initialize_identity_state
from k5vision.media.mediamtx_provider import (
    MEDIAMTX_VERSION,
    MediaMTXBinding,
    MediaMTXCredentials,
    MediaMTXProvider,
    MediaPathConfiguration,
    MediaPathState,
    MediaPlaybackSpan,
    MediaWorkerError,
    MediaWorkerErrorCode,
)
from k5vision.operator_analytics import OperatorAnalyticsResult
from k5vision.operator_onvif import OnvifClockReading, OnvifError, OnvifProfile
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

    async def path_snapshot(self, bindings):
        result = {}
        for binding in bindings:
            try:
                result[binding] = (
                    await self.path_state(binding),
                    await self.path_configuration(binding),
                )
            except MediaWorkerError as error:
                result[binding] = error.code
        return result

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
        self.media_generation = 0
        self.progress = {
            "observed_bytes": 4096,
            "last_progress_age_seconds": 0,
            "inspection_complete": True,
            "observed": True,
        }
        self.preference_calls = []
        self.preference_callback = None
        self.preference_error = None

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

    def recording_progress(self, _binding):
        return dict(self.progress)

    async def apply_recording_preferences(self, sources):
        self.preference_calls.append(sources)
        if self.preference_error:
            raise self.preference_error
        self.provider.recording = sources[0].recording_enabled
        if self.preference_callback:
            await self.preference_callback()


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
        "management_port": 80,
        "management_secure": False,
        "status": "live",
        "recording": True,
        "recording_enabled": True,
        "recording_state": "recording",
        "recording_status": {
            "reason": "disk_progress",
            "observed_bytes": 4096,
            "last_progress_age_seconds": 0,
        },
        "connection_error": None,
        "analytics_status": "disabled",
    }
    assert "private-test-value" not in response.text
    assert "rtsp://" not in response.text
    assert product.cameras.get(UUID(device_id)).binding == sources[0].binding


def test_recording_pause_resume_preserves_live_source_history_and_persistent_choice(product):
    identifier = product.enroll()
    original = product.cameras.get(UUID(identifier))
    route = f"/api/v1/operator/cameras/{identifier}/recording"
    paused = product.client.put(route, headers=product.headers, json={"enabled": False})
    assert paused.status_code == 200
    assert paused.headers["cache-control"] == "no-store"
    assert paused.json()["status"] == "live"
    assert paused.json()["recording_enabled"] is False
    assert paused.json()["recording_state"] == "paused"
    assert paused.json()["recording_status"]["reason"] == "operator_paused"
    source = product.cameras.get(UUID(identifier))
    assert source.binding == original.binding and source.source_uri == original.source_uri
    assert source.recording_enabled is False
    assert (
        product.client.get(
            f"/api/v1/operator/cameras/{identifier}/history", headers=product.headers
        ).status_code
        == 200
    )
    assert (
        product.client.get(
            "/api/v1/operator/media-auth",
            headers=product.media_headers(f"/media/live/{source.binding.path_name}/index.m3u8"),
        ).status_code
        == 200
    )
    # Repeating an already committed choice does not touch a running writer.
    assert (
        product.client.put(route, headers=product.headers, json={"enabled": False}).status_code
        == 200
    )
    assert len(product.service.preference_calls) == 1
    resumed = product.client.put(route, headers=product.headers, json={"enabled": True})
    assert resumed.status_code == 200
    assert resumed.json()["recording_enabled"] is True
    assert product.cameras.get(UUID(identifier)).recording_enabled is True
    assert len(product.service.preference_calls) == 2


@pytest.mark.parametrize("role", [UserRole.VIEWER, UserRole.OPERATOR])
def test_recording_choice_requires_current_admin_and_local_origin(product, role):
    identifier = product.enroll()
    route = f"/api/v1/operator/cameras/{identifier}/recording"
    account, _, _ = product.users.create_with_bootstrap(
        UserCreate(username="recording-reader", display_name="Reader", role=role), actor="test"
    )
    token, _ = asyncio.run(product.sessions.issue(account))
    assert (
        product.client.put(
            route, headers={"Authorization": "Bearer " + token}, json={"enabled": False}
        ).status_code
        == 403
    )
    assert product.client.put(route, json={"enabled": False}).status_code == 401
    assert (
        product.client.put(
            route,
            headers={**product.headers, "Origin": "https://outside.invalid"},
            json={"enabled": False},
        ).status_code
        == 403
    )
    assert not product.service.preference_calls
    assert product.cameras.get(UUID(identifier)).recording_enabled is True


@pytest.mark.parametrize(
    "payload", [{"enabled": 1}, {"enabled": "false"}, {"enabled": False, "secret": "private"}]
)
def test_recording_choice_rejects_coercion_and_unknown_fields(product, payload):
    identifier = product.enroll()
    response = product.client.put(
        f"/api/v1/operator/cameras/{identifier}/recording",
        headers=product.headers,
        json=payload,
    )
    assert response.status_code == 422
    assert "private" not in response.text
    assert not product.service.preference_calls


def test_recording_choice_worker_failure_leaves_saved_intent_unchanged(product):
    identifier = product.enroll()
    product.service.preference_error = RuntimeError("rtsp://private-material")
    response = product.client.put(
        f"/api/v1/operator/cameras/{identifier}/recording",
        headers=product.headers,
        json={"enabled": False},
    )
    assert response.status_code == 503
    assert "private" not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert product.cameras.get(UUID(identifier)).recording_enabled is True


def test_recording_choice_store_failure_compensates_running_writer(product, monkeypatch):
    identifier = product.enroll()

    def refused(*_args):
        raise RuntimeError("private storage material")

    monkeypatch.setattr(product.cameras, "set_recording_enabled", refused)
    response = product.client.put(
        f"/api/v1/operator/cameras/{identifier}/recording",
        headers=product.headers,
        json={"enabled": False},
    )
    assert response.status_code == 503 and "private" not in response.text
    assert [sources[0].recording_enabled for sources in product.service.preference_calls] == [
        False,
        True,
    ]
    assert product.service.provider.recording is True
    assert product.cameras.get(UUID(identifier)).recording_enabled is True


def test_recording_choice_reauthenticates_after_native_work_and_compensates(product):
    identifier = product.enroll()

    async def revoke():
        await product.sessions.revoke(product.token)

    product.service.preference_callback = revoke
    response = product.client.put(
        f"/api/v1/operator/cameras/{identifier}/recording",
        headers=product.headers,
        json={"enabled": False},
    )
    assert response.status_code == 401
    assert product.cameras.get(UUID(identifier)).recording_enabled is True
    assert product.service.provider.recording is True


def test_never_live_camera_has_monotonic_deadline_and_recovers_without_reenrollment(
    product, monkeypatch
):
    clock = SimpleNamespace(value=100.0)
    monkeypatch.setattr("k5vision.operator_product_api.monotonic", lambda: clock.value)
    product.service.provider.online = product.service.provider.available = False
    identifier = product.enroll()

    def row():
        return product.client.get("/api/v1/operator/cameras", headers=product.headers).json()[
            "cameras"
        ][0]

    assert row()["status"] == "connecting"
    clock.value = 129.999
    assert row()["status"] == "connecting"
    clock.value = 130.0
    assert row()["status"] == "disconnected"
    assert row()["connection_error"] == "stream_unavailable"
    product.service.provider.online = product.service.provider.available = True
    assert row()["status"] == "live" and row()["connection_error"] is None
    product.service.provider.online = product.service.provider.available = False
    assert row()["status"] == "disconnected"
    product.service.media_generation += 1
    assert row()["status"] == "connecting"
    clock.value += 30
    assert row()["status"] == "disconnected"
    assert product.cameras.get(UUID(identifier)) is not None


def test_recording_claim_requires_recent_complete_disk_progress(product, monkeypatch):
    clock = SimpleNamespace(value=100.0)
    monkeypatch.setattr("k5vision.operator_product_api.monotonic", lambda: clock.value)
    product.service.progress["last_progress_age_seconds"] = None
    identifier = product.enroll()

    def row():
        return product.client.get("/api/v1/operator/cameras", headers=product.headers).json()[
            "cameras"
        ][0]

    assert row()["recording_state"] == "waiting"
    clock.value += 45
    assert row()["recording_state"] == "stalled"
    product.service.progress["last_progress_age_seconds"] = 0
    assert row()["recording_state"] == "recording"

    product.service.progress["last_progress_age_seconds"] = 46
    assert row()["recording_state"] == "stalled"
    product.service.progress["inspection_complete"] = False
    assert row()["recording_state"] == "error"
    product.service.progress["inspection_complete"] = True
    product.service.worker_status = "disk_full"
    assert row()["recording_state"] == "paused"
    assert row()["recording_status"]["reason"] == "storage_paused"
    product.service.worker_status = "ready"
    route = f"/api/v1/operator/cameras/{identifier}/recording"
    assert (
        product.client.put(route, headers=product.headers, json={"enabled": False}).status_code
        == 200
    )
    clock.value += 10
    product.service.progress["last_progress_age_seconds"] = 5
    resumed = product.client.put(route, headers=product.headers, json={"enabled": True})
    assert resumed.json()["recording_state"] == "waiting"
    product.service.progress["last_progress_age_seconds"] = 0
    assert row()["recording_state"] == "recording"


def test_camera_poll_keeps_worker_capacity_for_concurrent_history(product):
    """Hold an actual bounded native read while a separate history request runs."""
    for index in range(1, 5):
        response = product.client.post(
            "/api/v1/operator/cameras",
            headers=product.headers,
            json={
                "name": f"Camera {index}",
                "host": f"127.0.0.{index}",
                "source_uri": f"rtsp://127.0.0.{index}/live",
            },
        )
        assert response.status_code == 201
    sources = product.cameras.list_sources()
    entered, release = threading.Event(), threading.Event()
    lock = threading.Lock()
    state_calls, configuration_calls = [], []

    def state(source):
        return {
            "name": source.binding.path_name,
            "available": True,
            "online": True,
            "tracks2": [],
            "readers": [],
            "inboundBytes": 123,
            "outboundBytes": 456,
        }

    def configuration(source):
        return {
            "name": source.binding.path_name,
            "record": True,
            "recordFormat": "fmp4",
            "sourceOnDemand": False,
        }

    def request(request, _timeout, _limit):
        path = request.full_url.split("?", 1)[0].split(":9997", 1)[-1]
        if "/v3/paths/" in path:
            with lock:
                state_calls.append(path)
                if path == "/v3/paths/list" or len(state_calls) == 4:
                    entered.set()
            assert release.wait(5), "Controlled native state read was not released"
            if path == "/v3/paths/list":
                result = {
                    "itemCount": 4,
                    "pageCount": 1,
                    "items": [state(source) for source in sources],
                }
            else:
                result = state(
                    next(
                        source
                        for source in sources
                        if source.binding.path_name == path.rsplit("/", 1)[-1]
                    )
                )
        elif "/v3/config/paths/" in path:
            with lock:
                configuration_calls.append(path)
            if path == "/v3/config/paths/list":
                result = {
                    "itemCount": 4,
                    "pageCount": 1,
                    "items": [configuration(source) for source in sources],
                }
            else:
                result = configuration(sources[0])
        elif "/list?" in request.full_url:
            result = [{"start": "2026-10-11T12:00:00Z", "duration": 12.5}]
        else:
            result = {"version": MEDIAMTX_VERSION, "started": "2026-10-11T12:00:00Z"}
        return json.dumps(result).encode()

    history_failure_codes = []

    class ObservedProvider(MediaMTXProvider):
        async def playback_spans(self, *args, **kwargs):
            try:
                return await super().playback_spans(*args, **kwargs)
            except MediaWorkerError as error:
                history_failure_codes.append(error.code.value)
                raise

    provider = ObservedProvider(
        site_id=product.service.site_id,
        api_origin="http://127.0.0.1:9997",
        playback_origin="http://127.0.0.1:9996",
        credentials=MediaMTXCredentials("worker", "x" * 43),
        requester=request,
    )
    provider._version_admitted = True
    product.service.provider = provider
    with ThreadPoolExecutor(max_workers=1) as executor:
        poll = executor.submit(
            product.client.get, "/api/v1/operator/cameras", headers=product.headers
        )
        try:
            assert entered.wait(5), "Camera poll did not reach the native read"
            history = product.client.get(
                f"/api/v1/operator/cameras/{sources[0].binding.device_id}/history",
                headers=product.headers,
                params={"start": "2026-10-11T12:00:00Z", "end": "2026-10-11T12:01:00Z"},
            )
            assert history.status_code == 200, history_failure_codes
            assert history.json()["spans"][0]["duration_seconds"] == 12.5
        finally:
            release.set()
        assert poll.result(timeout=5).status_code == 200
    assert state_calls == ["/v3/paths/list"]
    assert configuration_calls == ["/v3/config/paths/list"]


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
    assert all(field not in cookie for field in ("Max-Age=", "Expires=", "Domain="))
    assert response.json()["analytics_url"] is None
    assert response.json()["live_url"].startswith("/media/live/k5_")
    assert response.json()["playback_url"].startswith("/media/playback?path=k5_")
    assert "private" not in response.text
    assert product.client.delete("/api/v1/operator/media-session").status_code == 204
    cleared = product.client.delete("/api/v1/operator/media-session", headers=product.headers)
    assert cleared.status_code == 204
    assert "Max-Age=0" in cleared.headers["set-cookie"]


@pytest.mark.parametrize("wall_step", [172800, -172800])
@pytest.mark.parametrize("end_session", ["expiry", "revoke"])
def test_media_cookie_survives_host_clock_steps_but_never_extends_human_session(
    product, monkeypatch, wall_step, end_session
):
    identifier = product.enroll()
    response = product.client.post(
        f"/api/v1/operator/cameras/{identifier}/media-session", headers=product.headers
    )
    assert response.status_code == 200
    deadline = next(iter(product.sessions._sessions.values())).deadline
    steady = SimpleNamespace(value=deadline - 1)
    monkeypatch.setattr("k5vision.auth_sessions.monotonic", lambda: steady.value)
    wall = SimpleNamespace(value=datetime.now(UTC).timestamp())
    monkeypatch.setattr(http.cookiejar, "time", SimpleNamespace(time=lambda: wall.value))
    headers = Message()
    headers["Set-Cookie"] = response.headers["set-cookie"]
    jar = http.cookiejar.CookieJar()
    jar.extract_cookies(
        SimpleNamespace(info=lambda: headers),
        Request(
            str(
                product.client.base_url.join(f"/api/v1/operator/cameras/{identifier}/media-session")
            )
        ),
    )

    def admitted_cookie():
        request = Request(str(product.client.base_url.join(response.json()["live_url"])))
        jar.add_cookie_header(request)
        return request.get_header("Cookie", "")

    before = admitted_cookie()
    assert before
    wall.value += wall_step
    # Use the jar's actual browser-path selection; no copied token or renewal
    # can conceal browser expiry while the authoritative session remains valid.
    after = admitted_cookie()
    same_cookie = after == before
    assert same_cookie, "Media cookie changed after the host clock correction"
    auth_headers = {
        "Cookie": after,
        "X-Forwarded-Method": "GET",
        "X-Forwarded-Uri": response.json()["live_url"],
    }
    assert (
        product.client.get("/api/v1/operator/media-auth", headers=auth_headers).status_code == 200
    )
    if end_session == "expiry":
        steady.value = deadline
    else:
        asyncio.run(product.sessions.revoke(product.token))
    unchanged_cookie = admitted_cookie() == after
    assert unchanged_cookie, "Server expiry or revocation unexpectedly rewrote the client cookie"
    assert (
        product.client.get("/api/v1/operator/media-auth", headers=auth_headers).status_code == 401
    )


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


class _OnvifConnector:
    def __init__(self):
        self.discovery_calls = 0
        self.probe_calls = []
        self.error = None
        self.callback = None
        self.candidates = [
            {"host": "127.0.0.1", "port": 8081, "secure": False, "name": "Network camera"}
        ]
        self.profiles = (
            OnvifProfile(
                "private-profile-token",
                "Main camera view",
                "H264",
                1920,
                1080,
                "rtsp://camera-user:camera-password@127.0.0.1/live?private-stream-token",
            ),
        )

    async def discover(self):
        self.discovery_calls += 1
        if self.error is not None:
            raise self.error
        return self.candidates

    async def probe(self, **request):
        self.probe_calls.append(request)
        if self.callback is not None:
            await self.callback()
        if self.error is not None:
            raise self.error
        return self.profiles


@pytest.fixture
def onvif(product):
    connector = _OnvifConnector()
    product.app.state.operator_onvif_connector = connector
    return connector


def _onvif_probe(product, **overrides):
    return product.client.post(
        "/api/v1/operator/camera-profiles",
        headers=product.headers,
        json={
            "host": "127.0.0.1",
            "port": 8081,
            "secure": False,
            "username": "camera-user",
            "password": "camera-password",
            **overrides,
        },
    )


def _onvif_connect_body(probe, *, name="Front door"):
    return {
        "probe_id": probe["probe_id"],
        "profile_id": probe["profiles"][0]["id"],
        "name": name,
    }


_UNAVAILABLE_CLOCK = {
    "status": "unavailable",
    "offset_seconds": None,
    "measured_at": None,
    "uncertainty_seconds": None,
    "reported_mode": "unknown",
    "sync_verified": False,
}


def _measured_clock(*, offset=1200.5, mode="ntp"):
    return OnvifClockReading(
        status="measured",
        offset_seconds=offset,
        measured_at=datetime(2026, 10, 18, 12, 0, tzinfo=UTC),
        uncertainty_seconds=0.25,
        reported_mode=mode,
        sync_verified=False,
    )


def test_onvif_discovery_projects_only_public_candidates(product, onvif):
    onvif.candidates[0]["source_uri"] = "rtsp://private-credential@127.0.0.1/source"
    response = product.client.post(
        "/api/v1/operator/camera-discovery", headers=product.headers, json={}
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "cameras": [{"host": "127.0.0.1", "port": 8081, "secure": False, "name": "Network camera"}],
        "status": "complete",
    }
    assert onvif.discovery_calls == 1
    assert "private" not in response.text and "rtsp" not in response.text


def test_onvif_probe_handles_hide_execution_material_and_connect_private_source(product, onvif):
    response = _onvif_probe(product)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    result = response.json()
    assert set(result) == {"probe_id", "expires_in_seconds", "profiles", "clock"}
    assert result["clock"] == _UNAVAILABLE_CLOCK
    assert len(result["probe_id"]) == 32 and 0 < result["expires_in_seconds"] <= 120
    profile = result["profiles"][0]
    assert set(profile) == {"id", "name", "codec", "width", "height", "supported"}
    assert len(profile["id"]) == 32 and profile["id"] != onvif.profiles[0].token
    assert profile["codec"] == "H264" and profile["supported"] is True
    for private in ("camera-user", "camera-password", "private-profile-token", "rtsp://"):
        assert private not in response.text
    assert not product.cameras.list_sources() and not product.registry.list()
    connected = product.client.post(
        "/api/v1/operator/cameras/connect",
        headers=product.headers,
        json=_onvif_connect_body(result),
    )
    assert connected.status_code == 201
    assert connected.headers["cache-control"] == "no-store"
    assert "rtsp://" not in connected.text and "camera-password" not in connected.text
    saved = product.cameras.list_sources()[0]
    assert saved.source_uri == onvif.profiles[0].source_uri
    assert saved.device.management_port == 8081
    assert saved.device.protocols == {DeviceProtocol.ONVIF, DeviceProtocol.RTSP}
    assert product.service.applied == [(saved,)]
    assert connected.json()["device_id"] == str(saved.device.id)
    replay = product.client.post(
        "/api/v1/operator/cameras/connect",
        headers=product.headers,
        json=_onvif_connect_body(result),
    )
    assert replay.status_code == 410 and replay.json()["detail"] == "camera_probe_expired"


@pytest.mark.parametrize(("offset", "mode"), [(1200.5, "ntp"), (-1200, "manual"), (0, "unknown")])
def test_onvif_probe_publishes_only_read_only_measured_clock(product, onvif, offset, mode):
    onvif.profiles = (replace(onvif.profiles[0], clock=_measured_clock(offset=offset, mode=mode)),)

    response = _onvif_probe(product)

    assert response.status_code == 200
    assert response.json()["clock"] == {
        "status": "measured",
        "offset_seconds": offset,
        "measured_at": "2026-10-18T12:00:00.000Z",
        "uncertainty_seconds": 0.25,
        "reported_mode": mode,
        "sync_verified": False,
    }
    assert response.headers["cache-control"] == "no-store"
    assert "camera-password" not in response.text and "rtsp://" not in response.text
    assert not product.cameras.list_sources()


@pytest.mark.parametrize(
    "changes",
    [
        {"offset_seconds": True},
        {"offset_seconds": float("nan")},
        {"offset_seconds": float("inf")},
        {"offset_seconds": 315576000001},
        {"uncertainty_seconds": -1},
        {"uncertainty_seconds": 15.1},
        {"measured_at": "2026-02-30T12:00:00Z"},
        {"measured_at": "camera-password<UTCDateTime>private-raw-time</UTCDateTime>"},
        {"reported_mode": "private-camera-mode"},
        {"sync_verified": True},
        {"raw_utc": "private-camera-time"},
    ],
)
def test_onvif_probe_sanitizes_invalid_clock_without_disclosing_remote_fields(
    product, onvif, changes
):
    report = {**_measured_clock().public(), **changes}
    reading = SimpleNamespace(public=lambda: report)
    onvif.profiles = (replace(onvif.profiles[0], clock=reading),)

    response = _onvif_probe(product)

    assert response.status_code == 200
    assert response.json()["clock"] == _UNAVAILABLE_CLOCK
    assert "private-" not in response.text and "camera-password" not in response.text
    assert "UTCDateTime" not in response.text and "raw_utc" not in response.text


def test_onvif_https_management_endpoint_is_public_and_durably_preserved(product, onvif):
    probe = _onvif_probe(product, secure=True, port=443).json()
    response = product.client.post(
        "/api/v1/operator/cameras/connect",
        headers=product.headers,
        json=_onvif_connect_body(probe),
    )
    assert response.status_code == 201
    assert response.json()["management_port"] == 443
    assert response.json()["management_secure"] is True
    saved = product.cameras.list_sources()[0]
    assert "k5-onvif-https" in saved.device.tags


@pytest.mark.parametrize("role", [UserRole.OPERATOR, UserRole.VIEWER])
def test_onvif_all_mutations_require_administrator_before_connector_use(product, onvif, role):
    account = product.users.create(
        UserCreate(username="onvif-reader", display_name="Reader", role=role), actor="test"
    )
    token, _ = asyncio.run(product.sessions.issue(account))
    for route, payload in (
        ("camera-discovery", {}),
        ("camera-profiles", {"host": "127.0.0.1"}),
        ("cameras/connect", {"probe_id": "x" * 32, "profile_id": "y" * 32, "name": "Door"}),
    ):
        response = product.client.post(
            "/api/v1/operator/" + route,
            headers={"Authorization": "Bearer " + token},
            json=payload,
        )
        assert response.status_code == 403
    assert onvif.discovery_calls == 0 and onvif.probe_calls == []


@pytest.mark.parametrize("route", ["camera-discovery", "camera-profiles", "cameras/connect"])
def test_onvif_mutations_reject_cross_origin_and_missing_human_session(product, onvif, route):
    response = product.client.post("/api/v1/operator/" + route, json={})
    assert response.status_code == 401
    response = product.client.post(
        "/api/v1/operator/" + route,
        headers={**product.headers, "Origin": "https://untrusted.invalid"},
        json={},
    )
    assert response.status_code == 403
    assert onvif.discovery_calls == 0 and onvif.probe_calls == []


@pytest.mark.parametrize(
    ("error", "status", "detail"),
    [
        (OnvifError("auth_failed"), 422, "camera_auth_failed"),
        (OnvifError("unreachable"), 503, "camera_unreachable"),
        (OnvifError("unsupported"), 422, "camera_unsupported"),
        (OnvifError("invalid_response"), 502, "camera_invalid_response"),
        (OnvifError("busy"), 429, "camera_busy"),
        (
            RuntimeError("rtsp://camera-user:camera-password@127.0.0.1/live"),
            502,
            "camera_invalid_response",
        ),
    ],
)
def test_onvif_failures_never_echo_credentials_or_logout_human(
    product, onvif, error, status, detail
):
    onvif.error = error
    response = _onvif_probe(product)
    assert response.status_code == status
    assert response.json() == {"detail": {"code": detail, "clock": _UNAVAILABLE_CLOCK}}
    assert "camera-password" not in response.text and "rtsp://" not in response.text
    assert product.client.get("/api/v1/operator/status", headers=product.headers).status_code == 200
    assert not product.app.state.operator_onvif_probes._probes


def test_onvif_failed_authentication_preserves_sanitized_clock_diagnostic(product, onvif):
    onvif.error = OnvifError("auth_failed", clock=_measured_clock())

    response = _onvif_probe(product)

    assert response.status_code == 422
    assert response.json() == {
        "detail": {
            "code": "camera_auth_failed",
            "clock": {
                "status": "measured",
                "offset_seconds": 1200.5,
                "measured_at": "2026-10-18T12:00:00.000Z",
                "uncertainty_seconds": 0.25,
                "reported_mode": "ntp",
                "sync_verified": False,
            },
        }
    }
    assert "camera-password" not in response.text and "rtsp://" not in response.text
    assert not product.app.state.operator_onvif_probes._probes
    assert not product.cameras.list_sources()


def test_onvif_failed_probe_sanitizes_invalid_clock_report(product, onvif):
    reading = SimpleNamespace(public=lambda: {"raw_utc": "private-clock-xml"})
    onvif.error = OnvifError("unreachable", clock=reading)

    response = _onvif_probe(product)

    assert response.status_code == 503
    assert response.json() == {
        "detail": {"code": "camera_unreachable", "clock": _UNAVAILABLE_CLOCK}
    }
    assert "private-clock-xml" not in response.text and "raw_utc" not in response.text


@pytest.mark.parametrize(
    "changes",
    [
        {"password": {"private-secret": "credential"}},
        {"private-secret": "credential"},
        {"host": "camera-user:camera-password@example.invalid"},
        {"port": True},
        {"secure": "false"},
    ],
)
def test_onvif_validation_has_no_password_input_or_unbounded_body_echo(product, onvif, changes):
    response = _onvif_probe(product, **changes)
    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid operator request"}
    assert not onvif.probe_calls
    response = product.client.post(
        "/api/v1/operator/camera-profiles",
        headers={**product.headers, "Content-Type": "application/json"},
        content=b'{"password":"private-secret' + b"x" * 16384,
    )
    assert response.status_code == 413 and "private-secret" not in response.text


def test_onvif_probe_is_bound_to_original_user_and_session(product, onvif):
    probe = _onvif_probe(product).json()
    peer, _credential, _expires = product.users.create_with_bootstrap(
        UserCreate(username="onvif-admin", display_name="Admin", role=UserRole.ADMINISTRATOR),
        actor="test",
    )
    peer_token, _ = asyncio.run(product.sessions.issue(peer))
    response = product.client.post(
        "/api/v1/operator/cameras/connect",
        headers={"Authorization": "Bearer " + peer_token},
        json=_onvif_connect_body(probe),
    )
    assert response.status_code == 410
    renewed, _ = asyncio.run(product.sessions.issue(product.admin))
    response = product.client.post(
        "/api/v1/operator/cameras/connect",
        headers={"Authorization": "Bearer " + renewed},
        json=_onvif_connect_body(probe),
    )
    assert response.status_code == 410
    response = product.client.post(
        "/api/v1/operator/cameras/connect",
        headers=product.headers,
        json=_onvif_connect_body(probe),
    )
    assert response.status_code == 401
    assert not product.cameras.list_sources()


def test_onvif_revocation_during_network_probe_rejects_result_and_discards_private_state(
    product, onvif
):
    async def revoke():
        await product.sessions.revoke(product.token)

    onvif.callback = revoke
    response = _onvif_probe(product)
    assert response.status_code == 401
    assert not product.app.state.operator_onvif_probes._probes
    assert not product.cameras.list_sources()


def test_onvif_revocation_during_failed_probe_withholds_clock_metadata(product, onvif):
    async def revoke():
        await product.sessions.revoke(product.token)

    onvif.callback = revoke
    onvif.error = OnvifError("auth_failed", clock=_measured_clock())

    response = _onvif_probe(product)

    assert response.status_code == 401
    assert "clock" not in response.text and "1200.5" not in response.text
    assert not product.app.state.operator_onvif_probes._probes
    assert not product.cameras.list_sources()


def test_onvif_ttl_and_capacity_bound_private_profiles_and_allow_retry(product, onvif, monkeypatch):
    import k5vision.operator_onvif_api as api

    now = [100.0]
    monkeypatch.setattr(api, "monotonic", lambda: now[0])
    original = _onvif_probe(product).json()
    for _ in range(15):
        assert _onvif_probe(product).status_code == 200
    assert _onvif_probe(product).status_code == 429
    assert len(onvif.probe_calls) == 16
    now[0] += 120
    response = product.client.post(
        "/api/v1/operator/cameras/connect",
        headers=product.headers,
        json=_onvif_connect_body(original),
    )
    assert response.status_code == 410
    assert not product.app.state.operator_onvif_probes._probes
    assert _onvif_probe(product).status_code == 200


def test_onvif_logout_cleanup_discards_private_profiles_even_after_revocation(product, onvif):
    assert _onvif_probe(product).status_code == 200
    asyncio.run(product.sessions.revoke(product.token))
    response = product.client.delete("/api/v1/operator/media-session", headers=product.headers)
    assert response.status_code == 204
    assert not product.app.state.operator_onvif_probes._probes


def test_onvif_non_h264_profiles_are_public_but_cannot_start_recording(product, onvif):
    original = onvif.profiles[0]
    onvif.profiles = (
        OnvifProfile(original.token, "H265 profile", "H265", 1920, 1080, original.source_uri),
    )
    probe = _onvif_probe(product).json()
    assert probe["profiles"][0]["supported"] is False
    response = product.client.post(
        "/api/v1/operator/cameras/connect",
        headers=product.headers,
        json=_onvif_connect_body(probe),
    )
    assert response.status_code == 422
    assert response.json() == {"detail": "camera_profile_unsupported"}
    assert not product.cameras.list_sources() and not product.service.applied


def test_onvif_remote_profile_labels_cannot_reflect_password_or_source_token(product, onvif):
    original = onvif.profiles[0]
    onvif.profiles = (
        OnvifProfile(original.token, original.source_uri, "H264", 1920, 1080, original.source_uri),
    )
    result = _onvif_probe(product)
    assert result.status_code == 200 and result.json()["profiles"][0]["name"] == "Profile 1"
    assert "camera-password" not in result.text and "rtsp://" not in result.text


def test_onvif_failed_worker_application_keeps_probe_for_retry_without_new_camera_identity(
    product, onvif, monkeypatch
):
    probe = _onvif_probe(product).json()
    original = product.service.apply_sources

    async def unavailable(_sources):
        raise RuntimeError("private worker configuration")

    monkeypatch.setattr(product.service, "apply_sources", unavailable)
    response = product.client.post(
        "/api/v1/operator/cameras/connect",
        headers=product.headers,
        json=_onvif_connect_body(probe),
    )
    assert response.status_code == 503 and "private" not in response.text
    saved = product.cameras.list_sources()[0]
    assert probe["probe_id"] in product.app.state.operator_onvif_probes._probes
    monkeypatch.setattr(product.service, "apply_sources", original)
    response = product.client.post(
        "/api/v1/operator/cameras/connect",
        headers=product.headers,
        json=_onvif_connect_body(probe),
    )
    assert response.status_code == 201
    assert response.json()["device_id"] == str(saved.device.id)
    assert len(product.registry.list()) == 1


def test_onvif_legacy_reconnect_and_removed_camera_restore_preserve_uuid_and_private_source(
    product, onvif
):
    original_id = product.enroll()
    for secure, port in ((False, 8081), (True, 8443)):
        probe = _onvif_probe(product, secure=secure, port=port).json()
        response = product.client.post(
            "/api/v1/operator/cameras/connect",
            headers=product.headers,
            json=_onvif_connect_body(probe),
        )
        assert response.status_code == 201
        assert response.json()["device_id"] == original_id
        assert response.json()["management_port"] == port
        assert response.json()["management_secure"] is secure
        source = product.cameras.list_sources()[0]
        assert source.binding.device_id == UUID(original_id)
        assert source.source_uri == onvif.profiles[0].source_uri
        assert len(product.registry.list()) == 1
        assert (
            product.client.delete(
                "/api/v1/operator/cameras/" + original_id, headers=product.headers
            ).status_code
            == 204
        )


def test_onvif_different_name_cannot_replace_an_existing_endpoint(product, onvif):
    original_id = product.enroll()
    original_source = product.cameras.list_sources()[0]
    probe = _onvif_probe(product, port=80).json()
    response = product.client.post(
        "/api/v1/operator/cameras/connect",
        headers=product.headers,
        json=_onvif_connect_body(probe, name="Another name"),
    )
    assert response.status_code == 409
    assert product.cameras.list_sources() == (original_source,)
    assert str(product.registry.list()[0].id) == original_id


def test_onvif_each_active_camera_network_is_discovered_without_loopback_or_down_interfaces(
    monkeypatch,
):
    import k5vision.operator_onvif_api as api

    def ipv4(address):
        return SimpleNamespace(family=socket.AF_INET, address=address)

    monkeypatch.setattr(
        api.psutil,
        "net_if_addrs",
        lambda: {
            "camera-ethernet": [ipv4("192.168.50.2"), ipv4("192.168.50.2")],
            "wifi": [ipv4("10.0.0.5")],
            "link-local": [ipv4("169.254.20.5")],
            "loopback": [ipv4("127.0.0.1")],
            "disabled": [ipv4("192.168.60.2")],
            "other": [ipv4("0.0.0.0"), ipv4("224.0.0.1"), ipv4("8.8.8.8")],
        },
    )
    monkeypatch.setattr(
        api.psutil,
        "net_if_stats",
        lambda: {
            name: SimpleNamespace(isup=name != "disabled") for name in api.psutil.net_if_addrs()
        },
    )
    assert set(api._camera_ipv4_interfaces()) == {"192.168.50.2", "10.0.0.5", "169.254.20.5"}


def test_onvif_interface_enumeration_failure_retains_default_route_fallback(monkeypatch):
    import k5vision.operator_onvif_api as api

    def unavailable():
        raise OSError("private interface information")

    monkeypatch.setattr(api.psutil, "net_if_addrs", unavailable)
    assert api._camera_ipv4_interfaces() == ()
