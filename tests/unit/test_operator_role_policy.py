from __future__ import annotations

import asyncio
import json
import os
from uuid import UUID

import pytest
import test_operator_admin_api as admin_helpers
import test_operator_product_api as helpers

from k5vision.operator_component_state import ComponentStateAnchor, ComponentStateError
from k5vision.operator_role_policy import OperatorRolePolicy, RolePolicyError

product = helpers.product
pytestmark = pytest.mark.parametrize("product", [{"roles": True}], indirect=True)
_ROLES = "/api/v1/operator/admin/roles"


def _role(product, *, name="Camera manager", base="administrator", permissions=None):
    response = product.client.post(
        _ROLES,
        headers=product.headers,
        json={
            "name": name,
            "description": "Delegated responsibilities",
            "base_role": base,
            "permissions": permissions
            if permissions is not None
            else ["cameras.view_live", "cameras.manage"],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _account(product, *, role="viewer", username="delegated-user"):
    issue = admin_helpers._create(product, role=role, username=username, mode="all")
    admin_helpers._activate(product, issue)
    return UUID(issue["account"]["id"])


def _assign(product, user_id, role_id):
    revision = product.client.get(_ROLES, headers=product.headers).json()["revision"]
    return product.client.put(
        f"/api/v1/operator/admin/users/{user_id}/role-profile",
        headers=product.headers,
        json={"role_profile_id": role_id, "expected_revision": revision},
    )


def _headers(product, user_id):
    token, _ = asyncio.run(product.sessions.issue(product.users.get(user_id)))
    return {"Authorization": "Bearer " + token}


def test_delegated_camera_manager_cannot_manage_people_roles_or_recording(product):
    role = _role(product)["role"]
    user = _account(product)
    assert _assign(product, user, role["id"]).status_code == 200
    headers = _headers(product, user)
    result = product.client.get("/api/v1/operator/capabilities", headers=headers).json()
    assert result["account"]["role"] == "administrator"
    assert result["account"]["role_profile_id"] == role["id"]
    assert result["can_manage_cameras"] is True and result["can_manage_users"] is False
    assert result["can_export"] is False and result["can_view_history"] is False
    assert product.client.get("/api/v1/operator/admin/users", headers=headers).status_code == 403
    assert product.client.get(_ROLES, headers=headers).status_code == 403
    enrolled = product.client.post(
        "/api/v1/operator/cameras",
        headers=headers,
        json={
            "name": "Delegated camera",
            "host": "127.0.0.1",
            "source_uri": "rtsp://test:private-camera-value@127.0.0.1/delegated",
        },
    )
    assert enrolled.status_code == 201
    camera = enrolled.json()["device_id"]
    assert (
        product.client.put(
            f"/api/v1/operator/cameras/{camera}/recording", headers=headers, json={"enabled": False}
        ).status_code
        == 403
    )


def test_cannot_replace_last_full_administrator_with_restricted_admin(product):
    role = _role(product)["role"]
    assert _assign(product, product.admin.id, role["id"]).status_code == 409
    assert product.app.state.operator_roles.account_fields(product.admin)["role_profile_id"] is None
    delegated = _account(product, role="administrator")
    assert _assign(product, delegated, role["id"]).status_code == 200
    response = product.client.patch(
        f"/api/v1/operator/admin/users/{product.admin.id}",
        headers=product.headers,
        json={"enabled": False},
    )
    assert response.status_code == 409
    response = product.client.post(
        f"/api/v1/operator/admin/users/{product.admin.id}/reset-password",
        headers=product.headers,
        json={},
    )
    assert response.status_code == 409
    assert product.users.credential_status(product.admin.id) == "active"


def test_revision_conflict_profile_in_use_and_base_role_immutability(product):
    created = _role(product)
    role, revision = created["role"], created["revision"]
    user = _account(product)
    assert _assign(product, user, role["id"]).status_code == 200
    assert (
        product.client.delete(
            _ROLES + "/" + role["id"],
            headers=product.headers,
            params={"expected_revision": revision},
        ).status_code
        == 409
    )
    revision = product.app.state.operator_roles.snapshot()["revision"]
    response = product.client.delete(
        _ROLES + "/" + role["id"], headers=product.headers, params={"expected_revision": revision}
    )
    assert response.status_code == 409 and response.json()["detail"] == "roles_in_use"
    body = {k: v for k, v in role.items() if k != "id"}
    body.update(base_role="viewer", permissions=["cameras.view_live"], expected_revision=revision)
    assert (
        product.client.patch(
            _ROLES + "/" + role["id"], headers=product.headers, json=body
        ).status_code
        == 409
    )


def test_policy_restart_preserves_restrictions_and_session_revocation(product):
    role = _role(product, base="operator", permissions=["cameras.view_live"])["role"]
    user = _account(product, role="operator")
    stale = _headers(product, user)
    assert _assign(product, user, role["id"]).status_code == 200
    assert product.client.get("/api/v1/operator/cameras", headers=stale).status_code == 401
    old = product.app.state.operator_roles
    root = old._root
    old.close()
    restored = OperatorRolePolicy(root, product.service.site_id)
    product.app.state.operator_roles = restored
    try:
        permissions = restored.permissions(product.users.get(user))
        assert permissions == {"cameras.view_live"}
        fresh = _headers(product, user)
        camera = product.enroll()
        assert (
            product.client.get(
                f"/api/v1/operator/cameras/{camera}/history", headers=fresh
            ).status_code
            == 403
        )
        assert (
            product.client.post(
                f"/api/v1/operator/cameras/{camera}/exports",
                headers=fresh,
                json={"start": "2026-10-31T12:00:00Z", "duration": 1},
            ).status_code
            == 403
        )
    finally:
        restored.close()


def test_private_policy_replacement_is_not_admitted(product):
    policy = product.app.state.operator_roles
    path = policy._path
    raw = path.read_bytes()
    temporary = path.with_suffix(".replacement")
    temporary.write_bytes(raw)
    temporary.chmod(0o600)
    temporary.replace(path)
    assert (
        product.client.get("/api/v1/operator/capabilities", headers=product.headers).status_code
        == 503
    )
    with pytest.raises(RolePolicyError):
        policy.permissions(product.admin)


def test_invalid_policy_cannot_restore_base_authority(product):
    policy = product.app.state.operator_roles
    body = json.loads(policy._path.read_text())
    body["bindings"] = {str(product.admin.id): str(product.admin.id)}
    policy._path.write_text(json.dumps(body))
    assert (
        product.client.get("/api/v1/operator/cameras", headers=product.headers).status_code == 503
    )


def test_builtin_role_change_requires_explicit_profile_removal(product):
    role = _role(product, base="operator", permissions=["cameras.view_live"])["role"]
    user = _account(product, role="operator")
    assert _assign(product, user, role["id"]).status_code == 200
    response = product.client.patch(
        f"/api/v1/operator/admin/users/{user}", headers=product.headers, json={"role": "viewer"}
    )
    assert response.status_code == 409 and response.json()["detail"] == "roles_profile_bound"
    assert _assign(product, user, None).status_code == 200
    response = product.client.patch(
        f"/api/v1/operator/admin/users/{user}", headers=product.headers, json={"role": "viewer"}
    )
    assert response.status_code == 200


def test_component_seal_refuses_linked_authority_journal_before_sqlite_open(product):
    policy = product.app.state.operator_roles
    anchor = ComponentStateAnchor(policy._root, product.service.site_id, "roles", present=True)
    unrelated = policy._root / "private-unrelated-backup"
    unrelated.write_bytes(b"preserve these unrelated bytes" * 32)
    unrelated.chmod(0o600)
    journal = policy._root / "users.sqlite3-journal"
    original_database = (policy._root / "users.sqlite3").read_bytes()
    os.link(unrelated, journal)
    try:
        with pytest.raises(ComponentStateError):
            anchor.seal()
        assert unrelated.read_bytes() == b"preserve these unrelated bytes" * 32
        assert journal.samefile(unrelated)
        assert (policy._root / "users.sqlite3").read_bytes() == original_database
    finally:
        journal.unlink()
        unrelated.unlink()
