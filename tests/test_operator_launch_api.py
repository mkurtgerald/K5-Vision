"""API regressions for the human-session Stage-One live operator launch seam."""

from __future__ import annotations

from fastapi.testclient import TestClient

from k5vision.domain.devices import Device
from k5vision.main import create_app
from k5vision.operator_launch import OperatorLaunchMetrics, ResolvedLiveSource
from k5vision.user_admin_api import USER_ADMIN_TOKEN_ENV, USER_DB_PATH_ENV

_ADMIN_TOKEN = "synthetic-admin-service-token"
_SITE_ID = "synthetic-site"
_PASSWORD = "synthetic-password-12345"


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _issue_session(
    client: TestClient,
    *,
    username: str,
    role: str,
) -> tuple[str, str]:
    created = client.post(
        "/api/v1/users",
        json={
            "username": username,
            "display_name": username,
            "role": role,
            "enabled": True,
        },
        headers=_headers(_ADMIN_TOKEN),
    )
    assert created.status_code == 201

    bootstrap = client.post(
        "/api/v1/auth/bootstrap-password",
        json={
            "username": username,
            "temporary_credential": created.json()["temporary_credential"],
            "new_password": _PASSWORD,
        },
    )
    assert bootstrap.status_code == 204

    logged_in = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": _PASSWORD},
    )
    assert logged_in.status_code == 200
    return logged_in.json()["session_token"], created.json()["account"]["id"]


class _Resolver:
    def __init__(self) -> None:
        self.calls = 0
        self.tokens: list[str] = []

    async def resolve(self, device: Device, stream_token: str) -> ResolvedLiveSource:
        self.calls += 1
        self.tokens.append(stream_token)
        return ResolvedLiveSource(
            f"rtsp://operator:private-secret@{device.host}/live?transport=tcp",
            96,
        )


class _Launcher:
    def __init__(self) -> None:
        self.calls = 0

    async def run(
        self,
        source: ResolvedLiveSource,
        *,
        width: int,
        height: int,
    ) -> OperatorLaunchMetrics:
        self.calls += 1
        assert "private-secret" in source.source_uri
        assert source.payload_type == 96
        assert width == 1280
        assert height == 720
        return OperatorLaunchMetrics(
            delivered_frames=12,
            presentations=12,
            processed_controls=1,
        )


def _configure_user_state(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("K5_CONTROL_PLANE_TOKEN", raising=False)
    monkeypatch.delenv("K5_CONTROL_PLANE_READ_TOKEN", raising=False)
    monkeypatch.setenv(USER_ADMIN_TOKEN_ENV, _ADMIN_TOKEN)
    monkeypatch.setenv(USER_DB_PATH_ENV, str(tmp_path / "users.sqlite3"))


def test_viewer_session_selects_enrolled_device_and_invokes_private_launcher(
    monkeypatch,
    tmp_path,
) -> None:
    _configure_user_state(monkeypatch, tmp_path)
    resolver = _Resolver()
    launcher = _Launcher()
    application = create_app(
        control_plane_site_id=_SITE_ID,
        device_db_path=tmp_path / "devices.sqlite3",
        operator_source_resolver=resolver,
        operator_launcher=launcher,
    )

    with TestClient(application) as client:
        viewer, _viewer_id = _issue_session(client, username="viewer-user", role="viewer")
        operator, _operator_id = _issue_session(
            client,
            username="operator-user",
            role="operator",
        )
        enrolled = client.post(
            "/api/v1/devices",
            json={
                "name": "Synthetic Camera",
                "host": "192.0.2.40",
                "kind": "camera",
                "protocols": ["rtsp"],
                "tags": ["synthetic"],
            },
            headers=_headers(operator),
        )
        assert enrolled.status_code == 201

        response = client.post(
            "/api/v1/operator/live",
            json={
                "device_id": enrolled.json()["id"],
                "stream_token": "main-profile",
            },
            headers=_headers(viewer),
        )

    assert response.status_code == 200
    assert response.json() == {
        "schema_version": "2",
        "completed": True,
        "delivered_frames": 12,
        "presentations": 12,
        "processed_controls": 1,
        "analytics_enabled": False,
        "analytics_provider_submissions": 0,
        "analytics_provider_completions": 0,
        "analytics_failures": 0,
        "analytics_rendered_boxes": 0,
    }
    assert resolver.calls == 1
    assert resolver.tokens == ["main-profile"]
    assert launcher.calls == 1
    retained = response.text.casefold()
    for forbidden in (
        "rtsp://",
        "192.0.2.40",
        "private-secret",
        "main-profile",
        "source_uri",
    ):
        assert forbidden not in retained


def test_service_device_token_is_not_accepted_as_human_operator_session(
    monkeypatch,
    tmp_path,
) -> None:
    _configure_user_state(monkeypatch, tmp_path)
    resolver = _Resolver()
    launcher = _Launcher()
    application = create_app(
        control_plane_token="synthetic-device-write-token",
        control_plane_site_id=_SITE_ID,
        device_db_path=tmp_path / "devices.sqlite3",
        operator_source_resolver=resolver,
        operator_launcher=launcher,
    )

    with TestClient(application) as client:
        enrolled = client.post(
            "/api/v1/devices",
            json={
                "name": "Synthetic Camera",
                "host": "192.0.2.41",
                "kind": "camera",
                "protocols": ["rtsp"],
            },
            headers=_headers("synthetic-device-write-token"),
        )
        assert enrolled.status_code == 201
        response = client.post(
            "/api/v1/operator/live",
            json={"device_id": enrolled.json()["id"], "stream_token": "main"},
            headers=_headers("synthetic-device-write-token"),
        )

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == {"detail": "Unauthorized"}
    assert resolver.calls == 0
    assert launcher.calls == 0


def test_disabled_user_session_is_revoked_before_operator_launch(monkeypatch, tmp_path) -> None:
    _configure_user_state(monkeypatch, tmp_path)
    resolver = _Resolver()
    launcher = _Launcher()
    application = create_app(
        control_plane_site_id=_SITE_ID,
        device_db_path=tmp_path / "devices.sqlite3",
        operator_source_resolver=resolver,
        operator_launcher=launcher,
    )

    with TestClient(application) as client:
        administrator, administrator_id = _issue_session(
            client,
            username="administrator-user",
            role="administrator",
        )
        enrolled = client.post(
            "/api/v1/devices",
            json={
                "name": "Synthetic Camera",
                "host": "192.0.2.42",
                "kind": "camera",
                "protocols": ["rtsp"],
            },
            headers=_headers(administrator),
        )
        assert enrolled.status_code == 201
        disabled = client.patch(
            f"/api/v1/users/{administrator_id}",
            json={"enabled": False},
            headers=_headers(_ADMIN_TOKEN),
        )
        assert disabled.status_code == 200

        response = client.post(
            "/api/v1/operator/live",
            json={"device_id": enrolled.json()["id"], "stream_token": "main"},
            headers=_headers(administrator),
        )

    assert response.status_code == 401
    assert resolver.calls == 0
    assert launcher.calls == 0


def test_operator_launch_fails_closed_when_runtime_is_not_configured(monkeypatch, tmp_path) -> None:
    _configure_user_state(monkeypatch, tmp_path)
    application = create_app(
        control_plane_site_id=_SITE_ID,
        device_db_path=tmp_path / "devices.sqlite3",
    )

    with TestClient(application) as client:
        viewer, _viewer_id = _issue_session(client, username="viewer-user", role="viewer")
        response = client.post(
            "/api/v1/operator/live",
            json={
                "device_id": "00000000-0000-0000-0000-000000000001",
                "stream_token": "main",
            },
            headers=_headers(viewer),
        )

    assert response.status_code == 503
    assert response.json() == {"detail": "Live operator runtime is not configured"}


def test_operator_launch_request_body_is_bounded_before_parsing(monkeypatch, tmp_path) -> None:
    _configure_user_state(monkeypatch, tmp_path)
    application = create_app(
        control_plane_site_id=_SITE_ID,
        device_db_path=tmp_path / "devices.sqlite3",
        max_device_request_bytes=1024,
    )

    with TestClient(application) as client:
        response = client.post(
            "/api/v1/operator/live",
            content=b"x" * 1025,
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 413
    assert response.json() == {"detail": "Request body too large"}
