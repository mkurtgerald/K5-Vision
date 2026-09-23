from __future__ import annotations

from fastapi.testclient import TestClient

from k5vision.main import create_app
from k5vision.user_admin_api import USER_ADMIN_TOKEN_ENV, USER_DB_PATH_ENV

_ADMIN_TOKEN = "synthetic-admin-service-token"
_SITE_ID = "synthetic-site"
_PASSWORD = "synthetic-password-12345"


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _device(index: int) -> dict[str, object]:
    return {
        "name": f"Synthetic Camera {index}",
        "host": f"192.0.2.{index}",
        "kind": "camera",
        "protocols": ["rtsp"],
        "tags": ["synthetic"],
    }


def _issue_session(client: TestClient, *, username: str, role: str) -> str:
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
    return logged_in.json()["session_token"]


def test_human_sessions_enforce_device_role_permissions_without_service_device_token(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.delenv("K5_CONTROL_PLANE_TOKEN", raising=False)
    monkeypatch.delenv("K5_CONTROL_PLANE_READ_TOKEN", raising=False)
    monkeypatch.setenv(USER_ADMIN_TOKEN_ENV, _ADMIN_TOKEN)
    monkeypatch.setenv(USER_DB_PATH_ENV, str(tmp_path / "users.sqlite3"))

    application = create_app(
        control_plane_site_id=_SITE_ID,
        device_db_path=tmp_path / "devices.sqlite3",
    )
    with TestClient(application) as client:
        viewer = _issue_session(client, username="viewer-user", role="viewer")
        operator = _issue_session(client, username="operator-user", role="operator")
        administrator = _issue_session(
            client,
            username="administrator-user",
            role="administrator",
        )

        viewer_list = client.get("/api/v1/devices", headers=_headers(viewer))
        operator_list = client.get("/api/v1/devices", headers=_headers(operator))
        viewer_write = client.post(
            "/api/v1/devices",
            json=_device(1),
            headers=_headers(viewer),
        )
        operator_write = client.post(
            "/api/v1/devices",
            json=_device(2),
            headers=_headers(operator),
        )
        administrator_write = client.post(
            "/api/v1/devices",
            json=_device(3),
            headers=_headers(administrator),
        )
        viewer_after = client.get("/api/v1/devices", headers=_headers(viewer))
        operator_after = client.get("/api/v1/devices", headers=_headers(operator))

    assert viewer_list.status_code == 200
    assert viewer_list.json() == []
    assert operator_list.status_code == 200
    assert operator_list.json() == []
    assert viewer_write.status_code == 403
    assert viewer_write.json() == {"detail": "Insufficient device permission"}
    assert operator_write.status_code == 403
    assert operator_write.json() == {"detail": "Insufficient device permission"}
    assert administrator_write.status_code == 201
    assert [item["name"] for item in viewer_after.json()] == ["Synthetic Camera 3"]
    assert operator_after.status_code == 200
    assert operator_after.json() == viewer_after.json()


def test_invalid_human_session_cannot_use_device_api(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("K5_CONTROL_PLANE_TOKEN", raising=False)
    monkeypatch.delenv("K5_CONTROL_PLANE_READ_TOKEN", raising=False)
    monkeypatch.setenv(USER_ADMIN_TOKEN_ENV, _ADMIN_TOKEN)
    monkeypatch.setenv(USER_DB_PATH_ENV, str(tmp_path / "users.sqlite3"))

    application = create_app(
        control_plane_site_id=_SITE_ID,
        device_db_path=tmp_path / "devices.sqlite3",
    )
    with TestClient(application) as client:
        response = client.get(
            "/api/v1/devices",
            headers=_headers("not-a-valid-session-token"),
        )

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == {"detail": "Unauthorized"}
