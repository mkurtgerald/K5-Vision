from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from urllib.parse import urlencode
from uuid import UUID, uuid4

import pytest
import test_operator_product_api as helpers

from k5vision.domain.users import UserPatch, UserRole
from k5vision.media.mediamtx_provider import MediaMTXBinding
from k5vision.services.user_registry import UserRegistry

product = helpers.product
_PREFIX = "/api/v1/operator"
_ADMIN = _PREFIX + "/admin"


def _create(product, *, role="viewer", camera_ids=(), username="site-reader", mode="selected"):
    response = product.client.post(
        _ADMIN + "/users",
        headers=product.headers,
        json={
            "username": username,
            "display_name": "Site reader",
            "role": role,
            "enabled": True,
            "camera_access": {"mode": mode, "camera_ids": list(camera_ids)},
        },
    )
    assert response.status_code == 201, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


def _activate(product, issue):
    account = issue["account"]
    initialized = product.users.activate_bootstrap_password(
        username=account["username"],
        temporary_credential=issue["temporary_credential"],
        new_password="admin-controls-user-password",
        actor="test",
    )
    assert initialized is not None
    token, _ = asyncio.run(product.sessions.issue(initialized))
    return token, {"Authorization": "Bearer " + token}


def _two_cameras(product):
    first = product.enroll()
    response = product.client.post(
        _PREFIX + "/cameras",
        headers=product.headers,
        json={
            "name": "Restricted camera",
            "host": "127.0.0.2",
            "source_uri": "rtsp://127.0.0.2/live",
        },
    )
    assert response.status_code == 201
    return first, response.json()["device_id"]


def _media(product, token, camera_id, *, export=None):
    path = MediaMTXBinding(product.service.site_id, UUID(camera_id)).path_name
    if export is None:
        uri = f"/media/live/{path}/index.m3u8"
    else:
        uri = "/media/playback?" + urlencode(
            {"path": path, "start": datetime.now(UTC).isoformat(), "duration": 2, "format": export}
        )
    return product.client.get(
        _PREFIX + "/media-auth", headers=product.media_headers(uri, token=token)
    )


def test_admin_user_creation_and_audit_keep_stored_credentials_private(product):
    first, second = _two_cameras(product)
    issue = _create(product, camera_ids=[first])
    account = issue["account"]
    assert account["credential_status"] == "pending"
    assert account["camera_access"] == {"mode": "selected", "camera_ids": [first]}
    users = product.client.get(_ADMIN + "/users?limit=1", headers=product.headers)
    assert users.status_code == 200 and users.headers["cache-control"] == "no-store"
    assert users.json()["has_more"] is True
    assert users.json()["users"][0]["credential_status"] == "active"
    next_page = product.client.get(_ADMIN + "/users?offset=1&limit=1", headers=product.headers)
    assert next_page.json()["users"] == [account]
    assert next_page.json()["has_more"] is False
    audit = product.client.get(_ADMIN + "/audit", headers=product.headers)
    assert audit.status_code == 200 and audit.headers["cache-control"] == "no-store"
    events = audit.json()["events"]
    assert [event["event_id"] for event in events] == sorted(
        [event["event_id"] for event in events], reverse=True
    )
    assert any(event["action"] == "camera-access-updated" for event in events)
    assert issue["temporary_credential"] not in users.text + next_page.text + audit.text
    assert second not in str(account["camera_access"])


def test_selected_viewer_permissions_cover_list_grants_history_media_and_export(product):
    first, second = _two_cameras(product)
    token, headers = _activate(product, _create(product, camera_ids=[first]))
    listing = product.client.get(_PREFIX + "/cameras", headers=headers)
    assert listing.status_code == 200
    assert [camera["device_id"] for camera in listing.json()["cameras"]] == [first]
    caps = product.client.get(_PREFIX + "/capabilities", headers=headers)
    assert caps.status_code == 200
    assert caps.json()["can_manage_users"] is False
    assert caps.json()["can_manage_cameras"] is False
    assert caps.json()["can_export"] is False
    assert caps.json()["can_view_history"] is True
    assert (
        product.client.get(_PREFIX + f"/cameras/{first}/history", headers=headers).status_code
        == 200
    )
    assert (
        product.client.get(_PREFIX + f"/cameras/{second}/history", headers=headers).status_code
        == 404
    )
    assert (
        product.client.post(
            _PREFIX + f"/cameras/{second}/media-session", headers=headers
        ).status_code
        == 404
    )
    assert (
        product.client.post(
            _PREFIX + f"/cameras/{first}/media-session", headers=headers
        ).status_code
        == 200
    )
    assert _media(product, token, first).status_code == 200
    assert _media(product, token, second).status_code == 401
    assert _media(product, token, first, export="fmp4").status_code == 200
    assert _media(product, token, first, export="mp4").status_code == 403
    assert product.client.get(_ADMIN + "/users", headers=headers).status_code == 403
    assert product.client.get(_ADMIN + "/audit", headers=headers).status_code == 403
    assert (
        product.client.put(
            _PREFIX + f"/cameras/{first}/recording", headers=headers, json={"enabled": False}
        ).status_code
        == 403
    )


def test_operator_can_export_only_an_assigned_camera(product):
    first, second = _two_cameras(product)
    token, _ = _activate(product, _create(product, role="operator", camera_ids=[first]))
    assert _media(product, token, first, export="mp4").status_code == 200
    assert _media(product, token, second, export="mp4").status_code == 401


def test_permission_change_revokes_session_and_cached_media_then_persists(product):
    first, second = _two_cameras(product)
    issue = _create(product, camera_ids=[first])
    token, headers = _activate(product, issue)
    user_id = issue["account"]["id"]
    response = product.client.patch(
        _ADMIN + f"/users/{user_id}",
        headers=product.headers,
        json={
            "display_name": "Changed reader",
            "camera_access": {"mode": "selected", "camera_ids": [second]},
        },
    )
    assert response.status_code == 200
    assert response.json()["display_name"] == "Changed reader"
    assert response.json()["camera_access"]["camera_ids"] == [second]
    assert product.client.get(_PREFIX + "/capabilities", headers=headers).status_code == 401
    assert _media(product, token, first).status_code == 401
    user = product.users.get(UUID(user_id))
    renewed, _ = asyncio.run(product.sessions.issue(user))
    assert _media(product, renewed, first).status_code == 401
    assert _media(product, renewed, second).status_code == 200
    reopened = UserRegistry(
        database_path=product.users._database_path, site_id=product.users.site_id
    )
    try:
        assert reopened.get(UUID(user_id)).display_name == "Changed reader"
        assert reopened.get_camera_access(UUID(user_id))["camera_ids"] == [UUID(second)]
    finally:
        reopened.close()


def test_disabled_account_and_password_reset_revoke_cached_access(product):
    first = product.enroll()
    issue = _create(product, role="operator", camera_ids=[first])
    token, headers = _activate(product, issue)
    user_id = issue["account"]["id"]
    assert _media(product, token, first).status_code == 200
    disabled = product.client.patch(
        _ADMIN + f"/users/{user_id}", headers=product.headers, json={"enabled": False}
    )
    assert disabled.status_code == 200
    assert product.client.get(_PREFIX + "/cameras", headers=headers).status_code == 401
    assert _media(product, token, first).status_code == 401
    assert (
        product.client.patch(
            _ADMIN + f"/users/{user_id}", headers=product.headers, json={"enabled": True}
        ).status_code
        == 200
    )
    account = product.users.get(UUID(user_id))
    token, _ = asyncio.run(product.sessions.issue(account))
    reset = product.client.post(
        _ADMIN + f"/users/{user_id}/reset-password", headers=product.headers
    )
    assert reset.status_code == 200
    assert reset.headers["cache-control"] == "no-store"
    assert reset.json()["account"]["credential_status"] == "pending"
    assert _media(product, token, first).status_code == 401
    assert (
        product.users.verify_password(
            username=account.username, password="admin-controls-user-password"
        )
        is None
    )
    _activate(product, reset.json())


def test_last_active_administrator_cannot_be_disabled_demoted_or_reset(product):
    route = _ADMIN + f"/users/{product.admin.id}"
    for patch in ({"enabled": False}, {"role": "viewer"}):
        response = product.client.patch(route, headers=product.headers, json=patch)
        assert (
            response.status_code == 409 and response.json()["detail"] == "admin_last_administrator"
        )
    reset = product.client.post(route + "/reset-password", headers=product.headers)
    assert reset.status_code == 409 and reset.json()["detail"] == "admin_last_administrator"
    assert product.client.get(_PREFIX + "/capabilities", headers=product.headers).status_code == 200


def test_invalid_combined_change_does_not_change_role_or_camera_access(product):
    first = product.enroll()
    issue = _create(product, camera_ids=[first])
    user_id = issue["account"]["id"]
    response = product.client.patch(
        _ADMIN + f"/users/{user_id}",
        headers=product.headers,
        json={
            "role": "operator",
            "camera_access": {"mode": "selected", "camera_ids": [str(uuid4())]},
        },
    )
    assert response.status_code == 422
    assert product.users.get(UUID(user_id)).role is UserRole.VIEWER
    assert product.users.get_camera_access(UUID(user_id))["camera_ids"] == [UUID(first)]


def test_unavailable_camera_grant_survives_unrelated_account_edit(product):
    first = product.enroll()
    issue = _create(product, camera_ids=[first])
    assert (
        product.client.delete(_PREFIX + f"/cameras/{first}", headers=product.headers).status_code
        == 204
    )
    user_id = issue["account"]["id"]
    response = product.client.patch(
        _ADMIN + f"/users/{user_id}",
        headers=product.headers,
        json={
            "display_name": "Updated with unavailable camera",
            "camera_access": {"mode": "selected", "camera_ids": [first]},
        },
    )
    assert response.status_code == 200
    assert response.json()["camera_access"]["camera_ids"] == [first]


def test_reset_rejects_unexpected_password_in_body_without_echo(product):
    issue = _create(product)
    response = product.client.post(
        _ADMIN + "/users/" + issue["account"]["id"] + "/reset-password",
        headers=product.headers,
        json={"password": "never-echo-password"},
    )
    assert response.status_code == 422
    assert response.json() == {"detail": "admin_invalid_request"}
    assert "never-echo" not in response.text


def test_old_session_cannot_inherit_a_promoted_role_from_registry(product):
    issue = _create(product)
    _, headers = _activate(product, issue)
    product.users.update(
        UUID(issue["account"]["id"]), UserPatch(role=UserRole.OPERATOR), actor="test"
    )
    assert product.client.get(_PREFIX + "/capabilities", headers=headers).status_code == 401


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/devices",
        "/api/v1/devices/" + str(uuid4()),
        "/api/v1/operator/recordings",
        "/api/v1/operator/recordings/" + str(uuid4()) + "/export",
        "/api/v1/operator/live",
        "/api/v1/operator/recording-sessions",
        "/api/v1/operator/playback-controls/" + str(uuid4()) + "/resume",
    ],
)
def test_installed_product_does_not_dispatch_legacy_device_or_recording_routes(product, path):
    # Register a deliberately revealing legacy endpoint, as main.create_app does.
    # The product guard must intercept it before its older broad-role admission.
    async def legacy():
        return {"name": "Confidential unassigned camera", "host": "192.0.2.55"}

    product.app.add_api_route(path, legacy, methods=["GET", "POST"])
    _, headers = _activate(product, _create(product))
    for caller in (headers, product.headers, {}):
        response = product.client.get(path, headers=caller)
        assert response.status_code == 404
        assert response.headers["cache-control"] == "no-store"
        assert "Confidential" not in response.text and "192.0.2.55" not in response.text


@pytest.mark.parametrize("endpoint", ["users", "audit"])
def test_admin_reads_require_current_administrator_and_no_service_credential_ui(product, endpoint):
    route = _ADMIN + "/" + endpoint
    assert product.client.get(route).status_code == 401
    _, headers = _activate(product, _create(product, role="operator"))
    assert product.client.get(route, headers=headers).status_code == 403
    assert product.client.get(route + "?limit=101", headers=product.headers).status_code == 422


def test_admin_mutations_reject_cross_origin_and_redact_invalid_secret_bodies(product):
    body = {"username": "bad", "display_name": "Bad", "password": "never-echo-this-password"}
    response = product.client.post(_ADMIN + "/users", headers=product.headers, json=body)
    assert response.status_code == 422
    assert response.json() == {"detail": "admin_invalid_request"}
    assert "never-echo" not in response.text
    response = product.client.post(
        _ADMIN + "/users",
        headers={**product.headers, "Origin": "https://external.invalid"},
        json={"username": "cross-origin", "display_name": "External"},
    )
    assert response.status_code == 403
    response = product.client.post(
        _ADMIN + "/users",
        headers={**product.headers, "Content-Type": "application/json"},
        content=b'{"password":"never-echo' + b"x" * 16384,
    )
    assert response.status_code == 413 and "never-echo" not in response.text


def test_pending_account_cannot_use_an_internally_issued_session(product):
    issue = _create(product)
    account = product.users.get(UUID(issue["account"]["id"]))
    token, _ = asyncio.run(product.sessions.issue(account))
    assert (
        product.client.get(
            _PREFIX + "/capabilities", headers={"Authorization": "Bearer " + token}
        ).status_code
        == 401
    )


def test_display_name_edit_keeps_own_session_when_authority_is_unchanged(product):
    response = product.client.patch(
        _ADMIN + f"/users/{product.admin.id}",
        headers=product.headers,
        json={
            "display_name": "Renamed administrator",
            "role": "administrator",
            "enabled": True,
            "camera_access": {"mode": "all", "camera_ids": []},
        },
    )
    assert response.status_code == 200
    capabilities = product.client.get(_PREFIX + "/capabilities", headers=product.headers)
    assert capabilities.status_code == 200
    assert capabilities.json()["account"]["display_name"] == "Renamed administrator"


@pytest.mark.parametrize("operation", ["reset", "update"])
def test_authority_is_rechecked_after_session_revocation_before_sensitive_response(
    product, monkeypatch, operation
):
    _activate(product, _create(product, role="administrator", mode="all", username="backup-admin"))
    issue = _create(product)
    target_id = UUID(issue["account"]["id"])
    original = product.sessions.revoke_user

    async def actor_demoted_while_revoking(user_id):
        await original(user_id)
        product.users.update(product.admin.id, UserPatch(role=UserRole.VIEWER), actor="test")

    monkeypatch.setattr(product.sessions, "revoke_user", actor_demoted_while_revoking)
    if operation == "reset":
        response = product.client.post(
            _ADMIN + f"/users/{target_id}/reset-password", headers=product.headers
        )
    else:
        response = product.client.patch(
            _ADMIN + f"/users/{target_id}", headers=product.headers, json={"enabled": False}
        )
    assert response.status_code == 401
    assert "temporary_credential" not in response.text


def test_camera_list_rechecks_grants_after_worker_snapshot(product, monkeypatch):
    first, second = _two_cameras(product)
    issue = _create(product, camera_ids=[first, second])
    _, headers = _activate(product, issue)
    original = product.service.provider.path_snapshot

    async def revoke_during_snapshot(bindings):
        result = await original(bindings)
        product.users.set_camera_access(
            UUID(issue["account"]["id"]), mode="selected", camera_ids=[], actor="test"
        )
        return result

    monkeypatch.setattr(product.service.provider, "path_snapshot", revoke_during_snapshot)
    response = product.client.get(_PREFIX + "/cameras", headers=headers)
    assert response.status_code == 200
    assert response.json()["cameras"] == []


def test_history_rechecks_grants_after_worker_inventory(product, monkeypatch):
    first = product.enroll()
    issue = _create(product, camera_ids=[first])
    _, headers = _activate(product, issue)
    original = product.service.provider.playback_spans

    async def revoke_during_history(*args, **kwargs):
        spans = await original(*args, **kwargs)
        product.users.set_camera_access(
            UUID(issue["account"]["id"]), mode="selected", camera_ids=[], actor="test"
        )
        return spans

    monkeypatch.setattr(product.service.provider, "playback_spans", revoke_during_history)
    response = product.client.get(_PREFIX + f"/cameras/{first}/history", headers=headers)
    assert response.status_code == 404


@pytest.mark.parametrize("product", [{"analytics": True}], indirect=True)
def test_person_result_rechecks_current_camera_grant_after_inference(product):
    first = product.enroll()
    issue = _create(product, camera_ids=[first])
    _, headers = _activate(product, issue)

    async def revoke(_submission):
        product.users.set_camera_access(
            UUID(issue["account"]["id"]), mode="selected", camera_ids=[], actor="test"
        )

    product.analytics.callback = revoke
    response = product.client.post(
        helpers._person_url(first),
        headers={
            **headers,
            "Origin": "http://127.0.0.1:8080",
            "Content-Type": "application/octet-stream",
        },
        content=bytes(16),
    )
    assert response.status_code == 404
    assert len(product.analytics.submissions) == 1
