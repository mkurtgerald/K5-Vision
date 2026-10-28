"""Real browser account/permission acceptance using generated camera media only.

The shared helpers also run against the actual installed Windows application.
Temporary credentials and test passwords stay in private fixture memory; receipt
properties contain only bounded counts and outcomes. Linux is a regression,
while the Windows lane uses installed Edge and the native worker executables.
"""

from __future__ import annotations

import importlib.util
import json
import os
import platform
import secrets
import shutil
import socket
import subprocess
import time
from datetime import UTC, datetime, timedelta
from importlib.metadata import version
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from uuid import UUID

import pytest

from k5vision import identity_state

_TOOLS = ("K5_MEDIAMTX_DONOR_EXE", "K5_CADDY_DONOR_EXE", "K5_DONOR_FFMPEG", "K5_DONOR_FFPROBE")
_PLAYWRIGHT_VERSION = "1.58.0"
_BROWSER_HELPERS = None
_ADMIN_FAILURE_CODE = None
_ADMIN_RESTART_DIAGNOSTIC = {}
_FAILURE_MESSAGES = {
    "Restart changed a durable user identity": "restart_user_identity",
    "Restart changed the user's durable role, enabled state or access mode": "restart_role_state",
    "Restart changed the user's selected camera grants": "restart_camera_grants",
    "Restarted viewer inventory exceeded its persisted grants": "restart_viewer_inventory",
    "Restart granted the viewer an unassigned camera": "restart_viewer_boundary",
    "Generated user could not sign in through the UI": "user_ui_login",
    "Delayed initial inventory navigated away from administration": "late_login_navigation",
}
pytestmark = pytest.mark.skipif(
    os.getenv("K5_OPERATOR_BROWSER_ACCEPTANCE") != "1"
    or not all(os.getenv(name) for name in _TOOLS),
    reason="Admin browser qualification requires explicit opt-in and native development tools",
)


def _browser_helpers():
    global _BROWSER_HELPERS
    if _BROWSER_HELPERS is None:
        spec = importlib.util.spec_from_file_location(
            "k5_admin_browser_helpers",
            Path(__file__).with_name("test_operator_beta_browser.py"),
        )
        assert spec is not None and spec.loader is not None
        _BROWSER_HELPERS = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_BROWSER_HELPERS)
    return _BROWSER_HELPERS


def _require(value, message):
    global _ADMIN_FAILURE_CODE
    __tracebackhide__ = True
    if not value:
        _ADMIN_FAILURE_CODE = _FAILURE_MESSAGES.get(message, "admin_assertion")
        pytest.fail(message, pytrace=False)


def _route_response(response, method, route):
    return response.request.method == method and urlsplit(response.url).path == route


def _origin(page):
    address = urlsplit(page.url)
    return f"{address.scheme}://{address.netloc}"


def _api(page, token, method, route, data=None):
    """Make a bounded real same-origin request, keeping credentials out of errors."""
    __tracebackhide__ = True
    headers = {"Origin": _origin(page)}
    if token:
        headers["Authorization"] = "Bearer " + token
    response = page.request.fetch(
        _origin(page) + route,
        method=method,
        headers=headers,
        data=data,
        timeout=10_000,
    )
    body = response.body()
    _require(len(body) <= 1_048_576, "Administration response exceeded its fixture budget")
    result = None
    if body and response.headers.get("content-type", "").startswith("application/json"):
        result = json.loads(body)
    return response.status, result


def _login(page, origin, username, password):
    __tracebackhide__ = True
    page.goto(origin + "/operator/")
    page.locator("#username").fill(username)
    page.locator("#password").fill(password)
    with page.expect_response(
        lambda response: _route_response(response, "POST", "/api/v1/auth/login")
    ) as completed:
        page.locator("#sign-in-button").click()
    _require(completed.value.status == 200, "Generated user could not sign in through the UI")
    value = completed.value.json()
    token = value.get("session_token")
    _require(
        isinstance(token, str) and 32 <= len(token) <= 128,
        "User sign-in did not issue a bounded opaque session",
    )
    page.locator("#workspace").wait_for(state="visible")
    _require(page.locator("#password").input_value() == "", "Login retained a password field")
    return token


def _activate(page, origin, username, temporary_credential, password):
    __tracebackhide__ = True
    page.goto(origin + "/operator/")
    page.locator("#activate-account-button").click()
    page.locator("#activation-username").fill(username)
    page.locator("#activation-credential").fill(temporary_credential)
    page.locator("#activation-password").fill(password)
    page.locator("#activation-confirm").fill(password)
    with page.expect_response(
        lambda response: _route_response(response, "POST", "/api/v1/auth/bootstrap-password")
    ) as completed:
        page.locator("#activate-account-submit").click()
    _require(completed.value.status == 204, "User could not replace the one-time credential")
    page.locator("#sign-in-form").wait_for(state="visible")
    for identifier in ("activation-credential", "activation-password", "activation-confirm"):
        _require(
            page.locator("#" + identifier).input_value() == "",
            "Account activation retained a credential field",
        )
    return _login(page, origin, username, password)


def _open_admin(page):
    __tracebackhide__ = True
    page.locator("#admin-tab").click()
    page.locator("#admin-panel").wait_for(state="visible")
    page.locator("#admin-users-tab").click()
    page.locator("#admin-users-list").wait_for(state="visible")


def _set_access(page, identifiers):
    page.locator("#admin-access-mode").select_option("selected")
    choices = page.locator("#admin-camera-choices input[type=checkbox]")
    for index in range(choices.count()):
        checkbox = choices.nth(index)
        checkbox.set_checked(checkbox.get_attribute("value") in identifiers)


def _credential(page, username):
    __tracebackhide__ = True
    page.locator("#admin-credential-dialog").wait_for(state="visible")
    _require(
        page.locator("#admin-credential-username").inner_text() == username,
        "One-time credential was shown for the wrong account",
    )
    credential = page.locator("#admin-temporary-credential").input_value()
    _require(
        isinstance(credential, str) and 32 <= len(credential) <= 128,
        "Account creation did not show a bounded one-time credential",
    )
    page.locator("#admin-close-credential").click()
    page.locator("#admin-credential-dialog").wait_for(state="hidden")
    _require(
        page.locator("#admin-temporary-credential").input_value() == "",
        "Closing the one-time credential left it in the interface",
    )
    return credential


def _create_user(page, username, role, identifiers):
    __tracebackhide__ = True
    _open_admin(page)
    page.locator("#admin-create-user").click()
    page.locator("#admin-user-dialog").wait_for(state="visible")
    page.locator("#admin-username").fill(username)
    page.locator("#admin-display-name").fill("Generated " + role)
    page.locator("#admin-role").select_option(role)
    page.locator("#admin-enabled").set_checked(True)
    _set_access(page, identifiers)
    with page.expect_response(
        lambda response: _route_response(response, "POST", "/api/v1/operator/admin/users")
    ) as created:
        page.locator("#admin-save-user").click()
    _require(created.value.status == 201, "Administrator could not create a user through the UI")
    result = created.value.json()
    account = result.get("account", {})
    identifier = account.get("id")
    _require(
        isinstance(identifier, str)
        and str(UUID(identifier)) == identifier
        and account.get("username") == username
        and account.get("role") == role
        and account.get("enabled") is True,
        "New user did not retain its selected identity and role",
    )
    _require(
        account.get("camera_access", {}).get("mode") == "selected"
        and set(account["camera_access"]["camera_ids"]) == set(identifiers),
        "New account did not atomically receive its selected cameras",
    )
    credential = _credential(page, username)
    _require(
        credential == result.get("temporary_credential"),
        "Visible one-time credential did not match the actual account issue",
    )
    page.locator(f'#admin-users-list [data-user-id="{identifier}"]').wait_for(state="visible")
    return identifier, credential


def _edit_user(page, identifier, *, enabled=None, identifiers=None):
    __tracebackhide__ = True
    _open_admin(page)
    page.locator(
        f'#admin-users-list [data-user-id="{identifier}"] [data-user-action="edit"]'
    ).click()
    page.locator("#admin-user-dialog").wait_for(state="visible")
    if enabled is not None:
        page.locator("#admin-enabled").set_checked(enabled)
    if identifiers is not None:
        _set_access(page, identifiers)
    with page.expect_response(
        lambda response: _route_response(
            response, "PATCH", "/api/v1/operator/admin/users/" + identifier
        )
    ) as changed:
        page.locator("#admin-save-user").click()
    _require(changed.value.status == 200, "Administrator could not save the user's permissions")
    page.locator("#admin-user-dialog").wait_for(state="hidden")
    return changed.value.json()


def _new_user_page(page):
    browser = page.context.browser
    _require(browser is not None, "Admin acceptance requires an owned browser")
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    user_page = context.new_page()
    user_page.set_default_timeout(12_000)
    return context, user_page


def _return_to_cameras(page):
    if page.locator("#admin-panel").is_visible():
        page.locator("#admin-return-button").click()
    page.locator("#wall-tab").click()


def _visible_live(page, identifier, color, *, diagnostic=None):
    __tracebackhide__ = True
    helpers = _browser_helpers()
    helpers._open_camera_watch(page, identifier)
    return helpers._decoded_color(page, "live-video", color, diagnostic=diagnostic)


def run_admin_journey(
    page,
    *,
    admin_username,
    admin_password,
    camera_ids,
    colors=("red", "blue"),
    client=None,
    progress=None,
):
    """Exercise actual admin UI and concurrent limited user browsers.

    Returns public evidence and separate private restart state. The current
    administrator browser/session remains signed in throughout this journey.
    """
    __tracebackhide__ = True
    del admin_username, admin_password, client  # The existing admin session is authoritative.
    _require(len(camera_ids) >= 2, "Admin acceptance needs two generated camera identities")
    origin = _origin(page)
    helpers = _browser_helpers()
    contexts = []
    user_script_errors = []
    evidence = {"admin_user_count_created": 2}
    denied = []
    suffix = secrets.token_hex(4)
    viewer_name, operator_name = "viewer-" + suffix, "operator-" + suffix
    viewer_password, operator_password = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    private = {"users": []}

    def phase(name):
        if progress is not None:
            progress(name)

    def denial(user_page, token, method, route, expected, data=None):
        status, _ = _api(user_page, token, method, route, data)
        _require(status == expected, "A limited account received an unauthorized operation")
        denied.append(status)

    try:
        phase("admin_create_accounts")
        viewer_id, viewer_temporary = _create_user(page, viewer_name, "viewer", camera_ids[:1])
        operator_id, operator_temporary = _create_user(
            page, operator_name, "operator", camera_ids[:2]
        )
        evidence["admin_user_creation"] = True

        phase("admin_activate_viewer")
        viewer_context, viewer = _new_user_page(page)
        contexts.append(viewer_context)
        viewer.on(
            "pageerror", lambda error: user_script_errors.append(helpers._script_error_code(error))
        )
        viewer_token = _activate(viewer, origin, viewer_name, viewer_temporary, viewer_password)
        viewer_temporary = ""
        _require(viewer.locator("#admin-tab").is_hidden(), "Viewer received administration UI")
        _require(viewer.locator("#add-camera-button").is_hidden(), "Viewer received camera setup")
        status, cameras = _api(viewer, viewer_token, "GET", "/api/v1/operator/cameras")
        _require(
            status == 200
            and {camera["device_id"] for camera in cameras["cameras"]} == {camera_ids[0]},
            "Viewer inventory exposed a camera outside its assigned set",
        )
        evidence["admin_viewer_live_decoded_frames"] = _visible_live(
            viewer, camera_ids[0], colors[0]
        )
        status, history = _api(
            viewer, viewer_token, "GET", f"/api/v1/operator/cameras/{camera_ids[0]}/history"
        )
        _require(
            status == 200 and isinstance(history.get("spans"), list),
            "Viewer could not read assigned-camera recording history",
        )
        status, capabilities = _api(viewer, viewer_token, "GET", "/api/v1/operator/capabilities")
        _require(
            status == 200
            and capabilities.get("can_export") is False
            and capabilities.get("can_view_history") is True
            and capabilities.get("can_manage_users") is False
            and capabilities.get("can_manage_cameras") is False,
            "Viewer capabilities did not match its permitted role",
        )
        _require(
            viewer.locator("#toggle-recording").is_hidden(),
            "Viewer received recording administration controls",
        )
        for method, route, expected, data in (
            ("GET", "/api/v1/operator/admin/users", 403, None),
            ("GET", "/api/v1/users", 403, None),
            ("GET", "/api/v1/devices", 404, None),
            ("GET", f"/api/v1/devices/{camera_ids[1]}", 404, None),
            ("GET", f"/api/v1/operator/cameras/{camera_ids[1]}/history", 404, None),
            ("POST", f"/api/v1/operator/cameras/{camera_ids[1]}/media-session", 404, None),
            (
                "PUT",
                f"/api/v1/operator/cameras/{camera_ids[0]}/recording",
                403,
                {"enabled": False},
            ),
            ("DELETE", f"/api/v1/operator/cameras/{camera_ids[0]}", 403, None),
        ):
            denial(viewer, viewer_token, method, route, expected, data)
        status, grant = _api(
            viewer, viewer_token, "POST", f"/api/v1/operator/cameras/{camera_ids[0]}/media-session"
        )
        _require(status == 200, "Viewer could not obtain assigned-camera media")
        live_url, playback_url = grant["live_url"], grant["playback_url"]
        _require(
            live_url.startswith("/media/live/") and playback_url.startswith("/media/playback?"),
            "Camera media left the authorized same-origin gateway",
        )
        start = (datetime.now(UTC) - timedelta(seconds=5)).isoformat().replace("+00:00", "Z")
        export_url = (
            playback_url + "&" + urlencode({"start": start, "duration": 1, "format": "mp4"})
        )
        denial(viewer, "", "GET", export_url, 403)
        evidence["role_permissions"] = True

        phase("admin_activate_operator")
        operator_context, operator = _new_user_page(page)
        contexts.append(operator_context)
        operator.on(
            "pageerror", lambda error: user_script_errors.append(helpers._script_error_code(error))
        )
        operator_token = _activate(
            operator, origin, operator_name, operator_temporary, operator_password
        )
        operator_temporary = ""
        _require(operator.locator("#admin-tab").is_hidden(), "Operator received administration UI")
        evidence["admin_operator_live_decoded_frames"] = _visible_live(
            operator, camera_ids[1], colors[1]
        )
        denial(operator, operator_token, "GET", "/api/v1/operator/admin/users", 403)
        status, capabilities = _api(
            operator, operator_token, "GET", "/api/v1/operator/capabilities"
        )
        _require(
            status == 200 and capabilities.get("can_export") is True,
            "Operator did not receive its distinct recording-export capability",
        )
        evidence["admin_bootstrap_password_change"] = True

        phase("admin_revoke_camera_access")
        _edit_user(page, viewer_id, identifiers=[])
        denial(viewer, "", "GET", live_url, 401)
        denial(viewer, viewer_token, "GET", "/api/v1/operator/cameras", 401)
        viewer.locator("#sign-in").wait_for(state="visible", timeout=15_000)
        _require(
            viewer.locator("#wall-panel video").count() == 0,
            "Revoking camera access left active wall players in the user interface",
        )
        evidence["admin_live_media_revocation"] = True
        viewer_token = _login(viewer, origin, viewer_name, viewer_password)
        status, inventory = _api(viewer, viewer_token, "GET", "/api/v1/operator/cameras")
        _require(
            status == 200 and inventory.get("cameras") == [],
            "A revoked camera grant reappeared after a fresh user login",
        )
        _edit_user(page, viewer_id, identifiers=camera_ids[:1])
        evidence["camera_access_controls"] = True

        phase("admin_disable_and_reenable")
        viewer_token = _login(viewer, origin, viewer_name, viewer_password)
        _edit_user(page, viewer_id, enabled=False)
        denial(viewer, viewer_token, "GET", "/api/v1/operator/cameras", 401)
        denial(
            viewer,
            "",
            "POST",
            "/api/v1/auth/login",
            401,
            {"username": viewer_name, "password": viewer_password},
        )
        _edit_user(page, viewer_id, enabled=True)
        viewer_token = _login(viewer, origin, viewer_name, viewer_password)
        evidence["admin_disable_reenable"] = True

        phase("admin_reset_password")
        _open_admin(page)
        page.locator(
            f'#admin-users-list [data-user-id="{viewer_id}"] [data-user-action="reset"]'
        ).click()
        page.locator("#admin-reset-dialog").wait_for(state="visible")
        with page.expect_response(
            lambda response: _route_response(
                response, "POST", f"/api/v1/operator/admin/users/{viewer_id}/reset-password"
            )
        ) as reset:
            page.locator("#admin-confirm-reset").click()
        _require(reset.value.status == 200, "Administrator could not reset a user's password")
        replacement = _credential(page, viewer_name)
        denial(viewer, viewer_token, "GET", "/api/v1/operator/cameras", 401)
        denial(
            viewer,
            "",
            "POST",
            "/api/v1/auth/login",
            401,
            {"username": viewer_name, "password": viewer_password},
        )
        viewer_password = secrets.token_urlsafe(24)
        viewer_token = _activate(viewer, origin, viewer_name, replacement, viewer_password)
        replacement = ""
        evidence["admin_password_reset"] = True

        phase("admin_visible_audit")
        page.locator("#admin-audit-tab").click()
        page.locator("#admin-audit-panel").wait_for(state="visible")
        page.locator("#admin-audit-list").wait_for(state="visible")
        audit = page.locator("#admin-audit-list").inner_text()
        _require(
            viewer_name in audit and operator_name in audit,
            "Visible administration audit omitted the generated account changes",
        )
        _require(
            viewer_password not in audit and operator_password not in audit,
            "Administration audit exposed a password",
        )
        rows = page.locator("#admin-audit-list [data-event-id]")
        evidence["admin_audit_event_count"] = rows.count()
        _require(evidence["admin_audit_event_count"] >= 3, "Administration audit was empty")
        evidence["admin_audit_visible"] = True
        evidence["admin_user_management"] = True
        evidence["admin_denied_http_statuses"] = denied
        _require(6 <= len(denied) <= 32, "Permission evidence left its bounded request count")
        private["users"] = [
            {
                "id": viewer_id,
                "username": viewer_name,
                "password": viewer_password,
                "role": "viewer",
                "enabled": True,
                "camera_access": {"mode": "selected", "camera_ids": list(camera_ids[:1])},
            },
            {
                "id": operator_id,
                "username": operator_name,
                "password": operator_password,
                "role": "operator",
                "enabled": True,
                "camera_access": {"mode": "selected", "camera_ids": list(camera_ids[:2])},
            },
        ]
        helpers._assert_no_browser_storage(page)
        helpers._assert_no_browser_storage(viewer)
        helpers._assert_no_browser_storage(operator)
        _require(not user_script_errors, "A limited-account browser raised a script error")
        return evidence, private
    finally:
        for context in reversed(contexts):
            context.close()
        _return_to_cameras(page)


def assert_admin_restart(page, *, private_state, camera_ids, client=None):
    """Verify durable users/grants and a real limited-user login after app restart."""
    __tracebackhide__ = True
    del client
    users = private_state["users"]
    _ADMIN_RESTART_DIAGNOSTIC.clear()
    _ADMIN_RESTART_DIAGNOSTIC["phase"] = "admin_user_listing"
    _open_admin(page)
    for index, user in enumerate(users):
        _ADMIN_RESTART_DIAGNOSTIC.update(phase="account_readback", account_index=index)
        row = page.locator(f'#admin-users-list [data-user-id="{user["id"]}"]')
        row.wait_for(state="visible")
        _require(user["username"] in row.inner_text(), "Restart changed a durable user identity")
        row.locator('[data-user-action="edit"]').click()
        page.locator("#admin-user-dialog").wait_for(state="visible")
        role = page.locator("#admin-role").input_value()
        access_mode = page.locator("#admin-access-mode").input_value()
        _ADMIN_RESTART_DIAGNOSTIC.update(
            role=role if role in {"administrator", "operator", "viewer"} else "invalid",
            access_mode=access_mode if access_mode in {"all", "selected"} else "invalid",
            enabled=page.locator("#admin-enabled").is_checked(),
            available_camera_count=page.locator(
                "#admin-camera-choices input[type=checkbox]"
            ).count(),
            expected_camera_count=len(user["camera_access"]["camera_ids"]),
        )
        _require(
            page.locator("#admin-role").input_value() == user["role"]
            and page.locator("#admin-enabled").is_checked() is user["enabled"]
            and page.locator("#admin-access-mode").input_value() == "selected",
            "Restart changed the user's durable role, enabled state or access mode",
        )
        selected = page.locator("#admin-camera-choices input[type=checkbox]:checked").evaluate_all(
            "items => items.map(item => item.value)"
        )
        _ADMIN_RESTART_DIAGNOSTIC["selected_camera_count"] = len(selected)
        _require(
            set(selected) == set(user["camera_access"]["camera_ids"]),
            "Restart changed the user's selected camera grants",
        )
        page.locator("#admin-cancel-user").click()
        page.locator("#admin-user-dialog").wait_for(state="hidden")
    context, viewer = _new_user_page(page)
    script_errors = []
    viewer.on(
        "pageerror",
        lambda error: script_errors.append(_browser_helpers()._script_error_code(error)),
    )
    try:
        _ADMIN_RESTART_DIAGNOSTIC["phase"] = "viewer_login"
        user = users[0]
        token = _login(viewer, _origin(page), user["username"], user["password"])
        status, inventory = _api(viewer, token, "GET", "/api/v1/operator/cameras")
        _ADMIN_RESTART_DIAGNOSTIC.update(
            phase="viewer_inventory",
            inventory_http_status=status,
            inventory_camera_count=len(inventory.get("cameras", []))
            if isinstance(inventory, dict)
            else 0,
        )
        _require(
            status == 200
            and {camera["device_id"] for camera in inventory["cameras"]}
            == set(user["camera_access"]["camera_ids"]),
            "Restarted viewer inventory exceeded its persisted grants",
        )
        _ADMIN_RESTART_DIAGNOSTIC["phase"] = "viewer_video"
        frames = _visible_live(viewer, camera_ids[0], "red", diagnostic=_ADMIN_RESTART_DIAGNOSTIC)
        _ADMIN_RESTART_DIAGNOSTIC["decoded_frames"] = frames
        status, _ = _api(viewer, token, "GET", f"/api/v1/operator/cameras/{camera_ids[1]}/history")
        _require(status == 404, "Restart granted the viewer an unassigned camera")
        _require(not script_errors, "Restarted limited-account browser raised a script error")
        return {"admin_state_persisted": True, "admin_restart_viewer_live_decoded_frames": frames}
    finally:
        context.close()
        _return_to_cameras(page)


def test_real_browser_admin_users_roles_camera_access_and_restart(
    tmp_path, monkeypatch, record_testsuite_property
):
    __tracebackhide__ = True
    browser_api = pytest.importorskip("playwright.sync_api")
    _require(version("playwright") == _PLAYWRIGHT_VERSION, "Unqualified browser driver")
    channel = os.getenv("K5_OPERATOR_BROWSER_CHANNEL", "msedge" if os.name == "nt" else "chromium")
    _require(channel in {"chromium", "msedge"}, "Unqualified administration browser channel")
    if os.name == "nt":
        _require(channel == "msedge", "Native Windows acceptance requires installed Edge")
    browser_helpers = _browser_helpers()
    runtime = browser_helpers._runtime_helpers()
    helpers = runtime._helpers()
    media_input, caddy_input, ffmpeg, ffprobe = (helpers._tool(name) for name in _TOOLS)
    portable_adjustment = False
    if os.name == "posix":
        original_lstat = Path.lstat

        def ordinary_system_ancestor(path, *args, **kwargs):
            nonlocal portable_adjustment
            info = original_lstat(path, *args, **kwargs)
            if path in (Path("/"), Path("/tmp")) and info.st_uid == 65534:
                portable_adjustment = True
                values = list(info)
                values[4] = 0
                return os.stat_result(values)
            return info

        monkeypatch.setattr(Path, "lstat", ordinary_system_ancestor)
    private = tmp_path / "private generated admin browser"
    private.mkdir(mode=0o700)
    username, password = "fixture-admin", secrets.token_urlsafe(32)
    identity = identity_state.initialize_identity_state(
        private / "identity", username=username, password=password
    )
    media = private / ("mediamtx.exe" if os.name == "nt" else "mediamtx")
    caddy = private / ("caddy.exe" if os.name == "nt" else "caddy")
    for supplied, admitted in ((media_input, media), (caddy_input, caddy)):
        shutil.copyfile(supplied, admitted)
        if os.name == "posix":
            admitted.chmod(0o700)
    videos = []
    for color in ("red", "blue"):
        video = private / ("generated-" + color + ".mp4")
        result = subprocess.run(
            [
                str(ffmpeg),
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"color=c={color}:s=320x240:r=15",
                "-vf",
                "noise=alls=8:allf=t+u",
                "-t",
                "8",
                "-an",
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-tune",
                "zerolatency",
                "-pix_fmt",
                "yuv420p",
                "-g",
                "15",
                "-keyint_min",
                "15",
                "-bf",
                "0",
                str(video),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=20,
            check=False,
        )
        _require(result.returncode == 0, "Generated administration video fixture failed")
        _require(helpers._decoded_frames(ffprobe, video) == 120, "Generated video was invalid")
        videos.append(video)
    reservations, ports = helpers._reserve_ports(7)
    upstream_port, product_ports = ports[0], ports[1:]
    processes = []
    product = playwright = browser = context = None
    stage, failure = "fixture_ready", None
    script_errors, cleanup_failures = [], []
    receipt = {
        "schema_version": "1",
        "platform": platform.system(),
        "native_windows_execution": os.name == "nt",
        "browser_channel": channel,
        "playwright_version": _PLAYWRIGHT_VERSION,
        "generated_media_only": True,
        "real_camera_acceptance": False,
        "hardware_capacity_acceptance": False,
        "long_duration_acceptance": False,
        "physical_camera_acceptance": False,
        "capacity_acceptance": False,
        "endurance_acceptance": False,
        "host_clock_step_acceptance": False,
        "user_terminal_required": False,
        "portable_ancestor_adjustment": portable_adjustment,
        "camera_count": 2,
    }
    started = time.monotonic()
    try:
        for listener in reservations:
            listener.close()
        stage = "generated_sources_starting"
        config = private / "private generated admin sources.json"
        helpers._private_write(
            config,
            json.dumps(
                {
                    "logLevel": "warn",
                    "rtspAddress": f"127.0.0.1:{upstream_port}",
                    "rtspTransports": ["tcp"],
                    "authInternalUsers": [
                        {
                            "user": "any",
                            "ips": ["127.0.0.0/8", "::1"],
                            "permissions": [
                                {"action": action, "path": color}
                                for action in ("publish", "read")
                                for color in ("red", "blue")
                            ],
                        }
                    ],
                    "api": False,
                    "playback": False,
                    "hls": False,
                    "webrtc": False,
                    "rtmp": False,
                    "srt": False,
                    "moq": False,
                    "metrics": False,
                    "pprof": False,
                    "paths": {color: {"source": "publisher"} for color in ("red", "blue")},
                }
            ),
        )
        processes.append(helpers._start([str(media), str(config)], private))

        def upstream_ready():
            try:
                with socket.create_connection(("127.0.0.1", upstream_port), timeout=0.5):
                    return True
            except OSError:
                return False

        runtime._wait(upstream_ready, label="Generated admin sources", timeout=10)
        for color, video in zip(("red", "blue"), videos, strict=True):
            processes.append(
                browser_helpers._publish_generated(
                    helpers, ffmpeg, video, f"rtsp://127.0.0.1:{upstream_port}/{color}", private
                )
            )
        stage = "product_starting"
        product = runtime._Product(
            identity, identity.directory / "recordings", media, caddy, product_ports
        )
        client = runtime._Client(product_ports[0])
        runtime._wait(
            lambda: product.server.started and client.get("/operator/")[0] == 200,
            label="Admin product startup",
            product=product,
        )
        playwright = browser_api.sync_playwright().start()
        browser = playwright.chromium.launch(channel=channel, headless=True)
        receipt["browser_version"] = browser.version
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()
        page.set_default_timeout(12_000)
        page.on(
            "pageerror",
            lambda error: script_errors.append(browser_helpers._script_error_code(error)),
        )
        client.token = _login(page, client.origin, username, password)
        camera_ids = []
        stage = "generated_camera_enrollment"
        for color in ("red", "blue"):
            camera = browser_helpers._rtsp_enroll(
                page,
                name="Generated " + color + " admin camera",
                source_uri=f"rtsp://127.0.0.1:{upstream_port}/{color}",
                username="",
                password="",
            )
            camera_ids.append(camera["device_id"])

        def progress(value):
            nonlocal stage
            stage = value

        evidence, private_state = run_admin_journey(
            page,
            admin_username=username,
            admin_password=password,
            camera_ids=camera_ids,
            client=client,
            progress=progress,
        )
        receipt.update(evidence)
        preview = os.getenv("K5_ADMIN_PREVIEW_PATH")
        if preview:
            _open_admin(page)
            page.screenshot(path=preview, full_page=True)
            _return_to_cameras(page)
        # The standalone fixture owns an in-process ASGI server. Close its
        # browser clients before replacing it, then prove persistence with a
        # fresh browser session. The installed-Windows journey separately
        # exercises native shutdown and owned-process cleanup.
        stage = "admin_browser_closing_for_restart"
        context.close()
        context = None
        stage = "admin_product_restart"
        product.stop()
        stage = "admin_product_recreating"
        product = runtime._Product(
            identity, identity.directory / "recordings", media, caddy, product_ports
        )
        stage = "admin_product_startup"
        runtime._wait(
            lambda: product.server.started and client.get("/operator/")[0] == 200,
            label="Admin product restart",
            product=product,
        )
        stage = "admin_browser_recreating"
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()
        page.set_default_timeout(12_000)
        page.on(
            "pageerror",
            lambda error: script_errors.append(browser_helpers._script_error_code(error)),
        )
        stage = "admin_restarted_login"
        client.token = _login(page, client.origin, username, password)
        stage = "admin_restarted_permissions"
        receipt.update(
            assert_admin_restart(page, private_state=private_state, camera_ids=camera_ids)
        )
        _require(not script_errors, "Administration UI raised a script error")
        receipt["browser_script_errors"] = 0
    except BaseException as error:
        failure = browser_helpers._failure_code(error)
    finally:
        for label, resource in (("context", context), ("browser", browser)):
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    cleanup_failures.append(label)
        if playwright is not None:
            try:
                playwright.stop()
            except Exception:
                cleanup_failures.append("browser_driver")
        if product is not None:
            try:
                product.stop()
            except Exception:
                cleanup_failures.append("product")
        for process in reversed(processes):
            try:
                helpers._stop_owned(process)
            except Exception:
                cleanup_failures.append("generated_process")
        for listener in reservations:
            listener.close()
        diagnostic = {
            "stage": stage,
            "failure_code": failure,
            "assertion_code": _ADMIN_FAILURE_CODE,
            "restart": dict(_ADMIN_RESTART_DIAGNOSTIC),
            "script_error_codes": script_errors[:16],
            "cleanup_failure_codes": cleanup_failures,
        }
        record_testsuite_property("operator_admin_browser_diagnostic", json.dumps(diagnostic))
        if failure is not None or cleanup_failures:
            print("Administration browser qualification diagnostic: " + json.dumps(diagnostic))
    _require(failure is None, f"Administration browser failed at {stage}: {failure}")
    _require(not cleanup_failures, "Administration fixture cleanup failed")
    elapsed = time.monotonic() - started
    _require(elapsed < 180, "Administration browser qualification exceeded three minutes")
    receipt.update(duration_seconds=round(elapsed, 2), owned_cleanup=True)
    record_testsuite_property("operator_admin_browser_receipt", json.dumps(receipt, sort_keys=True))
