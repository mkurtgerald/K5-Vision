"""Hosted regression for the Stage-One human-authority + live analytics acceptance seam."""

from __future__ import annotations

from fastapi.testclient import TestClient

from k5vision.domain.devices import Device
from k5vision.main import create_app
from k5vision.operator_launch import OperatorLaunchMetrics, ResolvedLiveSource
from k5vision.user_admin_api import USER_ADMIN_TOKEN_ENV, USER_DB_PATH_ENV

_USER_ADMIN_TOKEN = "synthetic-user-admin-service-token"
_DEVICE_SERVICE_TOKEN = "synthetic-device-service-token"
_SITE_ID = "synthetic-stage-one-site"
_PASSWORD = "synthetic-password-12345"


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _issue_session(
    client: TestClient,
    *,
    username: str,
    role: str,
    creator_token: str,
) -> str:
    created = client.post(
        "/api/v1/users",
        json={
            "username": username,
            "display_name": username,
            "role": role,
            "enabled": True,
        },
        headers=_headers(creator_token),
    )
    assert created.status_code == 201

    initialized = client.post(
        "/api/v1/auth/bootstrap-password",
        json={
            "username": username,
            "temporary_credential": created.json()["temporary_credential"],
            "new_password": _PASSWORD,
        },
    )
    assert initialized.status_code == 204

    logged_in = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": _PASSWORD},
    )
    assert logged_in.status_code == 200
    return logged_in.json()["session_token"]


class _Resolver:
    def __init__(self) -> None:
        self.calls = 0

    async def resolve(self, device: Device, stream_token: str) -> ResolvedLiveSource:
        self.calls += 1
        assert stream_token == "main"
        return ResolvedLiveSource(
            f"rtsp://operator:private-secret@{device.host}/live",
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
        assert width == 1280
        assert height == 720
        return OperatorLaunchMetrics(
            delivered_frames=18,
            presentations=18,
            processed_controls=0,
            analytics_enabled=True,
            analytics_provider_submissions=3,
            analytics_provider_completions=3,
            analytics_failures=0,
            analytics_rendered_boxes=2,
        )


def test_stage_one_hosted_contract_preserves_split_authority_and_rendered_box_acceptance(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.delenv("K5_CONTROL_PLANE_READ_TOKEN", raising=False)
    monkeypatch.setenv(USER_ADMIN_TOKEN_ENV, _USER_ADMIN_TOKEN)
    monkeypatch.setenv(USER_DB_PATH_ENV, str(tmp_path / "users.sqlite3"))

    resolver = _Resolver()
    launcher = _Launcher()
    application = create_app(
        control_plane_token=_DEVICE_SERVICE_TOKEN,
        control_plane_site_id=_SITE_ID,
        device_db_path=tmp_path / "devices.sqlite3",
        operator_source_resolver=resolver,
        operator_launcher=launcher,
    )

    with TestClient(application) as client:
        administrator = _issue_session(
            client,
            username="stage-one-administrator",
            role="administrator",
            creator_token=_USER_ADMIN_TOKEN,
        )
        operator = _issue_session(
            client,
            username="stage-one-operator",
            role="operator",
            creator_token=administrator,
        )

        rejected_user = client.post(
            "/api/v1/users",
            json={
                "username": "operator-cannot-administer",
                "display_name": "Rejected Operator Admin",
                "role": "viewer",
                "enabled": True,
            },
            headers=_headers(operator),
        )
        assert rejected_user.status_code == 403

        operator_enrollment = client.post(
            "/api/v1/devices",
            json={
                "name": "Rejected Operator Camera",
                "host": "192.0.2.54",
                "kind": "camera",
                "protocols": ["rtsp"],
            },
            headers=_headers(operator),
        )
        assert operator_enrollment.status_code == 403

        enrolled = client.post(
            "/api/v1/devices",
            json={
                "name": "Stage One Synthetic Camera",
                "host": "192.0.2.55",
                "kind": "camera",
                "protocols": ["rtsp"],
                "tags": ["stage-one-hosted-acceptance"],
            },
            headers=_headers(administrator),
        )
        assert enrolled.status_code == 201
        device_id = enrolled.json()["id"]

        service_attempt = client.post(
            "/api/v1/operator/live",
            json={"device_id": device_id, "stream_token": "main"},
            headers=_headers(_DEVICE_SERVICE_TOKEN),
        )
        assert service_attempt.status_code == 401
        assert resolver.calls == 0
        assert launcher.calls == 0

        launched = client.post(
            "/api/v1/operator/live",
            json={
                "device_id": device_id,
                "stream_token": "main",
                "width": 1280,
                "height": 720,
            },
            headers=_headers(operator),
        )

    assert launched.status_code == 200
    receipt = launched.json()
    assert receipt["completed"] is True
    assert receipt["delivered_frames"] >= 1
    assert receipt["presentations"] >= 1
    assert receipt["analytics_enabled"] is True
    assert receipt["analytics_provider_submissions"] >= 1
    assert receipt["analytics_provider_completions"] >= 1
    assert receipt["analytics_failures"] == 0
    assert receipt["analytics_rendered_boxes"] >= 1
    assert resolver.calls == 1
    assert launcher.calls == 1

    retained = launched.text.casefold()
    for forbidden in (
        "rtsp://",
        "192.0.2.55",
        "private-secret",
        "source_uri",
        "stream_token",
        "device_id",
        _DEVICE_SERVICE_TOKEN,
        _USER_ADMIN_TOKEN,
        _PASSWORD,
    ):
        assert forbidden.casefold() not in retained
