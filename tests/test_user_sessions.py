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
PASSWORD = "correct horse battery staple"


@pytest.fixture(autouse=True)
def configure_control_plane(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv(CONTROL_PLANE_TOKEN_ENV, CONTROL_PLANE_TOKEN)
    monkeypatch.setenv(CONTROL_PLANE_SITE_ENV, SITE_ID)
    monkeypatch.setenv(DEVICE_DB_PATH_ENV, str(tmp_path / "devices.sqlite3"))
    monkeypatch.setenv(USER_DB_PATH_ENV, str(tmp_path / "users.sqlite3"))
    monkeypatch.setenv(USER_ADMIN_TOKEN_ENV, ADMIN_TOKEN)
    monkeypatch.delenv(CONTROL_PLANE_READ_TOKEN_ENV, raising=False)


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _user(username: str, role: str) -> dict[str, object]:
    return {
        "username": username,
        "display_name": username.replace(".", " ").title(),
        "role": role,
        "enabled": True,
    }


def _create_initialized_user(client: TestClient, *, username: str, role: str) -> dict[str, object]:
    created = client.post(
        "/api/v1/users",
        json=_user(username, role),
        headers=_headers(ADMIN_TOKEN),
    )
    assert created.status_code == 201
    body = created.json()
    activated = client.post(
        "/api/v1/auth/bootstrap-password",
        json={
            "username": username,
            "temporary_credential": body["temporary_credential"],
            "new_password": PASSWORD,
        },
    )
    assert activated.status_code == 204
    return body["account"]


def _login(client: TestClient, username: str, password: str = PASSWORD) -> dict[str, object]:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": password},
    )
    assert response.status_code == 200
    return response.json()


def test_initialized_admin_can_login_and_use_same_bounded_admin_surface() -> None:
    with TestClient(create_app()) as client:
        _create_initialized_user(client, username="admin.one", role="administrator")
        session = _login(client, "admin.one")
        token = str(session["session_token"])

        me = client.get("/api/v1/auth/me", headers=_headers(token))
        created = client.post(
            "/api/v1/users",
            json=_user("operator.two", "operator"),
            headers=_headers(token),
        )
        audit = client.get("/api/v1/users/audit", headers=_headers(token))
        logout = client.post("/api/v1/auth/logout", headers=_headers(token))
        after_logout = client.get("/api/v1/auth/me", headers=_headers(token))

    assert me.status_code == 200
    assert me.json()["username"] == "admin.one"
    assert created.status_code == 201
    assert created.json()["account"]["username"] == "operator.two"
    assert created.json()["temporary_credential"] not in audit.text
    assert audit.status_code == 200
    assert audit.json()[-2]["action"] == "created"
    assert audit.json()[-2]["actor"] == "admin.one"
    assert audit.json()[-1]["action"] == "bootstrap-issued"
    assert audit.json()[-1]["actor"] == "admin.one"
    assert logout.status_code == 204
    assert after_logout.status_code == 401


def test_non_admin_session_is_authenticated_but_cannot_administer_users() -> None:
    with TestClient(create_app()) as client:
        _create_initialized_user(client, username="operator.one", role="operator")
        session = _login(client, "operator.one")
        token = str(session["session_token"])

        me = client.get("/api/v1/auth/me", headers=_headers(token))
        rejected = client.post(
            "/api/v1/users",
            json=_user("operator.two", "operator"),
            headers=_headers(token),
        )
        remaining = client.get("/api/v1/users", headers=_headers(ADMIN_TOKEN))

    assert me.status_code == 200
    assert me.json()["role"] == "operator"
    assert rejected.status_code == 403
    assert rejected.json() == {"detail": "Insufficient user administration permission"}
    assert [user["username"] for user in remaining.json()] == ["operator.one"]


def test_authority_change_revokes_existing_human_session() -> None:
    with TestClient(create_app()) as client:
        account = _create_initialized_user(client, username="admin.one", role="administrator")
        session = _login(client, "admin.one")
        token = str(session["session_token"])

        changed = client.patch(
            f"/api/v1/users/{account['id']}",
            json={"role": "operator"},
            headers=_headers(ADMIN_TOKEN),
        )
        after_change = client.get("/api/v1/auth/me", headers=_headers(token))

    assert changed.status_code == 200
    assert changed.json()["role"] == "operator"
    assert after_change.status_code == 401


def test_password_identity_survives_restart_but_sessions_do_not() -> None:
    with TestClient(create_app()) as client:
        _create_initialized_user(client, username="admin.one", role="administrator")
        first = _login(client, "admin.one")
        first_token = str(first["session_token"])

    with TestClient(create_app()) as client:
        stale = client.get("/api/v1/auth/me", headers=_headers(first_token))
        second = _login(client, "admin.one")
        current = client.get(
            "/api/v1/auth/me",
            headers=_headers(str(second["session_token"])),
        )

    assert stale.status_code == 401
    assert current.status_code == 200
    assert current.json()["username"] == "admin.one"


def test_wrong_password_is_rejected_without_issuing_session() -> None:
    with TestClient(create_app()) as client:
        _create_initialized_user(client, username="operator.one", role="operator")
        wrong = client.post(
            "/api/v1/auth/login",
            json={"username": "operator.one", "password": "definitely-wrong"},
        )

    assert wrong.status_code == 401
    assert wrong.json() == {"detail": "Invalid username or password"}
    assert "session_token" not in wrong.text


def test_login_body_is_bounded_before_password_verification() -> None:
    encoded = json.dumps(
        {
            "username": "operator.one",
            "password": "x" * (MAX_USER_REQUEST_BYTES + 1),
        }
    ).encode("utf-8")
    assert len(encoded) > MAX_USER_REQUEST_BYTES

    with TestClient(create_app()) as client:
        response = client.post(
            "/api/v1/auth/login",
            content=encoded,
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 413
    assert response.json() == {"detail": "Request body too large"}
