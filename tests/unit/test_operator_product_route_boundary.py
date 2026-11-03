"""Exercise the installed composition, including its inherited control-plane API."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from k5vision.identity_state import initialize_identity_state
from k5vision.main import create_app
from k5vision.operator_product import create_operator_product_app

_ADMIN = "/api/v1/operator/admin"
_PASSWORD = "product-route-boundary-password"


@pytest.fixture
def composed(tmp_path, monkeypatch):
    # Only the managed container's two system ancestors have synthetic custody.
    # Every application-owned component retains the production checks.
    if os.name == "posix":
        original = Path.lstat

        def system_ancestor(path, *args, **kwargs):
            info = original(path, *args, **kwargs)
            if path in (Path("/"), Path("/tmp")) and info.st_uid == 65534:
                values = list(info)
                values[4] = 0
                return os.stat_result(values)
            return info

        monkeypatch.setattr(Path, "lstat", system_ancestor)
    identity = initialize_identity_state(
        tmp_path / "identity", username="full-admin", password=_PASSWORD
    )

    async def start(_sources):
        pass

    async def stop():
        pass

    service = SimpleNamespace(site_id=identity.site_id, public_port=8080, start=start, stop=stop)
    app = create_operator_product_app(identity_directory=identity.directory, service=service)
    with TestClient(app, base_url="http://127.0.0.1:8080") as client:

        def login(username):
            response = client.post(
                "/api/v1/auth/login", json={"username": username, "password": _PASSWORD}
            )
            assert response.status_code == 200, response.text
            return {"Authorization": "Bearer " + response.json()["session_token"]}

        full = login("full-admin")

        def create(username, role):
            response = client.post(
                _ADMIN + "/users",
                headers=full,
                json={"username": username, "display_name": username, "role": role},
            )
            assert response.status_code == 201, response.text
            issue = response.json()
            activated = client.post(
                "/api/v1/auth/bootstrap-password",
                json={
                    "username": username,
                    "temporary_credential": issue["temporary_credential"],
                    "new_password": _PASSWORD,
                },
            )
            assert activated.status_code == 204, activated.text
            return issue["account"]

        narrow = create("narrow-admin", "administrator")
        viewer = create("site-viewer", "viewer")
        roles = app.state.operator_roles
        role = roles.upsert(
            app.state.user_registry,
            {
                "name": "Account viewer administration",
                "description": "No camera administration or authority delegation",
                "base_role": "administrator",
                "permissions": ["users.manage"],
            },
        )
        roles.assign(app.state.user_registry, UUID(narrow["id"]), role["id"])
        yield SimpleNamespace(
            app=app,
            client=client,
            full=full,
            narrow=login("narrow-admin"),
            viewer=viewer,
            full_id=app.state.user_registry.list()[0].id,
        )


def test_composed_product_removes_legacy_mutations_for_restricted_administrator(composed):
    product = composed
    users = product.app.state.user_registry
    devices = product.app.state.device_registry
    before = [account.model_dump() for account in users.list()]
    user_audit = users.audit_events()
    requests = [
        ("GET", "/api/v1/users", None),
        ("GET", "/api/v1/users/audit", None),
        (
            "POST",
            "/api/v1/users",
            {"username": "escalated", "display_name": "Escalated", "role": "administrator"},
        ),
        ("PATCH", "/api/v1/users/" + product.viewer["id"], {"role": "administrator"}),
        ("PATCH", "/api/v1/users/" + str(product.full_id), {"enabled": False}),
        ("GET", "/api/v1/devices", None),
        ("POST", "/api/v1/devices", {"name": "Unmanaged", "host": "127.0.0.2"}),
        ("GET", "/api/v1/devices/" + str(product.full_id), None),
        ("POST", "/api/v1/operator/live", {}),
        ("POST", "/api/v1/operator/recordings", {}),
        ("GET", "/api/v1/operator/recording-sessions", None),
        ("POST", "/api/v1/operator/playback-controls", {}),
    ]
    for method, path, payload in requests:
        response = product.client.request(method, path, headers=product.narrow, json=payload)
        assert response.status_code == 404, (method, path, response.text)
    assert [account.model_dump() for account in users.list()] == before
    assert users.audit_events() == user_audit
    assert devices.list() == []
    # Route retirement is structural, not merely another authorization layer.
    paths = {getattr(route, "path", None) for route in product.app.routes}
    assert not any(path in paths for _, path, _ in requests)
    assert "/docs" not in paths and "/openapi.json" not in paths


def test_composed_facades_keep_delegation_last_full_admin_and_audit(composed):
    client = composed.client
    promoted = client.patch(
        _ADMIN + "/users/" + composed.viewer["id"],
        headers=composed.narrow,
        json={"role": "administrator"},
    )
    assert promoted.status_code == 403
    enrollment = client.post(
        "/api/v1/operator/cameras",
        headers=composed.narrow,
        json={"name": "Bypass", "host": "127.0.0.2", "source_uri": "rtsp://127.0.0.2/live"},
    )
    assert enrollment.status_code == 403
    disabled = client.patch(
        _ADMIN + "/users/" + str(composed.full_id),
        headers=composed.full,
        json={"enabled": False},
    )
    assert disabled.status_code == 409 and disabled.json()["detail"] == "roles_last_administrator"
    assert composed.app.state.user_registry.get(composed.full_id).enabled
    assert composed.app.state.user_registry.get(UUID(composed.viewer["id"])).role == "viewer"
    assert composed.app.state.device_registry.list() == []
    events = composed.app.state.operator_audit.events(limit=100)["events"]
    outcomes = {(row["action"], row["outcome"], row["status_code"]) for row in events}
    assert ("user.configure", "denied", 403) in outcomes
    assert ("user.configure", "failure", 409) in outcomes
    assert ("camera.enroll", "denied", 403) in outcomes
    assert client.get("/api/v1/health").status_code == 200
    assert client.get("/api/v1/auth/me", headers=composed.full).status_code == 200
    assert client.post("/api/v1/auth/logout", headers=composed.full).status_code == 204
    assert client.get("/api/v1/auth/me", headers=composed.full).status_code == 401


def test_standalone_control_plane_keeps_legacy_contract():
    app = create_app()
    routes = {getattr(route, "path", None) for route in app.routes}
    assert {
        "/api/v1/users",
        "/api/v1/users/{user_id}",
        "/api/v1/devices",
        "/api/v1/operator/live",
        "/api/v1/operator/recordings",
    } <= routes


def test_product_does_not_inherit_future_generic_routes(tmp_path, monkeypatch):
    from fastapi import FastAPI

    from k5vision.operator_product import _retain_product_authentication_routes

    app = FastAPI()
    app.add_api_route("/api/v1/auth/login", lambda: {}, methods=["POST"])
    app.add_api_route("/api/v1/auth/login", lambda: {}, methods=["DELETE"])
    app.add_api_route("/api/v1/new-management", lambda: {}, methods=["POST"])
    app.add_api_route("/api/v1/auth/new-administrator", lambda: {}, methods=["POST"])
    _retain_product_authentication_routes(app)
    assert [(route.path, route.methods) for route in app.routes] == [
        ("/api/v1/auth/login", {"POST"})
    ]
