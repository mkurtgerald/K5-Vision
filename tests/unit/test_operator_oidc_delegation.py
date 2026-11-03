"""SSO management cannot delegate identity authority beyond current K5 grants."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest
from test_operator_oidc import _configure
from test_operator_oidc import identity as identity  # noqa: F401
from test_operator_oidc import idp as idp  # noqa: F401
from test_operator_oidc import setup as setup  # noqa: F401

from k5vision.domain.users import UserCreate, UserRole
from k5vision.operator_role_policy import PERMISSIONS, OperatorRolePolicy


def _create(values, username, role):
    http, _, _, actor, users, _ = values
    account, temporary, _ = users.create_with_bootstrap(
        UserCreate(username=username, display_name=username, role=role), actor=actor.username
    )
    users.activate_bootstrap_password(
        username=username,
        temporary_credential=temporary,
        new_password="delegation-test-password",
        actor=actor.username,
    )
    response = http.post(
        "/api/v1/auth/login",
        json={
            "username": username,
            "password": "delegation-test-password",
        },
    )
    assert response.status_code == 200
    token = response.json()["session_token"]
    return account, token, {"Authorization": "Bearer " + token}


@pytest.fixture
def delegated(setup, identity, idp):
    http, oidc, _, full, users, sessions = setup
    policy = OperatorRolePolicy(identity.directory, identity.site_id)
    http.app.state.operator_roles = policy
    manager, token, headers = _create(setup, "delegated-sso-manager", UserRole.ADMINISTRATOR)
    target, _, _ = _create(setup, "delegated-viewer", UserRole.VIEWER)
    permissions = {"users.manage", "cameras.view_live", "cameras.playback"}
    profile = policy.upsert(
        users,
        {
            "name": "Scoped people manager",
            "description": "Limited delegation",
            "base_role": "administrator",
            "permissions": sorted(permissions),
        },
    )
    policy.assign(users, manager.id, UUID(profile["id"]))
    full_link = _configure(setup, idp)
    yield SimpleNamespace(
        values=setup,
        http=http,
        oidc=oidc,
        full=full,
        users=users,
        sessions=sessions,
        policy=policy,
        manager=manager,
        manager_token=token,
        headers=headers,
        target=target,
        profile=profile,
        full_link=full_link,
        permissions=permissions,
    )
    policy.close()


def test_limited_people_manager_cannot_replace_global_identity_provider(delegated, idp):
    value = delegated
    before = value.oidc.store.provider()
    response = value.http.put(
        "/api/v1/operator/admin/sso/provider",
        headers=value.headers,
        json={
            "enabled": False,
            "display_name": "Replacement trust",
            "issuer": idp.issuer,
            "client_id": "attacker-client",
        },
    )
    assert response.status_code == 403
    assert value.oidc.store.provider() == before
    assert (
        value.http.get("/api/v1/operator/admin/sso/provider", headers=value.headers).status_code
        == 200
    )
    assert (
        value.http.post(
            "/api/v1/operator/admin/sso/provider/test", headers=value.headers
        ).status_code
        == 200
    )


def test_limited_manager_cannot_link_own_external_subject_to_full_administrator(delegated):
    value = delegated
    before = value.oidc.store.links()
    response = value.http.post(
        "/api/v1/operator/admin/sso/links",
        headers=value.headers,
        json={
            "subject": "manager-controlled-subject",
            "user_id": str(value.full.id),
        },
    )
    assert response.status_code == 403
    assert value.oidc.store.links() == before


def test_limited_manager_cannot_remove_stronger_administrator_identity(delegated):
    value = delegated
    response = value.http.delete(
        "/api/v1/operator/admin/sso/links/" + value.full_link["id"],
        headers=value.headers,
    )
    assert response.status_code == 403
    assert any(row["id"] == value.full_link["id"] for row in value.oidc.store.links())


def test_limited_manager_can_link_and_remove_target_within_current_permission_ceiling(delegated):
    value = delegated
    response = value.http.post(
        "/api/v1/operator/admin/sso/links",
        headers=value.headers,
        json={
            "subject": "approved-viewer-subject",
            "user_id": str(value.target.id),
        },
    )
    assert response.status_code == 201
    identifier = response.json()["id"]
    assert (
        value.http.delete(
            "/api/v1/operator/admin/sso/links/" + identifier,
            headers=value.headers,
        ).status_code
        == 204
    )
    assert not any(row["id"] == identifier for row in value.oidc.store.links())


def test_full_administrator_can_configure_provider_and_manage_stronger_identity(setup, idp):
    http, oidc, _, account, _, _ = setup
    login = http.post(
        "/api/v1/auth/login",
        json={
            "username": account.username,
            "password": "federation-test-password",
        },
    )
    headers = {"Authorization": "Bearer " + login.json()["session_token"]}
    assert (
        http.put(
            "/api/v1/operator/admin/sso/provider",
            headers=headers,
            json={
                "enabled": True,
                "display_name": "Full administrator trust",
                "issuer": idp.issuer,
                "client_id": "k5-native-fixture",
            },
        ).status_code
        == 200
    )
    linked = http.post(
        "/api/v1/operator/admin/sso/links",
        headers=headers,
        json={
            "subject": "full-administrator-subject",
            "user_id": str(account.id),
        },
    )
    assert linked.status_code == 201
    assert (
        http.delete(
            "/api/v1/operator/admin/sso/links/" + linked.json()["id"],
            headers=headers,
        ).status_code
        == 204
    )
    assert oidc.store.links() == []


def test_link_checks_latest_permission_profile_after_authentication_await(delegated, monkeypatch):
    value = delegated
    resolve = value.sessions.resolve
    calls = 0
    narrower = value.policy.upsert(
        value.users,
        {
            "name": "Narrowed people manager",
            "description": "Permission changed during request",
            "base_role": "administrator",
            "permissions": ["users.manage", "cameras.view_live"],
        },
    )

    async def changed(token):
        nonlocal calls
        account = await resolve(token)
        if token == value.manager_token:
            calls += 1
            if calls == 2:
                value.policy.assign(value.users, value.manager.id, UUID(narrower["id"]))
        return account

    monkeypatch.setattr(value.sessions, "resolve", changed)
    response = value.http.post(
        "/api/v1/operator/admin/sso/links",
        headers=value.headers,
        json={
            "subject": "stale-grant-subject",
            "user_id": str(value.target.id),
        },
    )
    assert calls >= 2 and response.status_code == 403
    assert not any(row["subject"] == "stale-grant-subject" for row in value.oidc.store.links())


def test_provider_checks_current_full_authority_instead_of_base_admin_snapshot(
    delegated, idp, monkeypatch
):
    value = delegated
    full_profile = value.policy.upsert(
        value.users,
        {
            "name": "Initially full manager",
            "description": "Downgraded during authentication",
            "base_role": "administrator",
            "permissions": sorted(PERMISSIONS),
        },
    )
    value.policy.assign(value.users, value.manager.id, UUID(full_profile["id"]))
    resolve = value.sessions.resolve
    calls = 0

    async def changed(token):
        nonlocal calls
        account = await resolve(token)
        if token == value.manager_token:
            calls += 1
            if calls == 2:
                value.policy.assign(value.users, value.manager.id, UUID(value.profile["id"]))
        return account

    monkeypatch.setattr(value.sessions, "resolve", changed)
    before = value.oidc.store.provider()
    response = value.http.put(
        "/api/v1/operator/admin/sso/provider",
        headers=value.headers,
        json={
            "enabled": False,
            "display_name": "Stale full authority",
            "issuer": idp.issuer,
            "client_id": "changed-client",
        },
    )
    assert calls >= 2 and response.status_code == 403
    assert value.oidc.store.provider() == before
