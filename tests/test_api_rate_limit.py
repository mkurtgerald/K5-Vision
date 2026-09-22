import pytest
from fastapi.testclient import TestClient

from k5vision.main import MAX_DEVICE_RATE_LIMIT, MAX_DEVICE_RATE_WINDOW_SECONDS, create_app
from k5vision.user_admin_api import USER_ADMIN_TOKEN_ENV, USER_DB_PATH_ENV

WRITE_TOKEN = "write-token"
READ_TOKEN = "read-token"
SITE_ID = "rate-limit-test-site"
ADMIN_TOKEN = "synthetic-admin-service-token"
PASSWORD = "synthetic-password-12345"


def _headers(token: str = WRITE_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _payload(index: int) -> dict[str, object]:
    return {
        "name": f"Synthetic Camera {index}",
        "host": f"198.51.100.{index}",
        "kind": "camera",
        "protocols": ["rtsp"],
        "tags": ["synthetic"],
    }


def _app(tmp_path, **kwargs):
    return create_app(
        control_plane_token=WRITE_TOKEN,
        control_plane_read_token=READ_TOKEN,
        control_plane_site_id=SITE_ID,
        device_db_path=tmp_path / "devices.sqlite3",
        **kwargs,
    )


def _issue_viewer_session(client: TestClient, username: str) -> str:
    created = client.post(
        "/api/v1/users",
        json={
            "username": username,
            "display_name": username,
            "role": "viewer",
            "enabled": True,
        },
        headers=_headers(ADMIN_TOKEN),
    )
    assert created.status_code == 201

    activated = client.post(
        "/api/v1/auth/bootstrap-password",
        json={
            "username": username,
            "temporary_credential": created.json()["temporary_credential"],
            "new_password": PASSWORD,
        },
    )
    assert activated.status_code == 204

    logged_in = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": PASSWORD},
    )
    assert logged_in.status_code == 200
    return logged_in.json()["session_token"]


def test_device_write_rate_limit_rejects_excess_without_mutation(tmp_path) -> None:
    with TestClient(
        _app(
            tmp_path,
            device_write_rate_limit=2,
            device_read_rate_limit=10,
            device_rate_window_seconds=60.0,
        )
    ) as client:
        first = client.post("/api/v1/devices", json=_payload(1), headers=_headers())
        second = client.post("/api/v1/devices", json=_payload(2), headers=_headers())
        rejected = client.post("/api/v1/devices", json=_payload(3), headers=_headers())
        remaining = client.get("/api/v1/devices", headers=_headers())

    assert first.status_code == 201
    assert second.status_code == 201
    assert rejected.status_code == 429
    assert rejected.json() == {"detail": "Device request rate limit exceeded"}
    assert int(rejected.headers["retry-after"]) >= 1
    assert [device["name"] for device in remaining.json()] == [
        "Synthetic Camera 1",
        "Synthetic Camera 2",
    ]


def test_device_read_budgets_are_separate_by_permission(tmp_path) -> None:
    with TestClient(
        _app(
            tmp_path,
            device_write_rate_limit=10,
            device_read_rate_limit=1,
            device_rate_window_seconds=60.0,
        )
    ) as client:
        created = client.post("/api/v1/devices", json=_payload(1), headers=_headers())
        first_read_only = client.get("/api/v1/devices", headers=_headers(READ_TOKEN))
        second_read_only = client.get("/api/v1/devices", headers=_headers(READ_TOKEN))
        write_principal_read = client.get("/api/v1/devices", headers=_headers())
        health = client.get("/api/v1/health")

    assert created.status_code == 201
    assert first_read_only.status_code == 200
    assert second_read_only.status_code == 429
    assert second_read_only.headers["retry-after"]
    assert write_principal_read.status_code == 200
    assert health.status_code == 200


def test_device_read_rate_limit_is_isolated_per_human_principal(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("K5_CONTROL_PLANE_TOKEN", raising=False)
    monkeypatch.delenv("K5_CONTROL_PLANE_READ_TOKEN", raising=False)
    monkeypatch.setenv(USER_ADMIN_TOKEN_ENV, ADMIN_TOKEN)
    monkeypatch.setenv(USER_DB_PATH_ENV, str(tmp_path / "users.sqlite3"))

    application = create_app(
        control_plane_site_id=SITE_ID,
        device_db_path=tmp_path / "devices.sqlite3",
        device_write_rate_limit=10,
        device_read_rate_limit=1,
        device_rate_window_seconds=60.0,
    )
    with TestClient(application) as client:
        first_viewer = _issue_viewer_session(client, "rate-viewer-one")
        second_viewer = _issue_viewer_session(client, "rate-viewer-two")

        first_request = client.get("/api/v1/devices", headers=_headers(first_viewer))
        first_excess = client.get("/api/v1/devices", headers=_headers(first_viewer))
        second_request = client.get("/api/v1/devices", headers=_headers(second_viewer))

    assert first_request.status_code == 200
    assert first_excess.status_code == 429
    assert second_request.status_code == 200


@pytest.mark.parametrize(
    "kwargs",
    [
        {"device_read_rate_limit": 0},
        {"device_read_rate_limit": MAX_DEVICE_RATE_LIMIT + 1},
        {"device_write_rate_limit": 0},
        {"device_write_rate_limit": MAX_DEVICE_RATE_LIMIT + 1},
        {"device_rate_window_seconds": 0.5},
        {"device_rate_window_seconds": MAX_DEVICE_RATE_WINDOW_SECONDS + 1.0},
    ],
)
def test_device_rate_limit_configuration_is_bounded(kwargs) -> None:
    with pytest.raises(ValueError):
        create_app(**kwargs)
