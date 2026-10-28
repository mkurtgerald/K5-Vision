"""Actual Chromium checks for account administration and credential lifecycle."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import pytest

_ASSETS = Path(__file__).resolve().parents[2] / "src/k5vision/operator_web"
_ADMIN_ID = "a22c22bc-4089-45b7-a370-f360bb9d33c0"
_CAMERA = "e4fd401d-ae8e-4ac2-9f92-e8287f241532"
_CODE = "Generated-One-Time-Credential-0123456789"


def test_administration_assets_and_first_login_are_in_the_shipped_workspace():
    html = (_ASSETS / "index.html").read_text(encoding="utf-8")
    assert "/operator/admin.js" in html and "/operator/admin.css" in html
    assert 'id="admin-tab"' in html and 'id="admin-panel"' in html
    assert 'id="activate-account-form"' in html
    assert 'id="admin-temporary-credential"' in html
    script = (_ASSETS / "admin.js").read_text(encoding="utf-8")
    assert "localStorage" not in script and "sessionStorage" not in script
    assert "innerHTML" not in script


@pytest.fixture
def workspace():
    if os.getenv("K5_OPERATOR_BROWSER_ACCEPTANCE") != "1":
        pytest.skip("Administration UI proof requires the explicit browser environment")
    browser_api = pytest.importorskip("playwright.sync_api")

    @contextmanager
    def launch(role="administrator", *, hold_initial_cameras=False):
        account = {
            "id": _ADMIN_ID,
            "username": "fixture-admin",
            "display_name": "Fixture administrator",
            "role": role,
            "enabled": True,
            "credential_status": "active",
            "camera_access": {"mode": "all", "camera_ids": []},
        }
        cameras = [
            {
                "device_id": _CAMERA,
                "name": "Generated entrance camera",
                "host": "127.0.0.1",
                "status": "live",
                "recording": True,
                "recording_enabled": True,
                "recording_state": "recording",
                "analytics_status": "disabled",
            }
        ]
        state = {
            "users": [account.copy()],
            "requests": [],
            "bootstrap_status": 204,
            "last_admin_error": False,
            "hold_create": False,
            "held": None,
            "expire": False,
            "hold_initial_cameras": hold_initial_cameras,
            "held_inventory": None,
        }
        channel = os.getenv(
            "K5_OPERATOR_BROWSER_CHANNEL", "msedge" if os.name == "nt" else "chromium"
        )
        errors = []
        with browser_api.sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel=channel, headless=True)
            context = browser.new_context(viewport={"width": 1440, "height": 900})
            context.add_init_script("window.Hls = {isSupported: () => false};")
            page = context.new_page()
            page.set_default_timeout(8000)
            page.on("pageerror", lambda _error: errors.append("uncaught_script_error"))

            def respond(route, data, status=200):
                route.fulfill(
                    status=status,
                    content_type="application/json",
                    body="" if status == 204 else json.dumps(data),
                )

            def issue(row):
                return {
                    "account": row,
                    "temporary_credential": _CODE,
                    "expires_at": (datetime.now(UTC) + timedelta(minutes=30)).isoformat(),
                }

            def intercept(route):
                path = urlsplit(route.request.url).path
                method = route.request.method
                if path.startswith("/operator/"):
                    name = path.rsplit("/", 1)[1] or "index.html"
                    if name not in {
                        "index.html",
                        "app.js",
                        "app.css",
                        "wall.js",
                        "wall.css",
                        "timeline.js",
                        "timeline.css",
                        "admin.js",
                        "admin.css",
                        "analytics.js",
                        "analytics.css",
                    }:
                        return respond(route, {}, 404)
                    content_type = {
                        ".html": "text/html",
                        ".js": "application/javascript",
                        ".css": "text/css",
                    }[Path(name).suffix]
                    return route.fulfill(
                        content_type=content_type, body=(_ASSETS / name).read_text(encoding="utf-8")
                    )
                body = route.request.post_data_json if route.request.post_data else None
                state["requests"].append((method, path, body))
                if path == "/api/v1/auth/bootstrap-password":
                    return respond(route, {}, state["bootstrap_status"])
                if path == "/api/v1/auth/login":
                    return respond(
                        route,
                        {
                            "session_token": "generated-admin-ui-session-token-012345678901234567",
                            "account": account,
                            "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
                            "expires_in_seconds": 300,
                        },
                    )
                if path == "/api/v1/auth/me":
                    return respond(route, account)
                if path in {"/api/v1/auth/logout", "/api/v1/operator/media-session"}:
                    return respond(route, {}, 204)
                if path == "/api/v1/operator/status":
                    return respond(
                        route,
                        {"worker_status": "ready", "server_utc": datetime.now(UTC).isoformat()},
                    )
                if path == "/api/v1/operator/cameras":
                    if state["hold_initial_cameras"]:
                        state["hold_initial_cameras"] = False
                        state["held_inventory"] = (
                            route,
                            {"cameras": cameras, "worker_status": "ready"},
                        )
                        return None
                    return (
                        respond(route, {"detail": "Unauthorized"}, 401)
                        if state["expire"]
                        else respond(route, {"cameras": cameras, "worker_status": "ready"})
                    )
                if path == "/api/v1/operator/admin/users" and method == "GET":
                    return respond(route, {"users": state["users"], "has_more": False})
                if path == "/api/v1/operator/admin/users" and method == "POST":
                    row = dict(body, id=str(uuid4()), credential_status="pending")
                    state["users"].append(row)
                    if state["hold_create"]:
                        state["held"] = (route, issue(row))
                        return None
                    return respond(route, issue(row), 201)
                if path.startswith("/api/v1/operator/admin/users/"):
                    identifier = path.split("/")[6]
                    row = next(user for user in state["users"] if user["id"] == identifier)
                    if method == "PATCH":
                        if state["last_admin_error"]:
                            return respond(route, {"detail": "admin_last_administrator"}, 409)
                        row.update(body)
                        return respond(route, row)
                    if method == "POST" and path.endswith("/reset-password"):
                        row["credential_status"] = "pending"
                        return respond(route, issue(row))
                if path == "/api/v1/operator/admin/audit":
                    return respond(
                        route,
                        {
                            "events": [
                                {
                                    "event_id": 1,
                                    "user_id": _ADMIN_ID,
                                    "actor": "fixture-admin",
                                    "action": "updated",
                                    "changed_fields": ["camera_access"],
                                    "occurred_at": datetime.now(UTC).isoformat(),
                                }
                            ],
                            "has_more": False,
                        },
                    )
                if path.endswith("/media-session"):
                    return respond(
                        route,
                        {
                            "live_url": f"/media/live/{_CAMERA}/index.m3u8",
                            "playback_url": f"/media/playback?path={_CAMERA}",
                        },
                    )
                return respond(route, {}, 404)

            page.route("**/*", intercept)
            page.goto("http://127.0.0.1:18431/operator/")
            try:
                yield page, state, account, errors
            finally:
                context.close()
                browser.close()

    return launch


def _login(page):
    page.locator("#username").fill("fixture-admin")
    page.locator("#password").fill("Generated password 1234")
    page.locator("#sign-in-button").click()
    page.locator("#workspace").wait_for(state="visible")


def _create(page):
    page.locator("#admin-create-user").click()
    page.locator("#admin-username").fill("generated-viewer")
    page.locator("#admin-display-name").fill("Generated viewer")
    page.locator("#admin-role").select_option("viewer")
    page.locator("#admin-access-mode").select_option("selected")
    page.locator(f'#admin-camera-choices input[value="{_CAMERA}"]').check()
    page.locator("#admin-save-user").click()


def test_admin_create_edit_reset_audit_and_last_admin_protection(workspace):
    with workspace() as (page, state, _account, errors):
        _login(page)
        page.locator("#admin-tab").click()
        page.locator("#admin-users-list tr[data-user-id]").wait_for()
        _create(page)
        page.locator("#admin-credential-dialog").wait_for(state="visible")
        assert page.locator("#admin-temporary-credential").input_value() == _CODE
        assert state["users"][-1]["camera_access"] == {"mode": "selected", "camera_ids": [_CAMERA]}
        page.locator("#admin-credential-done").click()
        assert page.locator("#admin-temporary-credential").input_value() == ""
        row = page.locator("#admin-users-list tr").filter(has_text="generated-viewer")
        row.locator('[data-user-action="edit"]').click()
        page.locator("#admin-enabled").uncheck()
        page.locator(f'#admin-camera-choices input[value="{_CAMERA}"]').uncheck()
        page.locator("#admin-save-user").click()
        page.locator("#admin-user-dialog").wait_for(state="hidden")
        assert state["users"][-1]["enabled"] is False
        assert state["users"][-1]["camera_access"] == {"mode": "selected", "camera_ids": []}
        row.locator('[data-user-action="reset"]').click()
        page.locator("#admin-confirm-reset").click()
        page.locator("#admin-credential-dialog").wait_for(state="visible")
        page.locator("#admin-credential-done").click()
        own = page.locator(f'#admin-users-list tr[data-user-id="{_ADMIN_ID}"]')
        assert own.locator('[data-user-action="reset"]').is_disabled()
        own.locator('[data-user-action="edit"]').click()
        page.locator("#admin-display-name").fill("Updated administrator")
        page.locator("#admin-save-user").click()
        page.locator("#admin-user-dialog").wait_for(state="hidden")
        assert page.locator("#workspace").is_visible()
        assert page.locator("#account-name").inner_text() == "Updated administrator"
        state["last_admin_error"] = True
        own.locator('[data-user-action="edit"]').click()
        page.locator("#admin-role").select_option("viewer")
        page.locator("#admin-save-user").click()
        page.locator("#admin-user-message").filter(
            has_text="another enabled administrator"
        ).wait_for()
        assert page.locator("#admin-user-dialog").is_visible()
        page.locator("#admin-cancel-user").click()
        page.locator("#admin-audit-tab").click()
        page.locator('#admin-audit-list tr[data-event-id="1"]').wait_for()
        assert "fixture-admin" in page.locator("#admin-audit-list").inner_text()
        assert _CODE not in page.locator("#admin-audit-list").inner_text()
        assert (
            page.evaluate("Object.keys(localStorage).length + Object.keys(sessionStorage).length")
            == 0
        )
        assert not errors


def test_first_login_activation_has_confirmation_and_clears_all_secrets(workspace):
    with workspace() as (page, state, _account, errors):
        page.locator("#activate-account-button").click()
        page.locator("#activation-username").fill("generated-viewer")
        page.locator("#activation-credential").fill(_CODE)
        page.locator("#activation-password").fill("Generated new password 1234")
        page.locator("#activation-confirm").fill("Different new password 1234")
        page.locator("#activate-account-submit").click()
        assert "do not match" in page.locator("#activation-message").inner_text()
        assert not any(
            path.endswith("bootstrap-password") for _method, path, _body in state["requests"]
        )
        page.locator("#activation-confirm").fill("Generated new password 1234")
        page.locator("#activate-account-submit").click()
        page.locator("#sign-in-form").wait_for(state="visible")
        assert page.locator("#username").input_value() == "generated-viewer"
        assert page.locator("#password").input_value() == ""
        for identifier in ("activation-credential", "activation-password", "activation-confirm"):
            assert page.locator("#" + identifier).input_value() == ""
        state["bootstrap_status"] = 401
        page.locator("#activate-account-button").click()
        page.locator("#activation-credential").fill(_CODE)
        page.locator("#activation-password").fill("Generated new password 1234")
        page.locator("#activation-confirm").fill("Generated new password 1234")
        page.locator("#activate-account-submit").click()
        page.locator("#activation-message").filter(has_text="invalid or expired").wait_for()
        assert page.locator("#activation-credential").input_value() == ""
        assert not errors


def test_viewer_controls_are_read_only_and_admin_request_cannot_reveal_after_expiry(workspace):
    with workspace("viewer") as (page, _state, _account, errors):
        _login(page)
        assert page.locator("#admin-tab").is_hidden()
        assert page.locator("#add-camera-button").is_hidden()
        assert page.locator("#download-recording").is_hidden()
        assert page.locator("#clip-mark-in").is_hidden()
        assert not errors

    with workspace() as (page, state, _account, errors):
        _login(page)
        page.locator("#admin-tab").click()
        page.locator("#admin-users-list tr").wait_for()
        state["hold_create"] = True
        _create(page)
        page.wait_for_timeout(100)
        assert state["held"] is not None
        state["expire"] = True
        page.locator("#workspace").wait_for(state="hidden", timeout=7000)
        route, issue = state["held"]
        try:
            route.fulfill(status=201, content_type="application/json", body=json.dumps(issue))
        except Exception:
            pass  # The real UI aborted this request when its session expired.
        page.wait_for_timeout(100)
        assert page.locator("#admin-credential-dialog").is_hidden()
        assert page.locator("#admin-temporary-credential").input_value() == ""
        assert page.locator("#admin-users-list").inner_text() == ""
        assert not errors


def test_late_initial_camera_inventory_preserves_administration_and_open_permissions_editor(
    workspace,
):
    """A slow first inventory must not undo the administrator's later navigation."""
    with workspace(hold_initial_cameras=True) as (page, state, _account, errors):
        viewer_id = "4a45619d-74db-4e60-a003-c4929c98cf49"
        state["users"].append(
            {
                "id": viewer_id,
                "username": "persisted-viewer",
                "display_name": "Persisted viewer",
                "role": "viewer",
                "enabled": True,
                "credential_status": "active",
                "camera_access": {"mode": "selected", "camera_ids": [_CAMERA]},
            }
        )
        _login(page)
        page.locator("#admin-tab").click()
        viewer = page.locator(f'#admin-users-list tr[data-user-id="{viewer_id}"]')
        viewer.locator('[data-user-action="edit"]').click()
        selection = page.locator(f'#admin-camera-choices input[value="{_CAMERA}"]')
        assert selection.is_checked()
        assert "Unavailable camera" in page.locator("#admin-camera-choices").inner_text()
        route, inventory = state["held_inventory"]
        route.fulfill(content_type="application/json", body=json.dumps(inventory))
        page.locator("#admin-camera-choices").filter(
            has_text="Generated entrance camera"
        ).wait_for()
        assert page.locator("#admin-panel").is_visible()
        assert page.locator("#camera-workspace").is_hidden()
        assert page.locator("#admin-user-dialog").is_visible()
        assert page.locator("#admin-display-name").input_value() == "Persisted viewer"
        assert selection.is_checked()
        assert not any(
            method == "POST" and path.endswith("/media-session")
            for method, path, _body in state["requests"]
        )
        assert not errors
