"""Chromium interaction checks for recorder settings, provider setup and MFA sign-in."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

_ASSETS = Path(__file__).resolve().parents[2] / "src/k5vision/operator_web"
_ADMIN = "a22c22bc-4089-45b7-a370-f360bb9d33c0"
_CAMERA = "e4fd401d-ae8e-4ac2-9f92-e8287f241532"
_ROLE = "36943759-ff3f-4990-9503-5b525f554d72"
_CONNECTION = "e102562c-bfb3-410a-b3c0-0416ec69f6e2"
_GIB = 1024**3


def _ui_core(account):
    return {
        "id": account.get("id", "a22c22bc-4089-45b7-a370-f360bb9d33c0"),
        "username": account["username"],
        "display_name": account.get("display_name", account["username"]),
        "role": account["role"],
        "enabled": account.get("enabled", True),
    }


def _ui_inventory(account, cameras, **extra):
    from k5vision.operator_role_policy import BASE_PERMISSIONS

    permissions = sorted(account.get("effective_permissions", BASE_PERMISSIONS[account["role"]]))
    return {
        **extra,
        "cameras": cameras,
        "capabilities": {
            "account": {
                **_ui_core(account),
                "role_profile_id": account.get("role_profile_id"),
                "effective_permissions": permissions,
            },
            "effective_permissions": permissions,
            "can_export": "cameras.export" in permissions,
            "can_manage_users": "users.manage" in permissions,
            "can_manage_cameras": "cameras.manage" in permissions,
            "can_view_history": "cameras.playback" in permissions,
        },
    }


@pytest.fixture
def workspace():
    if os.getenv("K5_OPERATOR_BROWSER_ACCEPTANCE") != "1":
        pytest.skip("Settings UI proof requires the explicit browser environment")
    browser_api = pytest.importorskip("playwright.sync_api")

    @contextmanager
    def launch(
        *, role="administrator", permissions=None, mfa_login=False, sso=False, hold_inventory=False
    ):
        account = {
            "id": _ADMIN,
            "username": "fixture-admin",
            "display_name": "Fixture administrator",
            "role": role,
            "enabled": True,
            "credential_status": "active",
            "camera_access": {"mode": "all", "camera_ids": []},
            "role_profile_id": None,
        }
        if permissions is not None:
            account["effective_permissions"] = permissions
        cameras = [
            {
                "device_id": _CAMERA,
                "name": "Generated entrance camera",
                "host": "127.0.0.1",
                "status": "disconnected",
                "recording": False,
                "recording_enabled": True,
                "recording_state": "waiting",
                "analytics_status": "disabled",
            }
        ]
        state = {
            "requests": [],
            "users": [{**account, **_ui_inventory(account, [])["capabilities"]["account"]}],
            "storage": {
                "revision": 3,
                "quota_bytes": 20 * _GIB,
                "minimum_free_bytes": _GIB,
                "retention_seconds": 14 * 86400,
                "recording_directory": "C:\\K5\\recordings",
                "pending_recording_directory": None,
                "location_status": "active",
                "recording_bytes": 4 * _GIB,
                "free_bytes": 100 * _GIB,
                "recording_allowed": True,
                "inspection_available": True,
                "retention_basis": "elapsed_stable_time",
                "old_media_preserved": True,
            },
            "storage_conflict": False,
            "connections": [],
            "roles": [],
            "role_revision": 1,
            "mfa_enabled": False,
            "mfa_login": mfa_login,
            "sso_available": sso,
            "sso_provider": {
                "configured": False,
                "enabled": False,
                "display_name": "",
                "issuer": "",
                "client_id": "",
                "has_client_secret": False,
                "groups_claim": "groups",
            },
            "sso_links": [],
            "handshake": False,
            "audit_before": [],
            "time_sync_requests": 0,
            "reject_session": False,
            "cameras": cameras,
            "capability_fault": None,
            "hold_inventory": hold_inventory,
            "held_inventory": [],
        }
        errors = []
        channel = os.getenv(
            "K5_OPERATOR_BROWSER_CHANNEL", "msedge" if os.name == "nt" else "chromium"
        )
        with browser_api.sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel=channel, headless=True)
            context = browser.new_context(
                viewport={"width": 1440, "height": 1000}, accept_downloads=True
            )
            context.add_init_script("window.Hls = {isSupported: () => false};")
            page = context.new_page()
            page.set_default_timeout(8000)
            page.on("pageerror", lambda error: errors.append(str(error)))

            def respond(route, data=None, status=200):
                return route.fulfill(
                    status=status,
                    content_type="application/json",
                    body="" if status == 204 else json.dumps(data or {}),
                )

            def issue():
                return {
                    "session_token": "generated-settings-session-token-012345678901234567",
                    "account": account,
                    "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
                    "expires_in_seconds": 600,
                }

            def role_snapshot():
                return {
                    "revision": state["role_revision"],
                    "roles": state["roles"],
                    "available_permissions": [
                        "cameras.view_live",
                        "cameras.playback",
                        "cameras.export",
                    ],
                    "base_permissions": {
                        "viewer": ["cameras.view_live", "cameras.playback"],
                        "operator": ["cameras.view_live", "cameras.playback", "cameras.export"],
                        "administrator": [
                            "cameras.view_live",
                            "cameras.playback",
                            "cameras.export",
                            "users.manage",
                            "storage.manage",
                        ],
                    },
                }

            def intercept(route):
                url = urlsplit(route.request.url)
                path = url.path
                query = parse_qs(url.query)
                method = route.request.method
                if path.startswith("/operator/"):
                    name = path.rsplit("/", 1)[1] or "index.html"
                    asset = _ASSETS / name
                    if (
                        name != asset.name
                        or not asset.is_file()
                        or asset.suffix not in {".js", ".css", ".html"}
                    ):
                        return respond(route, {}, 404)
                    return route.fulfill(
                        content_type={
                            ".js": "application/javascript",
                            ".css": "text/css",
                            ".html": "text/html",
                        }[asset.suffix],
                        body=asset.read_text(encoding="utf-8"),
                    )
                if path == "/authorize":
                    # A fresh navigation models the identity-provider return. Playwright
                    # does not route HTTP redirects after the first intercepted request.
                    return route.fulfill(
                        content_type="text/html",
                        body='<script>location.replace("http://k5.test/operator/'
                        '?sso_code=generated-one-time-sso-code-0123456789")</script>',
                    )
                body = route.request.post_data_json if route.request.post_data else None
                state["requests"].append((method, path, body, query))
                if path == "/api/v1/auth/oidc":
                    return respond(
                        route,
                        {
                            "available": state["sso_available"],
                            "display_name": "Fixture organization",
                        },
                    )
                if path == "/api/v1/auth/oidc/start":
                    return respond(
                        route, {"authorization_url": "https://identity.example.test/authorize"}
                    )
                if path == "/api/v1/auth/oidc/complete":
                    return respond(route, issue())
                if path == "/api/v1/auth/login":
                    if state["mfa_login"]:
                        return respond(
                            route,
                            {
                                "mfa_required": True,
                                "challenge_token": "generated-mfa-challenge-0123456789",
                                "expires_in_seconds": 300,
                            },
                            202,
                        )
                    return respond(route, issue())
                if path == "/api/v1/auth/mfa":
                    if body["code"] != "123456":
                        return respond(route, {"detail": "mfa_invalid_code"}, 401)
                    return respond(route, issue())
                if state["reject_session"] and path in {
                    "/api/v1/auth/me",
                    "/api/v1/operator/status",
                    "/api/v1/operator/cameras",
                }:
                    return respond(route, {"detail": "Unauthorized"}, 401)
                if path == "/api/v1/auth/me":
                    return respond(route, _ui_core(account))
                if path in {"/api/v1/auth/logout", "/api/v1/operator/media-session"}:
                    return respond(route, {}, 204)
                if path == "/api/v1/operator/status":
                    return respond(
                        route,
                        {"worker_status": "ready", "server_utc": datetime.now(UTC).isoformat()},
                    )
                if path == "/api/v1/operator/cameras":
                    payload = _ui_inventory(account, cameras, worker_status="ready")
                    if state["hold_inventory"]:
                        state["held_inventory"].append((route, payload))
                        return None
                    fault = state["capability_fault"]
                    if fault == "missing":
                        payload.pop("capabilities")
                    elif fault == "foreign":
                        payload["capabilities"]["account"]["id"] = _CAMERA
                    elif fault == "contradictory":
                        payload["capabilities"]["can_export"] = False
                    elif fault == "expanded":
                        payload["capabilities"]["effective_permissions"].append("unknown.admin")
                    return respond(route, payload)
                if path == "/api/v1/operator/admin/users":
                    if method == "GET":
                        return respond(route, {"users": state["users"], "has_more": False})
                if path == "/api/v1/operator/admin/audit":
                    return respond(route, {"events": [], "has_more": False})
                if path == "/api/v1/operator/admin/storage":
                    if method == "PATCH":
                        if state["storage_conflict"]:
                            return respond(route, {"detail": "storage_conflict"}, 409)
                        assert body["expected_revision"] == state["storage"]["revision"]
                        for key in ("quota_bytes", "minimum_free_bytes", "retention_seconds"):
                            if key in body:
                                state["storage"][key] = body[key]
                        if "recording_directory" in body:
                            state["storage"].update(
                                pending_recording_directory=body["recording_directory"],
                                location_status="restart_required",
                            )
                        state["storage"]["revision"] += 1
                    return respond(route, state["storage"])
                if path == "/api/v1/operator/admin/time":
                    return respond(
                        route,
                        {
                            "host": {
                                "platform": "Windows",
                                "status": "observed",
                                "service_running": True,
                                "mode": "manual",
                                "ntp_servers": ["time.example.org"],
                                "clock": {},
                                "reference": {
                                    "status": "measured",
                                    "offset_seconds": 0.2,
                                    "uncertainty_seconds": 0.01,
                                    "measured_at": datetime.now(UTC).isoformat(),
                                    "sync_verified": False,
                                },
                            },
                            "capabilities": {"configure_ntp": True, "resynchronize": True},
                            **(
                                {
                                    "operation": {
                                        "action": "configure",
                                        "status": "configured",
                                        "verified": False,
                                    }
                                }
                                if method == "PATCH"
                                else {}
                            ),
                        },
                    )
                if path == "/api/v1/operator/admin/time/synchronize":
                    state["time_sync_requests"] += 1
                    return respond(
                        route,
                        {
                            "operation": {
                                "action": "resynchronize",
                                "status": "completed",
                                "verified": False,
                            }
                        },
                    )
                if path in {
                    f"/api/v1/operator/cameras/{_CAMERA}/time",
                    f"/api/v1/operator/cameras/{_CAMERA}/time/status",
                }:
                    return respond(
                        route,
                        {
                            "camera_id": _CAMERA,
                            "clock": {"status": "observed", "offset_seconds": 12.5},
                            "ntp": {"mode": "NTP", "from_dhcp": True, "servers": []},
                            "capabilities": {"configure_ntp": True, "synchronize_to_host": True},
                        },
                    )
                if path == f"/api/v1/operator/cameras/{_CAMERA}/time/synchronize":
                    return respond(
                        route,
                        {
                            "operation": {
                                "action": "copy_host",
                                "status": "verified",
                                "verified": True,
                            },
                            "absolute_sync_verified": False,
                        },
                    )
                if path.startswith("/api/v1/operator/admin/activity"):
                    event = {
                        "id": 9,
                        "occurred_at": "2026-10-31T11:00:00Z",
                        "run_id": "fixture",
                        "elapsed_ms": 100,
                        "request_id": "fixture-request",
                        "actor_id": _ADMIN,
                        "action": "camera.recording",
                        "outcome": "started",
                        "target_kind": "camera",
                        "target_id": _CAMERA,
                        "status_code": None,
                        "changes": ["recording_enabled"],
                    }
                    result = {
                        "events": [event],
                        "has_more": "before" not in query,
                        "next_before": 9 if "before" not in query else None,
                        "first_retained_id": 1,
                        "last_id": 9,
                        "total_rows": 9,
                    }
                    if path.endswith("/export"):
                        result.update(schema_version=1, site_id="fixture-site")
                    else:
                        state["audit_before"].append(query.get("before", [None])[0])
                    return respond(route, result)
                if path == "/api/v1/operator/admin/integrations":
                    if method == "POST":
                        c = {
                            "id": _CONNECTION,
                            **body,
                            "credential_configured": True,
                            "status": "awaiting_provider",
                            "last_test_utc": None,
                            "capabilities": [],
                            "queued_events": 0,
                            "delivered_events": 0,
                        }
                        c.pop("token", None)
                        state["connections"].append(c)
                        return respond(
                            route,
                            {**c, "setup_token": "generated-provider-secret-shown-once-0123456789"},
                            201,
                        )
                    return respond(
                        route,
                        {
                            "connections": state["connections"],
                            "supported_contract": "1.0",
                            "neural_authority": "observe_only",
                        },
                    )
                if path.startswith("/api/v1/operator/admin/integrations/"):
                    c = state["connections"][0]
                    if path.endswith("/test"):
                        return respond(route, {**c, "compatible": state["handshake"]})
                    if path.endswith("/retry"):
                        c["exhausted_events"] = 0
                        return respond(route, c)
                    if method == "DELETE":
                        state["connections"].clear()
                        return respond(route, {}, 204)
                    c.update(body)
                    return respond(route, c)
                if path == "/api/v1/operator/admin/roles":
                    if method == "POST":
                        state["roles"].append({"id": _ROLE, **body})
                        state["role_revision"] += 1
                    return respond(route, role_snapshot())
                if path.startswith("/api/v1/operator/admin/roles/"):
                    if method == "DELETE":
                        assert query["expected_revision"] == [str(state["role_revision"])]
                        state["roles"].clear()
                        return respond(route, {}, 204)
                    assert body["expected_revision"] == state["role_revision"]
                    state["role_revision"] += 1
                    state["roles"][0].update(
                        {k: v for k, v in body.items() if k != "expected_revision"}
                    )
                    return respond(route, role_snapshot())
                if path.startswith("/api/v1/operator/admin/users/") and method == "PATCH":
                    user_id = path.split("/")[6]
                    target = next(u for u in state["users"] if u["id"] == user_id)
                    if target.get("role_profile_id"):
                        assert "role" not in body
                    target.update(body)
                    return respond(route, target)
                if path.endswith("/role-profile"):
                    user_id = path.split("/")[6]
                    target = next(u for u in state["users"] if u["id"] == user_id)
                    target["role_profile_id"] = body["role_profile_id"]
                    profile = next(
                        (r for r in state["roles"] if r["id"] == body["role_profile_id"]), None
                    )
                    if profile:
                        target["role"] = profile["base_role"]
                        target["effective_permissions"] = profile["permissions"]
                    state["role_revision"] += 1
                    return respond(route, {"account": target, "revision": state["role_revision"]})
                if path == "/api/v1/operator/security":
                    return respond(
                        route,
                        {
                            "mfa_enabled": state["mfa_enabled"],
                            "recovery_codes_remaining": 8 if state["mfa_enabled"] else 0,
                            "sso_available": state["sso_available"],
                        },
                    )
                if path == "/api/v1/operator/security/mfa/enroll":
                    return respond(
                        route,
                        {
                            "enrollment_token": "generated-enrollment-token-0123456789",
                            "secret": "JBSWY3DPEHPK3PXP",
                            "otpauth_uri": "otpauth://totp/K5?secret=JBSWY3DPEHPK3PXP",
                            "expires_in_seconds": 300,
                        },
                    )
                if path == "/api/v1/operator/security/mfa/confirm":
                    state["mfa_enabled"] = True
                    state["reject_session"] = True
                    return respond(
                        route,
                        {
                            "mfa_enabled": True,
                            "recovery_codes": ["recovery-code-one", "recovery-code-two"],
                            "reauthentication_required": True,
                        },
                    )
                if path == "/api/v1/operator/security/mfa/disable":
                    state["mfa_enabled"] = False
                    return respond(route, {"mfa_enabled": False, "reauthentication_required": True})
                if path == "/api/v1/operator/security/mfa/recovery-codes":
                    return respond(
                        route, {"recovery_codes": ["replacement-code-one", "replacement-code-two"]}
                    )
                if path == "/api/v1/operator/admin/sso/provider":
                    if method == "PUT":
                        state["sso_provider"] = {
                            **body,
                            "configured": True,
                            "has_client_secret": bool(body.get("client_secret")),
                        }
                        state["sso_provider"].pop("client_secret", None)
                    return respond(route, state["sso_provider"])
                if path == "/api/v1/operator/admin/sso/provider/test":
                    return respond(
                        route,
                        {
                            "reachable": True,
                            "compatible": True,
                            "issuer_verified": True,
                            "pkce_s256": True,
                            "signing_algorithm": "RS256",
                        },
                    )
                if path == "/api/v1/operator/admin/sso/links":
                    if method == "POST":
                        state["sso_links"].append({"id": "generated-link", **body})
                    return respond(route, {"links": state["sso_links"]})
                if path.startswith("/api/v1/operator/admin/sso/links/"):
                    state["sso_links"].clear()
                    return respond(route, {}, 204)
                return respond(route, {}, 404)

            context.route("**/*", intercept)
            page.goto("http://k5.test/operator/")
            if not sso:
                page.locator("#username").fill("fixture-admin")
                page.locator("#password").fill("generated-password-0123456789")
                page.locator("#sign-in-button").click()
                if not mfa_login:
                    page.locator("#workspace").wait_for(state="visible")
            yield page, state, context
            context.close()
            browser.close()
        assert not errors, errors

    return launch


def open_settings(page, tab="storage"):
    page.locator("#admin-tab").click()
    page.locator("#admin-settings-tab").click()
    page.locator(f"#settings-{tab}-tab").click()
    page.locator(f"#settings-{tab}-panel").wait_for(state="visible")


def test_storage_policy_revision_and_pending_directory_are_explicit(workspace):
    with workspace() as (page, state, _):
        open_settings(page)
        page.locator("#storage-quota").fill("40")
        page.locator("#storage-retention").fill("7")
        page.locator("#storage-directory").fill("D:\\K5\\recordings")
        page.locator("#storage-save").click()
        page.get_by_text(
            "Storage policy saved. The directory change remains pending until a "
            "successful K5 restart migration.",
            exact=True,
        ).wait_for()
        assert state["storage"]["quota_bytes"] == 40 * _GIB
        assert state["storage"]["retention_seconds"] == 7 * 86400
        assert state["storage"]["recording_directory"] == "C:\\K5\\recordings"
        assert (
            "Active directory: C:\\K5\\recordings" in page.locator("#storage-health").inner_text()
        )
        assert (
            "Pending directory: D:\\K5\\recordings" in page.locator("#storage-health").inner_text()
        )


def test_storage_conflict_is_not_reported_saved(workspace):
    with workspace() as (page, state, _):
        open_settings(page)
        state["storage_conflict"] = True
        page.locator("#storage-quota").fill("30")
        page.locator("#storage-save").click()
        page.get_by_text(
            "Storage settings changed. Refresh before saving again.", exact=True
        ).wait_for()
        assert state["storage"]["quota_bytes"] == 20 * _GIB


def test_time_configuration_and_relative_camera_verification_are_separate(workspace):
    with workspace() as (page, state, _):
        open_settings(page, "time")
        page.locator("#time-save-host").click()
        page.get_by_text(
            "Configuration saved. Synchronization has not yet been verified.", exact=True
        ).wait_for()
        page.locator("#time-sync-host").click()
        page.get_by_text(
            "Windows Time resynchronization completed. Refresh time health to inspect the result.",
            exact=True,
        ).wait_for()
        assert state["time_sync_requests"] == 1
        assert page.locator("#time-sync-camera").is_disabled()
        page.locator("#time-confirm-camera").check()
        page.locator("#time-sync-camera").click()
        page.get_by_text(
            "Camera UTC was set and its relative offset verified. Absolute UTC "
            "accuracy still depends on the recorder's time reference.",
            exact=True,
        ).wait_for()
        assert not page.locator("#time-confirm-camera").is_checked()


def test_audit_append_cursor_filter_and_normal_download(workspace):
    with workspace() as (page, state, _):
        open_settings(page, "activity")
        page.get_by_role("cell", name="Started / incomplete", exact=True).wait_for()
        page.locator("#activity-older").click()
        page.wait_for_function("document.querySelector('#activity-older').disabled")
        assert state["audit_before"][-1] == "9"
        page.locator("#activity-action").fill("camera.recording")
        page.locator("#activity-outcome").select_option("started")
        page.locator("#activity-filter").click()
        with page.expect_download() as event:
            page.locator("#activity-export").click()
        download = event.value
        assert download.suggested_filename == "k5-activity.json"
        data = json.loads(Path(download.path()).read_text())
        assert data["events"][0]["action"] == "camera.recording"
        assert data["has_more"] is True


def test_provider_tokens_are_shown_once_and_handshake_is_truthful(workspace):
    with workspace() as (page, state, _):
        open_settings(page, "integrations")
        page.locator("#integration-name").fill("Fixture Neural")
        page.locator("#integration-kind").select_option("neural")
        page.locator(f"#integration-camera-choices input[value='{_CAMERA}']").check()
        page.locator("#integration-save").click()
        page.locator("#integration-setup-token").wait_for(state="visible")
        assert (
            page.locator("#integration-setup-token-value").inner_text()
            == "generated-provider-secret-shown-once-0123456789"
        )
        assert state["connections"][0]["enabled"] is False
        assert state["connections"][0]["camera_ids"] == [_CAMERA]
        assert not page.locator("#integration-token").input_value()
        page.get_by_role("button", name="Check provider handshake").click()
        page.get_by_text(
            "Waiting for a compatible provider handshake. A remote inference "
            "connection has not been established.",
            exact=True,
        ).wait_for()
        assert not page.evaluate(
            "Object.keys(localStorage).length || Object.keys(sessionStorage).length"
        )
        page.locator("#settings-storage-tab").click()
        page.locator("#settings-integrations-tab").click()
        assert page.locator("#integration-setup-token-value").inner_text() == "", (
            "A provider token must be cleared when leaving its panel"
        )


def test_custom_role_create_edit_delete_use_revision(workspace):
    with workspace() as (page, state, _):
        open_settings(page, "roles")
        page.locator("#roles-name").fill("Playback only")
        page.locator("#roles-permissions input[value='cameras.view_live']").uncheck()
        page.locator("#roles-save").click()
        page.get_by_text(
            "Custom role saved. Assign it from the account editor.", exact=True
        ).wait_for()
        assert state["roles"][0]["permissions"] == ["cameras.playback"]
        page.get_by_role("button", name="Edit role", exact=True).click()
        page.locator("#roles-description").fill("Only recorded footage")
        page.locator("#roles-save").click()
        page.get_by_text(
            "Custom role saved. Assign it from the account editor.", exact=True
        ).wait_for()
        assert state["roles"][0]["description"] == "Only recorded footage"
        page.get_by_role("button", name="Edit role", exact=True).click()
        page.locator("#roles-delete").click()
        page.get_by_text("Custom role removed.", exact=True).wait_for()
        assert not state["roles"]


def test_mfa_first_factor_never_opens_workspace_and_cancel_clears_challenge(workspace):
    with workspace(mfa_login=True) as (page, state, _):
        page.locator("#mfa-sign-in-form").wait_for(state="visible")
        assert page.locator("#workspace").is_hidden()
        assert not any(path == "/api/v1/auth/me" for _, path, _, _ in state["requests"])
        page.locator("#mfa-sign-in-code").fill("111111")
        page.locator("#mfa-sign-in-submit").click()
        page.get_by_text(
            "The code was not accepted. Enter a current authenticator code or unused "
            "recovery code.",
            exact=True,
        ).wait_for()
        assert page.locator("#workspace").is_hidden()
        page.locator("#mfa-sign-in-code").fill("123456")
        page.locator("#mfa-sign-in-submit").click()
        page.locator("#workspace").wait_for(state="visible")
        assert page.locator("#mfa-sign-in-code").input_value() == ""


def test_mfa_setup_displays_recovery_once_then_reauthenticates(workspace):
    with workspace() as (page, state, _):
        page.locator("#security-tab").click()
        page.locator("#security-password").fill("generated-password-0123456789")
        page.locator("#security-enroll").click()
        page.locator("#security-enrollment").wait_for(state="visible")
        assert page.locator("#security-password").input_value() == ""
        assert page.locator("#security-secret").inner_text() == "JBSWY3DPEHPK3PXP"
        page.locator("#security-confirm-code").fill("123456")
        page.locator("#security-confirm").click()
        page.locator("#security-recovery").wait_for(state="visible")
        assert page.locator("#security-secret").inner_text() == ""
        assert "recovery-code-one" in page.locator("#security-recovery-codes").inner_text()
        before = len(state["requests"])
        page.wait_for_timeout(3300)
        assert len(state["requests"]) == before, "Revoked-session background polling must pause"
        assert page.locator("#workspace").is_visible()
        assert "recovery-code-one" in page.locator("#security-recovery-codes").inner_text()
        page.locator("#settings-return-button").click()
        assert page.locator("#security-recovery").is_visible()
        page.locator("#security-tab").click()
        page.locator("#settings-security-tab").click()
        assert "recovery-code-one" in page.locator("#security-recovery-codes").inner_text()
        assert len(state["requests"]) == before
        page.locator("#security-recovery-done").click()
        page.locator("#sign-in").wait_for(state="visible")
        assert state["mfa_enabled"] is True
        assert page.locator("#security-recovery-codes").inner_text() == ""
        assert not page.evaluate(
            "Object.keys(localStorage).length || Object.keys(sessionStorage).length"
        )


def test_viewer_can_manage_own_security_without_admin_panels(workspace):
    with workspace(role="viewer", permissions=["cameras.view_live", "cameras.playback"]) as (
        page,
        _,
        _,
    ):
        assert page.locator("#admin-tab").is_hidden()
        page.locator("#security-tab").click()
        page.locator("#settings-security-panel").wait_for(state="visible")
        for name in ("storage", "time", "activity", "integrations", "roles", "sso"):
            assert page.locator(f"#settings-{name}-tab").is_hidden()
        page.locator("#settings-return-button").click()
        assert page.locator("#camera-workspace").is_visible()


def test_organization_signin_one_time_code_is_removed_before_exchange(workspace):
    with workspace(sso=True) as (page, state, _):
        page.locator("#sso-sign-in").wait_for(state="visible")
        page.locator("#sso-sign-in-button").click()
        page.locator("#workspace").wait_for(state="visible")
        assert "sso_code" not in page.url
        complete = [
            body for _, path, body, _ in state["requests"] if path == "/api/v1/auth/oidc/complete"
        ]
        assert complete == [{"code": "generated-one-time-sso-code-0123456789"}]


def test_sso_provider_secret_is_write_only_and_identity_link_is_explicit(workspace):
    with workspace() as (page, state, _):
        open_settings(page, "sso")
        page.locator("#sso-display-name").fill("Fixture organization")
        page.locator("#sso-issuer").fill("https://identity.example.test")
        page.locator("#sso-client-id").fill("fixture-client")
        page.locator("#sso-client-secret").fill("generated-client-secret")
        page.locator("#sso-enabled").check()
        page.locator("#sso-save").click()
        page.get_by_text(
            "Identity-provider configuration saved. Test the provider and link "
            "accounts before relying on organization sign-in.",
            exact=True,
        ).wait_for()
        assert page.locator("#sso-client-secret").input_value() == ""
        assert "client_secret" not in state["sso_provider"]
        page.locator("#sso-test").click()
        page.get_by_text(
            "The identity provider passed issuer, PKCE and signing-capability "
            "verification. A linked user's actual sign-in must still complete "
            "normally.",
            exact=True,
        ).wait_for()
        page.locator("#sso-link-subject").fill("stable-provider-subject")
        page.locator("#sso-link-user").select_option(_ADMIN)
        page.locator("#sso-link-group").fill("K5 operators")
        page.locator("#sso-link-save").click()
        page.get_by_text(
            "Organization identity linked to the existing account. Its current role, "
            "camera access and MFA still apply.",
            exact=True,
        ).wait_for()
        assert state["sso_links"][0]["subject"] == "stable-provider-subject"
        assert state["sso_links"][0]["required_group"] == "K5 operators"


def test_custom_permissions_hide_ungranted_admin_and_export_controls(workspace):
    with workspace(permissions=["time.manage"]) as (page, state, _):
        assert page.locator("#admin-tab").inner_text() == "Recorder settings"
        page.locator("#admin-tab").click()
        page.locator("#settings-time-panel").wait_for(state="visible")
        assert page.locator("#settings-storage-tab").is_hidden()
        assert page.locator("#settings-roles-tab").is_hidden()
        assert page.locator("#export-panel").is_hidden()
        assert page.locator("#add-camera-button").is_hidden()
        assert not any(p == "/api/v1/operator/admin/users" for _, p, _, _ in state["requests"])


def test_separate_camera_management_account_is_one_operation_only(workspace):
    with workspace() as (page, state, _):
        open_settings(page, "time")
        page.locator("#time-camera-use-account").check()
        page.locator("#time-camera-username").fill("generated-management-user")
        page.locator("#time-camera-password").fill("generated-management-secret")
        page.locator("#time-refresh-camera").click()
        page.wait_for_function("!document.querySelector('#time-refresh-camera').disabled")
        calls = [b for m, p, b, _ in state["requests"] if p.endswith("/time/status")]
        assert calls == [
            {"username": "generated-management-user", "password": "generated-management-secret"}
        ]
        assert page.locator("#time-camera-username").input_value() == ""
        assert page.locator("#time-camera-password").input_value() == ""
        assert not page.locator("#time-camera-use-account").is_checked()
        page.locator("#time-refresh-camera").click()
        page.wait_for_function("!document.querySelector('#time-refresh-camera').disabled")
        assert len([p for _, p, _, _ in state["requests"] if p.endswith("/time/status")]) == 1


def test_cancelled_mfa_challenge_cannot_be_submitted_from_hidden_form(workspace):
    with workspace(mfa_login=True) as (page, state, _):
        page.locator("#mfa-sign-in-form").wait_for(state="visible")
        page.locator("#mfa-sign-in-code").fill("123456")
        page.locator("#mfa-sign-in-cancel").click()
        page.locator("#sign-in-form").wait_for(state="visible")
        page.locator("#mfa-sign-in-form").evaluate(
            "e=>e.dispatchEvent(new Event('submit',{cancelable:true,bubbles:true}))"
        )
        assert not any(p == "/api/v1/auth/mfa" for _, p, _, _ in state["requests"])
        assert page.locator("#mfa-sign-in-code").input_value() == ""


def test_account_role_profile_application_is_explicit_and_revokes_target_scope(workspace):
    with workspace() as (page, state, _):
        target = "e782ebc7-82b3-4bf1-a27b-280c71c22a73"
        state["users"].append(
            {
                "id": target,
                "username": "fixture-operator",
                "display_name": "Fixture operator",
                "role": "operator",
                "enabled": True,
                "credential_status": "active",
                "camera_access": {"mode": "selected", "camera_ids": [_CAMERA]},
                "role_profile_id": None,
            }
        )
        state["roles"] = [
            {
                "id": _ROLE,
                "name": "Recorded footage only",
                "description": "",
                "base_role": "viewer",
                "permissions": ["cameras.playback"],
            }
        ]
        page.locator("#admin-tab").click()
        row = page.locator(f"tr[data-user-id='{target}']")
        row.get_by_role("button", name="Edit account", exact=True).click()
        page.locator("#admin-role-profile").select_option(_ROLE)
        assert (
            "explicitly changes this account to the viewer base role"
            in page.locator("#admin-role-profile-help").inner_text()
        )
        page.locator("#admin-apply-role-profile").click()
        page.get_by_text(
            "Role profile applied. The account's previous sessions were revoked.", exact=True
        ).wait_for()
        account = next(u for u in state["users"] if u["id"] == target)
        assert account["role_profile_id"] == _ROLE
        assert account["role"] == "viewer"
        assert account["effective_permissions"] == ["cameras.playback"]
        row.get_by_role("button", name="Edit account", exact=True).click()
        assert page.locator("#admin-role").is_disabled()
        page.locator("#admin-display-name").fill("Updated profiled operator")
        page.locator("#admin-save-user").click()
        page.locator("#admin-user-dialog").wait_for(state="hidden")
        assert account["display_name"] == "Updated profiled operator"
        updates = [b for m, p, b, _ in state["requests"] if m == "PATCH" and p.endswith(target)]
        assert len(updates) == 1 and "role" not in updates[0]


def test_camera_refresh_keeps_settings_and_cannot_retarget_camera_time(workspace):
    with workspace() as (page, state, _):
        page.locator(".camera-item").first.click()
        open_settings(page, "time")
        page.wait_for_function("document.querySelector('#time-camera').value !== ''")
        assert page.locator("#time-camera").input_value() == _CAMERA
        page.locator("#time-camera-use-account").check()
        page.locator("#time-camera-username").fill("generated-management-user")
        page.locator("#time-camera-password").fill("generated-management-secret")
        previous = dict(state["cameras"][0])
        state["cameras"].clear()
        page.wait_for_function("document.querySelector('#time-camera').value === ''")
        assert page.locator("#settings-panel").is_visible()
        assert page.locator("#settings-time-panel").is_visible()
        assert page.locator("#time-save-camera").is_disabled()
        assert page.locator("#time-sync-camera").is_disabled()
        assert page.locator("#time-camera-password").input_value() == ""
        assert page.locator("#time-camera-username").input_value() == ""
        previous["device_id"] = "649dfe8f-7173-4e0f-b97f-a8daf4962d19"
        state["cameras"].append(previous)
        page.wait_for_function("document.querySelector('#time-camera').options.length === 2")
        assert page.locator("#time-camera").input_value() == ""
        assert page.locator("#time-save-camera").is_disabled()


def test_exhausted_provider_deliveries_can_be_retried_from_gui(workspace):
    with workspace() as (page, state, _):
        state["connections"] = [
            {
                "id": _CONNECTION,
                "name": "Fixture provider",
                "kind": "analytics",
                "origin": "",
                "enabled": True,
                "credential_configured": True,
                "status": "capacity",
                "last_test_utc": None,
                "queued_events": 2,
                "delivered_events": 0,
                "exhausted_events": 2,
                "camera_ids": [_CAMERA],
            }
        ]
        open_settings(page, "integrations")
        page.get_by_role("button", name="Retry exhausted deliveries", exact=True).click()
        page.get_by_text(
            "Delivery retry budget renewed. Original observation identifiers are "
            "preserved; monitor provider delivery status.",
            exact=True,
        ).wait_for()
        assert state["connections"][0]["exhausted_events"] == 0
        assert any(
            m == "POST" and p == f"/api/v1/operator/admin/integrations/{_CONNECTION}/retry"
            for m, p, _, _ in state["requests"]
        )
        assert (
            page.get_by_role("button", name="Retry exhausted deliveries", exact=True).count() == 0
        )


def test_operator_permissions_come_from_inventory_not_core_auth_account(workspace):
    with workspace(role="operator", permissions=["cameras.view_live", "cameras.playback"]) as (
        page,
        state,
        _,
    ):
        assert page.locator("#admin-tab").is_hidden()
        assert page.locator("#export-panel").is_hidden()
        assert page.locator("#clip-mark-in").is_hidden()
        assert page.locator("#download-recording").is_hidden()
        assert page.locator("#add-camera-button").is_hidden()
        page.locator(".camera-item").first.click()
        assert page.locator("#history-tab").is_enabled()
        assert page.locator("#update-source-button").is_hidden()
        assert page.locator("#remove-camera-button").is_hidden()
        assert not any(p.endswith("/capabilities") for _, p, _, _ in state["requests"])


@pytest.mark.parametrize("fault", ["missing", "foreign", "contradictory", "expanded"])
def test_unverified_inventory_pauses_changes_and_preserves_open_settings(workspace, fault):
    with workspace() as (page, state, _):
        open_settings(page)
        page.locator("#storage-quota").fill("37")
        state["capability_fault"] = fault
        page.wait_for_function("document.querySelector('#storage-save').disabled")
        assert page.locator("#settings-storage-panel").is_visible()
        assert page.locator("#storage-quota").input_value() == "37"
        assert page.locator("#admin-tab").is_hidden()
        assert page.locator("#add-camera-button").is_hidden()
        assert page.locator("#export-panel").is_hidden()
        assert page.locator("#download-recording").is_hidden()
        assert page.locator(".camera-item").first.is_disabled()
        state["capability_fault"] = None
        page.wait_for_function("!document.querySelector('#storage-save').disabled")
        assert page.locator("#settings-storage-panel").is_visible()
        assert page.locator("#storage-quota").input_value() == "37"
        assert not any(m == "PATCH" and p.endswith("/storage") for m, p, _, _ in state["requests"])


def test_capability_refresh_preserves_open_user_editor(workspace):
    with workspace() as (page, state, _):
        page.locator("#admin-tab").click()
        page.locator(f'tr[data-user-id="{_ADMIN}"] [data-user-action="edit"]').click()
        page.locator("#admin-display-name").fill("Unsaved account name")
        state["capability_fault"] = "missing"
        page.wait_for_function("document.querySelector('#admin-save-user').disabled")
        assert page.locator("#admin-user-dialog").is_visible()
        assert page.locator("#admin-display-name").input_value() == "Unsaved account name"
        state["capability_fault"] = None
        # The modal owns pointer focus; normal background refresh still runs.
        page.wait_for_function("!document.querySelector('#admin-save-user').disabled")
        assert page.locator("#admin-display-name").input_value() == "Unsaved account name"
        page.locator("#admin-cancel-user").click()


def test_narrow_user_manager_cannot_change_provider_or_link_stronger_account(workspace):
    with workspace(permissions=["users.manage"]) as (page, state, _):
        stronger = _ui_inventory({"username": "full-admin", "role": "administrator"}, [])[
            "capabilities"
        ]["account"]
        stronger.update(
            id=_ROLE, credential_status="active", camera_access={"mode": "all", "camera_ids": []}
        )
        state["users"].append(stronger)
        state["sso_links"] = [
            {
                "id": "strong-link",
                "issuer": "https://identity.example.test",
                "subject": "full",
                "user_id": _ROLE,
            }
        ]
        open_settings(page, "sso")
        page.wait_for_function(
            "document.querySelector('#settings-sso-panel').getAttribute('aria-busy') === 'false'"
        )
        assert page.locator("#sso-save").is_disabled()
        assert page.locator("#sso-client-secret").is_disabled()
        assert page.locator("#sso-authority").is_visible()
        assert page.locator(f'#sso-link-user option[value="{_ROLE}"]').is_disabled()
        assert not page.locator(f'#sso-link-user option[value="{_ADMIN}"]').is_disabled()
        assert page.locator("#sso-unlink-strong-link").is_disabled()
        page.locator("#sso-provider-form").evaluate(
            "e=>e.dispatchEvent(new Event('submit',{cancelable:true,bubbles:true}))"
        )
        page.locator("#sso-unlink-strong-link").evaluate(
            "e=>e.dispatchEvent(new MouseEvent('click',{bubbles:true}))"
        )
        assert not any(m in {"PUT", "DELETE"} and "/sso/" in p for m, p, _, _ in state["requests"])


def test_no_privileged_action_before_canonical_inventory_arrives(workspace):
    with workspace(hold_inventory=True) as (page, state, _):
        assert page.locator("#workspace").is_visible()
        assert page.locator("#admin-tab").is_hidden()
        assert page.locator("#add-camera-button").is_hidden()
        assert page.locator("#security-tab").is_hidden()
        assert page.locator("#export-panel").is_hidden()
        page.wait_for_function(
            "document.querySelector('#account-role').textContent === 'administrator'"
        )
        # Playwright dispatches route callbacks asynchronously after the role
        # label appears. Wait for the actual held inventory request, while all
        # privileged UI controls remain hidden, before releasing the response.
        for _ in range(40):
            if state["held_inventory"]:
                break
            page.wait_for_timeout(50)
        assert state["held_inventory"]
        assert page.locator("#admin-tab").is_hidden()
        assert page.locator("#add-camera-button").is_hidden()
        state["hold_inventory"] = False
        route, payload = state["held_inventory"].pop()
        route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))
        page.locator("#admin-tab").wait_for(state="visible")
        assert page.locator("#add-camera-button").is_visible()
        assert page.locator("#security-tab").is_visible()


@pytest.mark.parametrize("refresh_status", [200, 503])
def test_profile_unbind_blocks_stale_editor_until_fresh_list_succeeds(workspace, refresh_status):
    with workspace() as (page, state, _):
        target = "e782ebc7-82b3-4bf1-a27b-280c71c22a73"
        state["users"].append(
            {
                "id": target,
                "username": "fixture-profiled",
                "display_name": "Fixture profiled",
                "role": "administrator",
                "enabled": True,
                "credential_status": "active",
                "camera_access": {"mode": "all", "camera_ids": []},
                "role_profile_id": _ROLE,
                "effective_permissions": ["time.manage"],
            }
        )
        state["roles"] = [
            {
                "id": _ROLE,
                "name": "Time only",
                "description": "",
                "base_role": "administrator",
                "permissions": ["time.manage"],
            }
        ]
        page.locator("#admin-tab").click()
        row = page.locator(f'tr[data-user-id="{target}"]')
        row.locator('[data-user-action="edit"]').click()
        page.locator("#admin-role-profile").select_option("")
        pending = []
        page.route(
            "**/api/v1/operator/admin/users?**", lambda route: pending.append(route), times=1
        )
        with page.expect_response(
            lambda response: (
                response.request.method == "PUT"
                and response.url.endswith(f"/users/{target}/role-profile")
            )
        ) as changed:
            page.locator("#admin-apply-role-profile").click()
        assert changed.value.status == 200
        page.locator("#admin-user-dialog").wait_for(state="hidden")
        edit = row.locator('[data-user-action="edit"]')
        assert edit.is_disabled()
        assert row.locator('[data-user-action="reset"]').is_disabled()
        assert page.locator("#admin-create-user").is_disabled()
        edit.evaluate("e=>e.dispatchEvent(new MouseEvent('click',{bubbles:true}))")
        assert page.locator("#admin-user-dialog").is_hidden()
        assert pending
        pending.pop().fulfill(
            status=refresh_status,
            content_type="application/json",
            body=json.dumps(
                {"users": state["users"], "has_more": False}
                if refresh_status == 200
                else {"detail": "unavailable"}
            ),
        )
        if refresh_status != 200:
            page.wait_for_function("!document.querySelector('#admin-refresh-users').disabled")
            assert edit.is_disabled()
            assert (
                "Account actions are paused. Select Refresh"
                in page.locator("#admin-message").inner_text()
            )
            edit.evaluate("e=>e.dispatchEvent(new MouseEvent('click',{bubbles:true}))")
            assert page.locator("#admin-user-dialog").is_hidden()
            page.locator("#admin-refresh-users").click()
        page.wait_for_function(
            "id=>!document.querySelector('tr[data-user-id=\"'+id+'\"] "
            "[data-user-action=edit]').disabled",
            arg=target,
        )
        assert page.locator(f'tr[data-user-id="{_ADMIN}"] [data-user-action="reset"]').is_disabled()
        edit.click()
        assert page.locator("#admin-role").is_enabled()
        assert page.locator("#admin-role-profile").input_value() == ""


@pytest.mark.parametrize("profiles_status", [200, 503])
def test_profile_choice_waits_for_current_metadata_before_selection(workspace, profiles_status):
    with workspace() as (page, state, _):
        target = "e782ebc7-82b3-4bf1-a27b-280c71c22a73"
        state["users"].append(
            {
                "id": target,
                "username": "fixture-profiled",
                "display_name": "Fixture profiled",
                "role": "administrator",
                "enabled": True,
                "credential_status": "active",
                "camera_access": {"mode": "all", "camera_ids": []},
                "role_profile_id": _ROLE,
                "effective_permissions": ["time.manage"],
            }
        )
        state["roles"] = [
            {
                "id": _ROLE,
                "name": "Time only",
                "description": "",
                "base_role": "administrator",
                "permissions": ["time.manage"],
            }
        ]
        page.locator("#admin-tab").click()
        row = page.locator(f'tr[data-user-id="{target}"]')
        row.locator('[data-user-action="edit"]').click()
        page.wait_for_function("!document.querySelector('#admin-apply-role-profile').disabled")
        page.locator("#admin-cancel-user").click()
        pending = []
        page.route("**/api/v1/operator/admin/roles", lambda r: pending.append(r), times=1)
        row.locator('[data-user-action="edit"]').click()
        assert page.locator("#admin-role-profile").is_disabled()
        assert page.locator("#admin-apply-role-profile").is_disabled()
        page.locator("#admin-apply-role-profile").evaluate(
            "e=>e.dispatchEvent(new MouseEvent('click',{bubbles:true}))"
        )
        assert not any(
            m == "PUT" and p.endswith("/role-profile") for m, p, _, _ in state["requests"]
        )
        assert pending
        pending.pop().fulfill(
            status=profiles_status,
            content_type="application/json",
            body=json.dumps(
                {"revision": state["role_revision"], "roles": state["roles"]}
                if profiles_status == 200
                else {"detail": "roles_unavailable"}
            ),
        )
        if profiles_status != 200:
            page.get_by_text(
                "Custom roles are unavailable. Built-in account fields remain available.",
                exact=True,
            ).wait_for()
            assert page.locator("#admin-role-profile").is_disabled()
            assert page.locator("#admin-apply-role-profile").is_disabled()
            page.locator("#admin-cancel-user").click()
            row.locator('[data-user-action="edit"]').click()
        page.locator("#admin-role-profile").select_option("")
        page.locator("#admin-apply-role-profile").click()
        page.get_by_text(
            "Role profile applied. The account's previous sessions were revoked.", exact=True
        ).wait_for()
        requests = [
            b for m, p, b, _ in state["requests"] if m == "PUT" and p.endswith("/role-profile")
        ]
        assert len(requests) == 1 and requests[0]["role_profile_id"] is None
