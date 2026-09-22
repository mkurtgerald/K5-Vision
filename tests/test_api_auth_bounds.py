from fastapi.testclient import TestClient

from k5vision.main import (
    CONTROL_PLANE_READ_TOKEN_ENV,
    CONTROL_PLANE_TOKEN_ENV,
    MAX_CONTROL_PLANE_BEARER_LENGTH,
    create_app,
)
from k5vision.user_admin_api import USER_ADMIN_TOKEN_ENV, USER_DB_PATH_ENV


def _clear_auth_environment(monkeypatch) -> None:
    for name in (
        CONTROL_PLANE_TOKEN_ENV,
        CONTROL_PLANE_READ_TOKEN_ENV,
        USER_ADMIN_TOKEN_ENV,
        USER_DB_PATH_ENV,
    ):
        monkeypatch.delenv(name, raising=False)


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


def test_overlong_configured_device_token_fails_closed(monkeypatch, tmp_path) -> None:
    _clear_auth_environment(monkeypatch)
    overlong = "w" * (MAX_CONTROL_PLANE_BEARER_LENGTH + 1)
    application = create_app(
        control_plane_token=overlong,
        control_plane_site_id="synthetic-site",
        device_db_path=tmp_path / "devices.sqlite3",
    )

    with TestClient(application) as client:
        response = client.get("/api/v1/devices", headers=_headers(overlong))

    assert response.status_code == 503
    assert response.json() == {"detail": "Control-plane authentication is not configured"}


def test_incoming_device_bearer_is_bounded_without_state_mutation(monkeypatch, tmp_path) -> None:
    _clear_auth_environment(monkeypatch)
    valid = "w" * MAX_CONTROL_PLANE_BEARER_LENGTH
    overlong = valid + "x"
    application = create_app(
        control_plane_token=valid,
        control_plane_site_id="synthetic-site",
        device_db_path=tmp_path / "devices.sqlite3",
    )

    with TestClient(application) as client:
        accepted = client.post(
            "/api/v1/devices",
            json=_device(1),
            headers=_headers(valid),
        )
        rejected = client.post(
            "/api/v1/devices",
            json=_device(2),
            headers=_headers(overlong),
        )
        listed = client.get("/api/v1/devices", headers=_headers(valid))

    assert accepted.status_code == 201
    assert rejected.status_code == 401
    assert rejected.headers["www-authenticate"] == "Bearer"
    assert rejected.json() == {"detail": "Unauthorized"}
    assert [item["name"] for item in listed.json()] == ["Synthetic Camera 1"]
