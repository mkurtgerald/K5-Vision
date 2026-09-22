import json

import pytest
from fastapi.testclient import TestClient

from k5vision.main import (
    CONTROL_PLANE_READ_TOKEN_ENV,
    CONTROL_PLANE_SITE_ENV,
    CONTROL_PLANE_TOKEN_ENV,
    DEVICE_DB_PATH_ENV,
    create_app,
)
from k5vision.user_admin_api import (
    MAX_USER_REQUEST_BYTES,
    USER_ADMIN_TOKEN_ENV,
    USER_DB_PATH_ENV,
)

CONTROL_PLANE_TOKEN = "test-device-write-token"
ADMIN_TOKEN = "test-user-admin-token"
SITE_ID = "test-site"


@pytest.fixture(autouse=True)
def configure_control_plane(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv(CONTROL_PLANE_TOKEN_ENV, CONTROL_PLANE_TOKEN)
    monkeypatch.setenv(CONTROL_PLANE_SITE_ENV, SITE_ID)
    monkeypatch.setenv(DEVICE_DB_PATH_ENV, str(tmp_path / "devices.sqlite3"))
    monkeypatch.setenv(USER_DB_PATH_ENV, str(tmp_path / "users.sqlite3"))
    monkeypatch.setenv(USER_ADMIN_TOKEN_ENV, ADMIN_TOKEN)
    monkeypatch.delenv(CONTROL_PLANE_READ_TOKEN_ENV, raising=False)


def _headers(token: str = ADMIN_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _user_payload(username: str = "operator.one") -> dict[str, object]:
    return {
        "username": username,
        "display_name": "Operator One",
        "role": "operator",
        "enabled": True,
    }


def test_user_admin_api_fails_closed_without_admin_authentication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(USER_ADMIN_TOKEN_ENV, raising=False)

    with TestClient(create_app()) as client:
        response = client.get("/api/v1/users", headers=_headers())
        health = client.get("/api/v1/health")

    assert response.status_code == 503
    assert response.json() == {"detail": "User administration authentication is not configured"}
    assert health.status_code == 200


def test_user_admin_api_fails_closed_without_durable_user_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(USER_DB_PATH_ENV, raising=False)

    with TestClient(create_app()) as client:
        response = client.get("/api/v1/users", headers=_headers())

    assert response.status_code == 503
    assert response.json() == {"detail": "User administration state is not configured"}


def test_admin_token_must_be_distinct_from_device_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(USER_ADMIN_TOKEN_ENV, CONTROL_PLANE_TOKEN)

    with TestClient(create_app()) as client:
        users = client.get("/api/v1/users", headers=_headers(CONTROL_PLANE_TOKEN))
        devices = client.get("/api/v1/devices", headers=_headers(CONTROL_PLANE_TOKEN))

    assert users.status_code == 503
    assert users.json() == {"detail": "User administration authentication is not configured"}
    assert devices.status_code == 200
    assert devices.json() == []


def test_device_credential_cannot_administer_users_and_rejection_does_not_mutate_state() -> None:
    with TestClient(create_app()) as client:
        rejected = client.post(
            "/api/v1/users",
            json=_user_payload(),
            headers=_headers(CONTROL_PLANE_TOKEN),
        )
        remaining = client.get("/api/v1/users", headers=_headers())

    assert rejected.status_code == 401
    assert rejected.headers["www-authenticate"] == "Bearer"
    assert rejected.json() == {"detail": "Unauthorized"}
    assert remaining.status_code == 200
    assert remaining.json() == []


def test_user_admin_surface_persists_create_update_and_audit_across_restart() -> None:
    with TestClient(create_app()) as client:
        created = client.post("/api/v1/users", json=_user_payload(), headers=_headers())
        updated = client.patch(
            f"/api/v1/users/{created.json()['id']}",
            json={"display_name": "Shift Supervisor", "role": "administrator", "enabled": False},
            headers=_headers(),
        )
        listed = client.get("/api/v1/users", headers=_headers())
        audit = client.get("/api/v1/users/audit", headers=_headers())

    assert created.status_code == 201
    assert updated.status_code == 200
    assert updated.json()["username"] == "operator.one"
    assert updated.json()["display_name"] == "Shift Supervisor"
    assert updated.json()["role"] == "administrator"
    assert updated.json()["enabled"] is False
    assert listed.json() == [updated.json()]
    assert [event["action"] for event in audit.json()] == ["created", "updated"]
    assert all(event["actor"] == "control-plane-admin" for event in audit.json())
    assert audit.json()[1]["changed_fields"] == ["display_name", "role", "enabled"]

    with TestClient(create_app()) as client:
        after_restart = client.get("/api/v1/users", headers=_headers())
        audit_after_restart = client.get("/api/v1/users/audit", headers=_headers())

    assert after_restart.status_code == 200
    assert after_restart.json() == [updated.json()]
    assert audit_after_restart.json() == audit.json()


def test_duplicate_user_is_rejected_without_replacing_existing_account() -> None:
    payload = _user_payload()

    with TestClient(create_app()) as client:
        created = client.post("/api/v1/users", json=payload, headers=_headers())
        duplicate = client.post(
            "/api/v1/users",
            json={**payload, "display_name": "Replacement Attempt"},
            headers=_headers(),
        )
        remaining = client.get("/api/v1/users", headers=_headers())

    assert created.status_code == 201
    assert duplicate.status_code == 409
    assert duplicate.json() == {"detail": "Username already exists"}
    assert remaining.json() == [created.json()]


def test_user_mutation_body_is_bounded_before_model_parsing() -> None:
    payload = _user_payload()
    payload["unexpected"] = "x" * (MAX_USER_REQUEST_BYTES + 1)
    encoded = json.dumps(payload).encode("utf-8")
    assert len(encoded) > MAX_USER_REQUEST_BYTES

    with TestClient(create_app()) as client:
        response = client.post(
            "/api/v1/users",
            content=encoded,
            headers={**_headers(), "Content-Type": "application/json"},
        )
        remaining = client.get("/api/v1/users", headers=_headers())

    assert response.status_code == 413
    assert response.json() == {"detail": "Request body too large"}
    assert remaining.json() == []


def test_user_admin_api_rejects_ambiguous_authorization_headers_without_mutation() -> None:
    application = create_app()
    with TestClient(application, raise_server_exceptions=False) as client:
        response = client.post(
            "/api/v1/users",
            json=_user_payload(),
            headers=[
                ("authorization", f"Bearer {ADMIN_TOKEN}"),
                ("authorization", "Bearer wrong-token"),
            ],
        )
        remaining = client.get("/api/v1/users", headers=_headers())

    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}
    assert remaining.json() == []
