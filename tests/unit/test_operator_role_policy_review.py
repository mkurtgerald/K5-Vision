"""Independent adversarial checks for custom-role authority and restart loss."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest
import test_operator_admin_api as admin_helpers
import test_operator_oidc as oidc_helpers
import test_operator_product_api as helpers
from fastapi.testclient import TestClient

from k5vision.domain.users import UserCreate, UserRole
from k5vision.operator_product import create_operator_product_app
from k5vision.operator_role_policy import (
    BASE_PERMISSIONS,
    PERMISSIONS,
    OperatorRolePolicy,
    RolePolicyError,
)
from k5vision.services.user_registry import UserRegistryStorageError

product = helpers.product
review_identity = oidc_helpers.identity
_ADMIN = "/api/v1/operator/admin"
_ROLES = _ADMIN + "/roles"


def _role(product, permissions, *, base="operator", name="Evidence review"):
    response = product.client.post(
        _ROLES,
        headers=product.headers,
        json={
            "name": name,
            "description": "Adversarial authority fixture",
            "base_role": base,
            "permissions": permissions,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["role"]


def _assign(product, account_id, role_id):
    return product.client.put(
        _ADMIN + f"/users/{account_id}/role-profile",
        headers=product.headers,
        json={"role_profile_id": role_id},
    )


def _session(product, account_id):
    current = product.users.get(UUID(account_id))
    token, _ = asyncio.run(product.sessions.issue(current))
    return token, {"Authorization": "Bearer " + token}


def _limited_admin(product, permissions):
    issue = admin_helpers._create(
        product, role="administrator", username="review-limited-admin", mode="all"
    )
    admin_helpers._activate(product, issue)
    role = _role(product, permissions, base="administrator", name="Delegated administration")
    account_id = issue["account"]["id"]
    assert _assign(product, account_id, role["id"]).status_code == 200
    return account_id, _session(product, account_id)[1]


@pytest.mark.parametrize("product", [{"roles": True}], indirect=True)
def test_playback_only_operator_cannot_bypass_export_policy_with_direct_donor_mp4(product):
    camera = product.enroll()
    issue = admin_helpers._create(product, role="operator", camera_ids=[camera])
    admin_helpers._activate(product, issue)
    role = _role(product, ["cameras.playback"])
    account_id = issue["account"]["id"]
    assert _assign(product, account_id, role["id"]).status_code == 200
    token, headers = _session(product, account_id)
    assert admin_helpers._media(product, token, camera, export="fmp4").status_code == 200
    assert admin_helpers._media(product, token, camera).status_code == 403
    assert admin_helpers._media(product, token, camera, export="mp4").status_code == 403
    request = product.client.post(
        f"/api/v1/operator/cameras/{camera}/exports",
        headers=headers,
        json={"start": datetime.now(UTC).isoformat(), "duration": 2.0},
    )
    assert request.status_code == 403


@pytest.mark.parametrize("product", [{"roles": True}], indirect=True)
def test_lost_custom_policy_is_not_recreated_as_unrestricted_base_authority(product):
    camera = product.enroll()
    issue = admin_helpers._create(product, role="operator", camera_ids=[camera])
    admin_helpers._activate(product, issue)
    role = _role(product, ["cameras.playback"])
    account_id = issue["account"]["id"]
    assert _assign(product, account_id, role["id"]).status_code == 200
    policy = product.app.state.operator_roles
    account = product.users.get(UUID(account_id))
    assert policy.permissions(account) == {"cameras.playback"}
    root = policy._root
    policy.close()
    policy._path.unlink()
    with pytest.raises(RolePolicyError, match="roles_unavailable"):
        OperatorRolePolicy(root, product.service.site_id)


@pytest.mark.parametrize("product", [{"roles": True}], indirect=True)
def test_failed_base_role_promotion_cannot_expand_current_or_restarted_authority(
    product, monkeypatch
):
    camera = product.enroll()
    issue = admin_helpers._create(product, role="viewer", camera_ids=[camera])
    admin_helpers._activate(product, issue)
    account_id = issue["account"]["id"]
    role = _role(product, ["users.manage", "roles.manage", "storage.manage"], base="administrator")
    original = product.users.update

    def storage_failure(*args, **kwargs):
        raise UserRegistryStorageError("review fixture")

    monkeypatch.setattr(product.users, "update", storage_failure)
    response = _assign(product, account_id, role["id"])
    assert response.status_code == 503
    account = product.users.get(UUID(account_id))
    policy = product.app.state.operator_roles
    assert policy.permissions(account) <= BASE_PERMISSIONS[account.role]
    assert "users.manage" not in policy.permissions(account)
    reopened = OperatorRolePolicy(policy._root, product.service.site_id)
    try:
        assert reopened.permissions(account) == policy.permissions(account)
    finally:
        reopened.close()
    token, headers = _session(product, account_id)
    assert product.client.get(_ADMIN + "/users", headers=headers).status_code == 403
    assert admin_helpers._media(product, token, camera).status_code == 403
    monkeypatch.setattr(product.users, "update", original)


@pytest.mark.parametrize("product", [{"roles": True}], indirect=True)
def test_export_authority_is_rechecked_after_suspended_recording_lookup(product, monkeypatch):
    camera = product.enroll()
    issue = admin_helpers._create(product, role="operator", camera_ids=[camera])
    admin_helpers._activate(product, issue)
    account_id = issue["account"]["id"]
    role = _role(product, ["cameras.playback", "cameras.export"])
    assert _assign(product, account_id, role["id"]).status_code == 200
    _, headers = _session(product, account_id)
    policy = product.app.state.operator_roles

    async def revoke_during_lookup(*args, **kwargs):
        policy.upsert(
            product.users,
            {
                "name": role["name"],
                "description": role["description"],
                "base_role": role["base_role"],
                "permissions": ["cameras.playback"],
            },
            role_id=role["id"],
        )
        return ()

    monkeypatch.setattr(product.service.provider, "playback_spans", revoke_during_lookup)
    response = product.client.post(
        f"/api/v1/operator/cameras/{camera}/exports",
        headers=headers,
        json={"start": datetime.now(UTC).isoformat(), "duration": 2.0},
    )
    assert response.status_code == 403
    assert not product.app.state.operator_exports._jobs


@pytest.mark.parametrize("product", [{"roles": True}], indirect=True)
def test_role_profile_cannot_smuggle_permissions_outside_its_base_or_unknown_keys(product):
    for payload in (
        {"name": "Restricted viewer", "base_role": "viewer", "permissions": ["users.manage"]},
        {
            "name": "Invalid privilege",
            "base_role": "administrator",
            "permissions": ["private.permission"],
        },
        {"name": "Extra key", "base_role": "administrator", "permissions": [], "id": "private"},
    ):
        response = product.client.post(_ROLES, headers=product.headers, json=payload)
        assert response.status_code in {403, 422}
    assert product.app.state.operator_roles.snapshot()["roles"] == []


@pytest.mark.parametrize("product", [{"roles": True}], indirect=True)
def test_user_manager_cannot_create_an_unrestricted_administrator(product):
    _, headers = _limited_admin(
        product, ["users.manage", "cameras.view_live", "cameras.playback", "cameras.export"]
    )
    response = product.client.post(
        _ADMIN + "/users",
        headers=headers,
        json={
            "username": "review-escalated-admin",
            "display_name": "Escalation fixture",
            "role": "administrator",
            "camera_access": {"mode": "all", "camera_ids": []},
        },
    )
    assert response.status_code == 403
    assert "review-escalated-admin" not in {a.username for a in product.users.list()}
    allowed = product.client.post(
        _ADMIN + "/users",
        headers=headers,
        json={
            "username": "review-delegated-viewer",
            "display_name": "Allowed viewer",
            "role": "viewer",
            "camera_access": {"mode": "selected", "camera_ids": []},
        },
    )
    assert allowed.status_code == 201


@pytest.mark.parametrize("product", [{"roles": True}], indirect=True)
def test_user_manager_cannot_promote_an_unbound_viewer_to_full_administrator(product):
    issue = admin_helpers._create(product, username="review-unbound-viewer")
    admin_helpers._activate(product, issue)
    _, headers = _limited_admin(
        product, ["users.manage", "cameras.view_live", "cameras.playback", "cameras.export"]
    )
    account_id = issue["account"]["id"]
    response = product.client.patch(
        _ADMIN + f"/users/{account_id}", headers=headers, json={"role": "administrator"}
    )
    assert response.status_code == 403
    assert product.users.get(UUID(account_id)).role.value == "viewer"


@pytest.mark.parametrize("product", [{"roles": True}], indirect=True)
def test_user_manager_cannot_reset_and_impersonate_a_stronger_administrator(product):
    stronger = admin_helpers._create(
        product, role="administrator", username="review-strong-admin", mode="all"
    )
    _, stronger_headers = admin_helpers._activate(product, stronger)
    _, headers = _limited_admin(
        product, ["users.manage", "cameras.view_live", "cameras.playback", "cameras.export"]
    )
    target = stronger["account"]["id"]
    response = product.client.post(
        _ADMIN + f"/users/{target}/reset-password", headers=headers, json={}
    )
    assert response.status_code == 403
    assert product.users.credential_status(UUID(target)) == "active"
    assert (
        product.client.get("/api/v1/operator/capabilities", headers=stronger_headers).status_code
        == 200
    )


@pytest.mark.parametrize("product", [{"roles": True}], indirect=True)
def test_role_manager_cannot_create_or_edit_a_stronger_permission_profile(product):
    stronger = _role(
        product,
        ["users.manage", "roles.manage", "storage.manage"],
        base="administrator",
        name="Stronger profile",
    )
    _, headers = _limited_admin(product, ["roles.manage", "cameras.view_live", "cameras.playback"])
    response = product.client.post(
        _ROLES,
        headers=headers,
        json={
            "name": "Escalation profile",
            "description": "",
            "base_role": "administrator",
            "permissions": ["storage.manage"],
        },
    )
    assert response.status_code == 403
    revision = product.app.state.operator_roles.snapshot()["revision"]
    modified = product.client.patch(
        _ROLES + "/" + stronger["id"],
        headers=headers,
        json={
            "name": stronger["name"],
            "description": "",
            "base_role": "administrator",
            "permissions": ["roles.manage"],
            "expected_revision": revision,
        },
    )
    assert modified.status_code == 403
    assert product.app.state.operator_roles.snapshot()["revision"] == revision


@pytest.mark.parametrize("product", [{"roles": True}], indirect=True)
def test_role_manager_cannot_assign_a_stronger_profile_to_a_viewer(product):
    issue = admin_helpers._create(product, username="review-profile-target")
    admin_helpers._activate(product, issue)
    stronger = _role(
        product,
        ["users.manage", "roles.manage", "storage.manage"],
        base="administrator",
        name="Stronger assignment",
    )
    _, headers = _limited_admin(product, ["roles.manage", "cameras.view_live", "cameras.playback"])
    account_id = issue["account"]["id"]
    response = product.client.put(
        _ADMIN + f"/users/{account_id}/role-profile",
        headers=headers,
        json={"role_profile_id": stronger["id"]},
    )
    assert response.status_code == 403
    current = product.users.get(UUID(account_id))
    assert current.role.value == "viewer"
    assert product.app.state.operator_roles.account_fields(current)["role_profile_id"] is None


@pytest.mark.parametrize("product", [{"roles": True}], indirect=True)
@pytest.mark.parametrize(
    "statement",
    [
        "ALTER TABLE operator_component_anchors RENAME TO original_anchors;"
        "CREATE VIEW operator_component_anchors AS SELECT * FROM original_anchors;",
        "UPDATE operator_component_anchors SET site_id='foreign-site';",
        "UPDATE operator_component_anchors SET component='unknown-component';",
        "UPDATE operator_component_anchors SET schema_version=2;",
        "ALTER TABLE operator_component_anchors RENAME TO original_anchors;"
        "CREATE TABLE operator_component_anchors(site_id TEXT NOT NULL,component TEXT NOT NULL,"
        "schema_version INTEGER NOT NULL);"
        "INSERT INTO operator_component_anchors SELECT * FROM original_anchors;",
    ],
)
def test_invalid_durable_anchor_cannot_be_treated_as_first_installation(product, statement):
    policy = product.app.state.operator_roles
    root, path = policy._root, policy._path
    before = path.read_bytes()
    policy.close()
    connection = sqlite3.connect(product.users._database_path)
    try:
        connection.executescript(statement)
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(RolePolicyError, match="roles_unavailable"):
        OperatorRolePolicy(root, product.service.site_id)
    assert path.read_bytes() == before


@pytest.fixture
def composed_review(review_identity):
    """Keep real identity, product policy, auth routes, audit and middleware."""
    service = helpers._Service(review_identity.site_id)
    service.public_port = 8080

    async def start(_sources):
        pass

    async def stop():
        pass

    service.start = start
    service.stop = stop
    app = create_operator_product_app(identity_directory=review_identity.directory, service=service)
    with TestClient(app, base_url="http://127.0.0.1:8080") as client:
        owner = app.state.user_registry.list()[0]
        login = client.post(
            "/api/v1/auth/login",
            json={"username": owner.username, "password": "federation-test-password"},
        )
        assert login.status_code == 200
        yield SimpleNamespace(
            app=app,
            client=client,
            owner=owner,
            headers={"Authorization": "Bearer " + login.json()["session_token"]},
            users=app.state.user_registry,
            policy=app.state.operator_roles,
        )


def _composed_manager(value, permissions):
    account, temporary, _ = value.users.create_with_bootstrap(
        UserCreate(
            username="composed-people-manager",
            display_name="Composed people manager",
            role=UserRole.ADMINISTRATOR,
        ),
        actor=value.owner.username,
    )
    value.users.activate_bootstrap_password(
        username=account.username,
        temporary_credential=temporary,
        new_password="composed-manager-test-password",
        actor=value.owner.username,
    )
    profile = value.policy.upsert(
        value.users,
        {
            "name": "Composed restricted administration",
            "description": "Independent composed security review",
            "base_role": "administrator",
            "permissions": sorted(permissions),
        },
    )
    value.policy.assign(value.users, account.id, UUID(profile["id"]))
    response = value.client.post(
        "/api/v1/auth/login",
        json={"username": account.username, "password": "composed-manager-test-password"},
    )
    assert response.status_code == 200
    return account, {"Authorization": "Bearer " + response.json()["session_token"]}


def test_composed_sso_ceiling_preserves_stronger_session_and_durable_denials(composed_review):
    value = composed_review
    manager, headers = _composed_manager(value, {"users.manage"})
    oidc = value.app.state.operator_oidc
    oidc.store.configure(
        enabled=True,
        display_name="Independent identity fixture",
        issuer="https://identity.example",
        client_id="composed-reviewed-client",
    )
    link = oidc.store.link(subject="full-owner-subject", user_id=value.owner.id)
    provider_before, links_before = oidc.store.provider(), oidc.store.links()
    attempts = (
        value.client.put(
            _ADMIN + "/sso/provider",
            headers=headers,
            json={
                "enabled": False,
                "display_name": "Unprivileged replacement",
                "issuer": "https://replacement.example",
                "client_id": "replacement-client",
            },
        ),
        value.client.post(
            _ADMIN + "/sso/links",
            headers=headers,
            json={"subject": "manager-controlled-subject", "user_id": str(value.owner.id)},
        ),
        value.client.delete(_ADMIN + "/sso/links/" + link["id"], headers=headers),
    )
    assert [response.status_code for response in attempts] == [403, 403, 403]
    assert oidc.store.provider() == provider_before
    assert oidc.store.links() == links_before
    assert value.client.get(_ADMIN + "/users", headers=value.headers).status_code == 200
    receipt = value.client.get(
        _ADMIN + "/activity",
        headers=value.headers,
        params={"action": "security.sso.configure", "outcome": "denied"},
    )
    assert receipt.status_code == 200
    events = receipt.json()["events"]
    assert len(events) == 3
    assert {event["actor_id"] for event in events} == {str(manager.id)}
    assert {event["status_code"] for event in events} == {403}


def test_composed_product_has_no_legacy_account_or_device_takeover_route(composed_review):
    value = composed_review
    _, headers = _composed_manager(value, {"users.manage", "roles.manage"})
    users_before = [account.model_dump() for account in value.users.list()]
    attempts = (
        ("POST", "/api/v1/users", {"username": "legacy-takeover", "role": "administrator"}),
        ("PATCH", f"/api/v1/users/{value.owner.id}", {"enabled": False}),
        ("POST", "/api/v1/%75sers", {"username": "encoded-takeover", "role": "administrator"}),
        ("GET", "/api/v1/users/audit", None),
        ("GET", "/api/v1/users/", None),
        ("POST", "/api/v1/devices", {"name": "Bypass camera", "host": "192.0.2.10"}),
        ("GET", "/api/v1/devices/", None),
    )
    for method, path, payload in attempts:
        response = value.client.request(method, path, headers=headers, json=payload)
        assert response.status_code == 404, (method, path, response.text)
    assert [account.model_dump() for account in value.users.list()] == users_before
    assert value.app.state.device_registry.list() == []
    assert value.client.get("/api/v1/auth/me", headers=headers).status_code == 200
    assert value.client.get(_ADMIN + "/users", headers=value.headers).status_code == 200


@pytest.mark.parametrize("product", [{"roles": True}], indirect=True)
def test_last_full_profile_keeps_time_authority_when_only_backup_is_a_people_manager(product):
    _limited_admin(product, ["users.manage", "roles.manage"])
    profile = _role(product, sorted(PERMISSIONS), base="administrator", name="Full site owner")
    assert _assign(product, product.admin.id, profile["id"]).status_code == 200
    _, headers = _session(product, str(product.admin.id))
    policy = product.app.state.operator_roles
    before = policy._path.read_bytes()
    revision = policy.snapshot()["revision"]
    response = product.client.patch(
        _ROLES + "/" + profile["id"],
        headers=headers,
        json={
            **{key: value for key, value in profile.items() if key != "id"},
            "permissions": sorted(PERMISSIONS - {"time.manage"}),
            "expected_revision": revision,
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "roles_last_administrator"
    assert policy._path.read_bytes() == before
    reopened = OperatorRolePolicy(policy._root, product.service.site_id)
    try:
        assert reopened.permissions(product.admin) == PERMISSIONS
    finally:
        reopened.close()
    assert product.client.get(_ROLES, headers=headers).status_code == 200
