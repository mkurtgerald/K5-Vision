"""Actual Chromium interactions for device-capability-driven camera encoder editing."""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import pytest

_ASSETS = Path(__file__).resolve().parents[2] / "src/k5vision/operator_web"
_ADMIN = "a22c22bc-4089-45b7-a370-f360bb9d33c0"
_CAMERA = "e4fd401d-ae8e-4ac2-9f92-e8287f241532"
_OTHER = "edbc1678-bf89-4936-bbc5-15aff54ffb83"
_REVISION = "generated-private-source-revision-0001"
_HANDLE = "generated-one-time-settings-handle-012345678901"


def _inspection(camera_id=_CAMERA):
    return {
        "camera_id": camera_id,
        "status": "available",
        "protocol": "media1",
        "source_revision": _REVISION,
        "current": {
            "codec": "H264",
            "frame_rate": 25,
            "resolution": {"width": 1280, "height": 720},
            "bitrate_kbps": 2048,
            "gop": 25,
            "quality": 3.5,
            "encoding_interval": 1,
        },
        "options": {
            "codecs": [
                {"codec": "H264", "device_supported": True, "product_supported": True},
                {"codec": "H265", "device_supported": True, "product_supported": False},
            ],
            "resolutions": [{"width": 640, "height": 360}, {"width": 1280, "height": 720}],
            "frame_rate": {"min": 5, "max": 30, "values": [], "integer_only": True},
            "bitrate_kbps": {"min": 128, "max": 4096},
            "gop": {"min": 1, "max": 60},
            "quality": {"min": 1, "max": 5},
            "encoding_interval": {"min": 1, "max": 4},
            "writable_fields": [
                "codec",
                "frame_rate",
                "resolution",
                "bitrate_kbps",
                "gop",
                "quality",
                "encoding_interval",
            ],
        },
        "shared_configuration": False,
        "shared_profile_count": 1,
        "writable": True,
        "writable_reason": None,
        "configuration_readback_verified": False,
        "stream_verified": False,
        "observed_fps": None,
        "settings_handle": _HANDLE,
        "expires_in_seconds": 120,
    }


@pytest.fixture
def workspace():
    if os.getenv("K5_OPERATOR_BROWSER_ACCEPTANCE") != "1":
        pytest.skip("Camera encoder UI proof requires the explicit browser environment")
    browser_api = pytest.importorskip("playwright.sync_api")

    @contextmanager
    def launch(*, role="administrator", permissions=None, inspection=None):
        from k5vision.operator_role_policy import BASE_PERMISSIONS

        account = {
            "id": _ADMIN,
            "username": "fixture-admin",
            "display_name": "Generated admin",
            "role": role,
            "enabled": True,
        }
        grants = list(BASE_PERMISSIONS[role]) if permissions is None else list(permissions)
        cameras = [
            {
                "device_id": value,
                "name": "Generated camera " + str(index + 1),
                "host": "127.0.0.1",
                "status": "disconnected",
                "recording": False,
                "recording_enabled": True,
                "recording_state": "waiting",
                "analytics_status": "disabled",
                "source_revision": _REVISION,
                "connection_type": "onvif",
            }
            for index, value in enumerate((_CAMERA, _OTHER))
        ]
        state = {
            "account": account,
            "permissions": grants,
            "cameras": cameras,
            "calls": [],
            "inspection": inspection or _inspection(),
            "hold_read": False,
            "held": [],
            "read_error": None,
            "apply_error": None,
            "reject_session": False,
            "expires": 120,
            "held_inventory": [],
        }
        with browser_api.sync_playwright() as api:
            browser = api.chromium.launch(
                channel=os.getenv("K5_OPERATOR_BROWSER_CHANNEL", "chromium"), headless=True
            )
            context = browser.new_context(viewport={"width": 1440, "height": 1100})
            context.add_init_script("window.Hls={isSupported:()=>false}")
            page = context.new_page()
            page.set_default_timeout(8000)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))

            def respond(route, value=None, status=200):
                route.fulfill(
                    status=status,
                    content_type="application/json",
                    body="" if status == 204 else json.dumps(value or {}),
                )

            def intercept(route):
                path = urlsplit(route.request.url).path
                method = route.request.method
                if path.startswith("/operator/"):
                    name = path.rsplit("/", 1)[-1] or "index.html"
                    asset = _ASSETS / name
                    if (
                        not asset.is_file()
                        or asset.name != name
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
                body = route.request.post_data_json if route.request.post_data else None
                state["calls"].append((method, path, body))
                if path == "/api/v1/auth/oidc":
                    return respond(route, {"available": False})
                if path == "/api/v1/auth/login":
                    return respond(
                        route,
                        {
                            "session_token": "generated-ui-session-token-01234567890123456789",
                            "account": account,
                            "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
                            "expires_in_seconds": 600,
                        },
                    )
                if path == "/api/v1/auth/me":
                    return respond(route, account)
                if path == "/api/v1/auth/logout":
                    return respond(route, {}, 204)
                if state["reject_session"]:
                    return respond(route, {"detail": "Unauthorized"}, 401)
                if path == "/api/v1/operator/status":
                    return respond(
                        route,
                        {"server_utc": datetime.now(UTC).isoformat(), "worker_status": "ready"},
                    )
                if path == "/api/v1/operator/cameras":
                    if state.get("inventory_error"):
                        return respond(route, {"detail": "unavailable"}, state["inventory_error"])
                    value = {
                        "cameras": cameras,
                        "worker_status": "ready",
                        "capabilities": {
                            "account": {
                                **account,
                                "role_profile_id": None,
                                "effective_permissions": grants,
                            },
                            "effective_permissions": grants,
                            "can_export": "cameras.export" in grants,
                            "can_manage_users": "users.manage" in grants,
                            "can_manage_cameras": "cameras.manage" in grants,
                            "can_view_history": "cameras.playback" in grants,
                        },
                    }
                    if state.get("hold_inventory"):
                        state.setdefault("held_inventory", []).append(
                            (route, json.loads(json.dumps(value)))
                        )
                        return None
                    return respond(route, value)
                if path in {
                    "/api/v1/operator/camera-stream-probe",
                    "/api/v1/operator/camera-profiles",
                } and state.get("reconnect_kind"):
                    return respond(
                        route,
                        {
                            "probe_id": "generated-reconnect-probe-0123456789012345",
                            "expires_in_seconds": 120,
                            "profiles": [
                                {
                                    "id": "generated-new-profile",
                                    "name": "Generated new profile",
                                    "codec": "H264",
                                    "width": 640,
                                    "height": 360,
                                    "supported": True,
                                }
                            ],
                        },
                    )
                if path in {
                    "/api/v1/operator/cameras/stream-connect",
                    "/api/v1/operator/cameras/connect",
                } and state.get("reconnect_kind"):
                    updated = {
                        **cameras[0],
                        "source_revision": "generated-reconnected-source-revision-0002",
                        "connection_type": state["reconnect_kind"],
                    }
                    cameras[0] = updated
                    value = {**_inspection(), "source_revision": updated["source_revision"]}
                    if state["reconnect_kind"] == "rtsp":
                        value.update(
                            status="unavailable",
                            current=None,
                            options=None,
                            writable=False,
                            writable_reason="video_unsupported",
                            shared_profile_count=0,
                        )
                        value.pop("settings_handle")
                        value.pop("expires_in_seconds")
                    state["inspection"] = value
                    if state.get("invalid_connect_ack"):
                        return respond(route, {**updated, "device_id": _OTHER}, 201)
                    return respond(route, updated, 201)
                if path.endswith("/media-session"):
                    return respond(
                        route,
                        {
                            "live_url": "/media/live/generated/index.m3u8",
                            "playback_url": "/media/playback?path=generated",
                        },
                    )
                if path.endswith("/video-settings") or path.endswith("/video-settings/read"):
                    if method == "PATCH":
                        if state["apply_error"]:
                            code, status = state["apply_error"]
                            return respond(route, {"detail": code}, status)
                        value = json.loads(json.dumps(state["inspection"]))
                        value["camera_id"] = path.split("/")[5]
                        if state.get("apply_source_revision"):
                            value["source_revision"] = state["apply_source_revision"]
                            next(c for c in cameras if c["device_id"] == value["camera_id"])[
                                "source_revision"
                            ] = value["source_revision"]
                        value["current"].update(
                            {
                                k: v
                                for k, v in body.items()
                                if k not in {"settings_handle", "shared_config_ack"}
                            }
                        )
                        value.update(
                            configuration_readback_verified=True,
                            stream_verified=False,
                            requested={
                                k: v
                                for k, v in body.items()
                                if k not in {"settings_handle", "shared_config_ack"}
                            },
                        )
                        value.pop("settings_handle", None)
                        value.pop("expires_in_seconds", None)
                        state["inspection"] = {
                            **value,
                            "settings_handle": _HANDLE,
                            "expires_in_seconds": state["expires"],
                        }
                        if state.get("hold_apply"):
                            state["held_apply"] = (route, value)
                            return None
                        return respond(route, value)
                    if state["read_error"]:
                        code, status = state["read_error"]
                        return respond(route, {"detail": code}, status)
                    value = json.loads(json.dumps(state["inspection"]))
                    value["camera_id"] = state.get("response_camera_id") or path.split("/")[5]
                    if "settings_handle" in value:
                        value["expires_in_seconds"] = state["expires"]
                    if state["hold_read"]:
                        state["held"].append((route, value))
                        return None
                    return respond(route, value)
                return respond(route, {}, 404)

            context.route("**/*", intercept)
            page.goto("http://k5.test/operator/")
            page.locator("#username").fill(account["username"])
            page.locator("#password").fill("generated-fixture-password")
            page.locator("#sign-in-button").click()
            page.locator("#security-tab").wait_for(state="visible")
            yield page, state
            context.close()
            browser.close()
        assert not errors, errors

    return launch


def _open(page, camera_id=_CAMERA, *, manager_only=False):
    page.locator(".camera-item").filter(
        has_text="Generated camera " + ("1" if camera_id == _CAMERA else "2")
    ).click()
    if not manager_only:
        page.locator("#camera-video-button").click()
    page.locator("#camera-video-dialog").wait_for(state="visible")
    page.wait_for_function(
        "document.querySelector('#camera-video-form').getAttribute('aria-busy') === 'false'"
    )


def _patches(state):
    return [
        body
        for method, path, body in state["calls"]
        if method == "PATCH" and path.endswith("/video-settings")
    ]


def test_supported_parameters_apply_once_and_truthful_readback(workspace):
    with workspace() as (page, state):
        _open(page)
        assert page.locator("#video-codec option[value=H265]").is_disabled()
        page.locator("#video-frame-rate").fill("15")
        page.locator("#video-resolution").select_option("640x360")
        page.locator("#video-bitrate-kbps").fill("1024")
        page.locator("#video-gop").fill("30")
        page.locator("#video-quality").fill("4")
        page.locator("#camera-video-apply").click()
        page.get_by_text(
            "Camera configuration readback verified. Read fresh settings before another change.",
            exact=True,
        ).wait_for()
        assert _patches(state) == [
            {
                "settings_handle": _HANDLE,
                "resolution": {"width": 640, "height": 360},
                "frame_rate": 15,
                "bitrate_kbps": 1024,
                "gop": 30,
                "quality": 4,
                "shared_config_ack": False,
            }
        ]
        assert (
            "Live stream is not verified" in page.locator("#camera-video-verification").inner_text()
        )
        assert (
            "Observed FPS: not measured" in page.locator("#camera-video-verification").inner_text()
        )
        assert page.locator("#camera-video-apply").is_disabled()
        page.locator("#camera-video-refresh").click()
        page.wait_for_function("!document.querySelector('#video-frame-rate').disabled")
        assert page.locator("#video-frame-rate").input_value() == "15"


def test_delayed_metadata_disables_edits_and_late_close_cannot_restore_handle(workspace):
    with workspace() as (page, state):
        state["hold_read"] = True
        page.locator(".camera-item").first.click()
        page.locator("#camera-video-button").click()
        assert page.locator("#video-frame-rate").is_disabled()
        assert page.locator("#camera-video-apply").is_disabled()
        page.locator("#camera-video-cancel").click()
        assert page.locator("#camera-video-dialog").is_hidden()
        for route, value in state["held"]:
            try:
                route.fulfill(content_type="application/json", body=json.dumps(value))
            except Exception:
                pass
        state["hold_read"] = False
        _open(page, _OTHER)
        assert "Generated camera 2" in page.locator("#camera-video-target").inner_text()
        page.locator("#video-frame-rate").fill("10")
        page.locator("#camera-video-apply").click()
        page.get_by_text(
            "Camera configuration readback verified. Read fresh settings before another change.",
            exact=True,
        ).wait_for()
        assert [p for m, p, _ in state["calls"] if m == "PATCH"] == [
            f"/api/v1/operator/cameras/{_OTHER}/video-settings"
        ]


def test_one_shot_management_account_and_camera_auth_failure_keeps_session(workspace):
    with workspace() as (page, state):
        state["read_error"] = ("video_auth_failed", 422)
        _open(page)
        assert (
            "management account was not accepted"
            in page.locator("#camera-video-message").inner_text()
        )
        assert page.locator("#workspace").is_visible()
        state["read_error"] = None
        page.locator("#camera-video-use-account").check()
        page.locator("#camera-video-username").fill("generated-manager")
        page.locator("#camera-video-password").fill("generated-private-password")
        page.locator("#camera-video-read").click()
        assert page.locator("#camera-video-password").input_value() == ""
        assert page.locator("#camera-video-username").input_value() == ""
        page.wait_for_function("!document.querySelector('#video-frame-rate').disabled")
        assert [
            b for m, p, b in state["calls"] if m == "POST" and p.endswith("/video-settings/read")
        ] == [{"username": "generated-manager", "password": "generated-private-password"}]
        assert page.evaluate("localStorage.length+sessionStorage.length") == 0


def test_shared_ack_optional_readonly_values_and_invalid_fps_do_not_send(workspace):
    value = _inspection()
    value.update(shared_configuration=True, shared_profile_count=2)
    value["options"]["bitrate_kbps"] = None
    with workspace(inspection=value) as (page, state):
        _open(page)
        assert page.locator("#video-bitrate-kbps").is_disabled()
        assert "2 camera profiles" in page.locator("#camera-video-shared-notice").inner_text()
        page.locator("#video-frame-rate").fill("40")
        page.locator("#video-shared-ack").check()
        assert page.locator("#camera-video-apply").is_disabled()
        page.locator("#video-frame-rate").fill("10")
        page.locator("#video-shared-ack").uncheck()
        assert page.locator("#camera-video-apply").is_disabled()
        page.locator("#video-shared-ack").check()
        page.locator("#camera-video-apply").click()
        page.get_by_text(
            "Camera configuration readback verified. Read fresh settings before another change.",
            exact=True,
        ).wait_for()
        assert len(_patches(state)) == 1 and _patches(state)[0]["shared_config_ack"] is True


def test_discrete_fractional_fps_and_h264_restore(workspace):
    value = _inspection()
    value["protocol"] = "media2"
    value["current"]["codec"] = "H265"
    value["current"]["frame_rate"] = 7.5
    value["options"]["frame_rate"] = {
        "min": 5,
        "max": 15,
        "values": [5, 7.5, 15],
        "integer_only": False,
    }
    with workspace(inspection=value) as (page, state):
        _open(page)
        assert page.locator("#video-frame-rate").is_disabled()
        page.locator("#video-codec").select_option("H264")
        page.locator("#video-frame-rate").select_option("7.5")
        page.locator("#camera-video-apply").click()
        page.get_by_text(
            "Camera configuration readback verified. Read fresh settings before another change.",
            exact=True,
        ).wait_for()
        assert _patches(state)[0]["codec"] == "H264"
        assert "frame_rate" not in _patches(state)[0]


@pytest.mark.parametrize(
    "code,status",
    [
        ("video_readback_mismatch", 502),
        ("video_source_changed", 409),
        ("video_settings_expired", 410),
    ],
)
def test_failed_apply_never_claims_success_and_consumes_handle(workspace, code, status):
    with workspace() as (page, state):
        _open(page)
        state["apply_error"] = (code, status)
        page.locator("#video-frame-rate").fill("10")
        page.locator("#camera-video-apply").click()
        page.wait_for_function(
            "document.querySelector('#camera-video-form').getAttribute('aria-busy') === 'false'"
        )
        assert page.locator("#camera-video-apply").is_disabled()
        assert "readback verified" not in page.locator("#camera-video-message").inner_text()
        assert page.locator("#camera-video-message").inner_text()
        assert len(_patches(state)) == 1


def test_rtsp_unavailable_does_not_render_fake_parameters(workspace):
    value = _inspection()
    value.update(
        status="unavailable",
        current=None,
        options=None,
        writable=False,
        writable_reason="video_unsupported",
        shared_profile_count=0,
    )
    value.pop("settings_handle")
    value.pop("expires_in_seconds")
    with workspace(inspection=value) as (page, state):
        _open(page)
        assert page.locator("#camera-video-values").is_hidden()
        assert page.locator("#camera-video-apply").is_disabled()
        assert "ONVIF encoder controls" in page.locator("#camera-video-message").inner_text()
        assert not _patches(state)


def test_inventory_preserves_dirty_fields_but_source_change_requires_fresh_read(workspace):
    with workspace() as (page, state):
        _open(page)
        page.locator("#video-frame-rate").fill("13")
        page.wait_for_timeout(3200)
        assert page.locator("#video-frame-rate").input_value() == "13"
        assert page.locator("#camera-video-apply").is_enabled()
        changed = "generated-private-source-revision-0002"
        state["cameras"][0]["source_revision"] = changed
        page.wait_for_function("document.querySelector('#camera-video-apply').disabled")
        assert page.locator("#video-frame-rate").input_value() == "13"
        assert "connection changed" in page.locator("#camera-video-message").inner_text()
        state["inspection"]["source_revision"] = changed
        page.locator("#camera-video-refresh").click()
        page.wait_for_function("!document.querySelector('#video-frame-rate').disabled")
        assert page.locator("#video-frame-rate").input_value() == "25"


def test_expiry_and_authority_revocation_disable_or_clear_editor(workspace):
    with workspace() as (page, state):
        state["expires"] = 1
        _open(page)
        page.locator("#video-frame-rate").fill("10")
        page.wait_for_function("document.querySelector('#camera-video-apply').disabled")
        assert "expired" in page.locator("#camera-video-message").inner_text()
        state["permissions"].remove("cameras.manage")
        page.wait_for_function("!document.querySelector('#camera-video-dialog').open")
        assert page.locator("#camera-video-password").input_value() == ""
        assert not _patches(state)


def test_manage_only_account_uses_settings_without_media_admission(workspace):
    with workspace(permissions=["cameras.manage"]) as (page, state):
        _open(page, manager_only=True)
        assert page.locator("#video-frame-rate").is_enabled()
        assert not any(path.endswith("/media-session") for _, path, _ in state["calls"])


def test_viewer_cannot_open_or_synthetically_mutate_camera_video(workspace):
    with workspace(role="viewer") as (page, state):
        page.locator(".camera-item").first.click()
        assert page.locator("#camera-video-button").is_hidden()
        page.locator("#camera-video-button").evaluate(
            "e=>e.dispatchEvent(new MouseEvent('click',{bubbles:true}))"
        )
        assert page.locator("#camera-video-dialog").is_hidden()
        assert not any("/video-settings" in path for _, path, _ in state["calls"])


def test_refresh_waits_for_new_options_before_reenabling_parameters(workspace):
    with workspace() as (page, state):
        _open(page)
        page.locator("#video-frame-rate").fill("13")
        state["inspection"]["current"]["frame_rate"] = 10
        state["inspection"]["options"]["frame_rate"]["max"] = 10
        state["hold_read"] = True
        page.locator("#camera-video-refresh").click()
        assert page.locator("#camera-video-apply").is_disabled()
        assert page.locator("#video-frame-rate").is_disabled()
        assert state["held"]
        route, value = state["held"].pop()
        route.fulfill(content_type="application/json", body=json.dumps(value))
        page.wait_for_function("!document.querySelector('#video-frame-rate').disabled")
        assert page.locator("#video-frame-rate").input_value() == "10"
        assert page.locator("#video-frame-rate").get_attribute("max") == "10"
        page.locator("#video-frame-rate").fill("15")
        assert page.locator("#camera-video-apply").is_disabled()
        assert not _patches(state)


@pytest.mark.parametrize("fault", ["camera", "range", "codec"])
def test_untrusted_metadata_never_enables_a_change(workspace, fault):
    with workspace() as (page, state):
        if fault == "camera":
            state["response_camera_id"] = _OTHER
        elif fault == "range":
            state["inspection"]["options"]["frame_rate"]["min"] = 100
        else:
            state["inspection"]["options"]["codecs"][0]["product_supported"] = False
        _open(page)
        assert page.locator("#camera-video-apply").is_disabled()
        assert page.locator("#camera-video-values").is_hidden()
        assert not _patches(state)


def test_human_session_expiry_clears_modal_and_ignores_pending_work(workspace):
    with workspace() as (page, state):
        _open(page)
        page.locator("#camera-video-use-account").check()
        page.locator("#camera-video-username").fill("generated-manager")
        page.locator("#camera-video-password").fill("generated-private-password")
        state["read_error"] = ("Unauthorized", 401)
        page.locator("#camera-video-read").click()
        page.locator("#sign-in").wait_for(state="visible")
        assert page.locator("#camera-video-dialog").is_hidden()
        assert page.locator("#camera-video-password").input_value() == ""
        assert page.locator("#camera-video-username").input_value() == ""
        assert not _patches(state)


def test_own_apply_source_renewal_survives_inventory_before_result(workspace):
    with workspace() as (page, state):
        _open(page)
        state.update(
            apply_source_revision="generated-private-source-revision-0002", hold_apply=True
        )
        page.locator("#video-frame-rate").fill("15")
        page.locator("#camera-video-apply").click()
        page.wait_for_timeout(3200)
        assert page.locator("#camera-video-dialog").is_visible()
        assert page.locator("#camera-video-form").get_attribute("aria-busy") == "true"
        route, value = state["held_apply"]
        route.fulfill(content_type="application/json", body=json.dumps(value))
        page.get_by_text(
            "Camera configuration readback verified. Read fresh settings before another change.",
            exact=True,
        ).wait_for()
        assert page.locator("#video-frame-rate").input_value() == "15"
        assert page.locator("#camera-video-apply").is_disabled()
        assert len(_patches(state)) == 1


def test_unavailable_authority_pauses_changes_without_discarding_dirty_fields(workspace):
    with workspace() as (page, state):
        _open(page)
        page.locator("#video-frame-rate").fill("13")
        state["inventory_error"] = 503
        page.wait_for_function("document.querySelector('#camera-video-apply').disabled")
        assert page.locator("#camera-video-dialog").is_visible()
        assert page.locator("#video-frame-rate").input_value() == "13"
        assert (
            "permissions could not be verified"
            in page.locator("#camera-video-message").inner_text()
        )
        state["inventory_error"] = None
        page.wait_for_function("!document.querySelector('#camera-video-refresh').disabled")
        assert page.locator("#video-frame-rate").input_value() == "13"
        assert page.locator("#camera-video-apply").is_disabled()
        page.locator("#camera-video-refresh").click()
        page.wait_for_function("!document.querySelector('#video-frame-rate').disabled")
        assert page.locator("#video-frame-rate").input_value() == "25"


@pytest.mark.parametrize("kind, invalid_ack", [("rtsp", False), ("onvif", False), ("rtsp", True)])
def test_reconnect_publishes_ack_before_delayed_inventory_for_video_settings(
    workspace, kind, invalid_ack
):
    with workspace() as (page, state):
        page.locator(".camera-item").first.click()
        state.update(hold_inventory=True, reconnect_kind=kind, invalid_connect_ack=invalid_ack)
        page.locator("#refresh-cameras").click()
        deadline = time.monotonic() + 2
        while not state["held_inventory"] and time.monotonic() < deadline:
            page.wait_for_timeout(10)
        assert state["held_inventory"]
        old_route, old_inventory = state["held_inventory"][0]
        page.locator("#update-source-button").click()
        page.locator("#camera-connection-type").select_option(kind)
        if kind == "rtsp":
            page.locator("#new-camera-stream-uri").fill("rtsp://127.0.0.1/reconnected")
        else:
            page.locator("#new-camera-username").fill("generated-camera-user")
            page.locator("#new-camera-password").fill("generated-camera-password")
        page.locator("#test-camera-connection").click()
        page.locator("#save-camera").wait_for(state="visible")
        page.wait_for_function("!document.getElementById('save-camera').disabled")
        page.locator("#save-camera").click()
        if invalid_ack:
            page.wait_for_function(
                "document.getElementById('add-camera-form').getAttribute('aria-busy') === 'false'"
            )
            assert "connection was updated" in page.locator("#add-camera-message").inner_text()
            assert page.locator("#add-camera-dialog").is_visible()
            assert page.locator("#save-camera").is_disabled()
            page.locator("#add-camera-form").evaluate(
                "form=>form.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}))"
            )
            assert len([call for call in state["calls"] if call[1].endswith("stream-connect")]) == 1
            for route, inventory in state["held_inventory"]:
                if inventory["cameras"][0]["source_revision"] == _REVISION:
                    route.fulfill(content_type="application/json", body=json.dumps(inventory))
            page.wait_for_timeout(50)
            page.locator("#cancel-add-camera").click()
            assert page.locator("#camera-video-button").is_hidden()
            assert not _patches(state)
            return
        deadline = time.monotonic() + 2
        fresh = []
        while not fresh and time.monotonic() < deadline:
            fresh = [
                item
                for item in state["held_inventory"]
                if item[1]["cameras"][0]["source_revision"] != _REVISION
            ]
            if fresh:
                break
            page.wait_for_timeout(10)
        assert fresh
        _, fresh_inventory = fresh[0]
        assert old_inventory["cameras"][0]["source_revision"] == _REVISION
        assert fresh_inventory["cameras"][0]["source_revision"] != _REVISION
        page.locator("#add-camera-dialog").wait_for(state="hidden")
        page.locator("#camera-video-button").click()
        page.wait_for_function(
            "document.getElementById('camera-video-form').getAttribute('aria-busy') === 'false'"
        )
        if kind == "rtsp":
            assert "ONVIF encoder controls" in page.locator("#camera-video-message").inner_text()
            assert page.locator("#camera-video-values").is_hidden()
            assert page.locator("#camera-video-apply").is_disabled()
        else:
            assert page.locator("#video-frame-rate").is_enabled()
            assert page.locator("#video-frame-rate").input_value() == "25"
        for route, inventory in fresh:
            route.fulfill(content_type="application/json", body=json.dumps(inventory))
        for route, inventory in state["held_inventory"]:
            if inventory["cameras"][0]["source_revision"] == _REVISION:
                route.fulfill(content_type="application/json", body=json.dumps(inventory))
        page.wait_for_timeout(50)
        assert "connection changed" not in page.locator("#camera-video-message").inner_text()
        assert page.locator("#camera-video-dialog").is_visible()
        if kind == "onvif":
            assert page.locator("#video-frame-rate").is_enabled()
        assert not _patches(state)
