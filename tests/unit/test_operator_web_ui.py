"""Real-browser behavior checks for enrollment status, using intercepted APIs.

These isolate UI promises and state transitions. Actual media, ONVIF and native
Windows acceptance remain independently qualified by the integration lanes.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("K5_OPERATOR_BROWSER_ACCEPTANCE") != "1",
    reason="UI behavior proof requires the explicit browser acceptance environment",
)
_CAMERA = "e4fd401d-ae8e-4ac2-9f92-e8287f241532"
_OTHER = "2cad3cbb-c56a-4368-b3e6-e5032576b77a"


@pytest.mark.parametrize("mode", ["add", "reconnect"])
def test_profile_acceptance_and_enrollment_wait_for_actual_stream_status(mode):
    browser_api = pytest.importorskip("playwright.sync_api")
    channel = os.getenv("K5_OPERATOR_BROWSER_CHANNEL", "msedge" if os.name == "nt" else "chromium")
    assets = Path(__file__).resolve().parents[2] / "src/k5vision/operator_web"
    camera = {
        "device_id": _CAMERA,
        "name": "Generated state camera",
        "host": "127.0.0.1",
        "management_port": 80,
        "management_secure": False,
        "status": "live",
        "recording": True,
        "analytics_status": "disabled",
    }
    cameras = [camera] if mode == "reconnect" else []
    service = {"worker_status": "ready"}
    account = {"username": "fixture-admin", "role": "administrator", "display_name": "Fixture"}
    calls, script_errors = {"profiles": 0, "connect": 0}, []
    playwright = browser = context = None
    try:
        playwright = browser_api.sync_playwright().start()
        browser = playwright.chromium.launch(channel=channel, headless=True)
        context = browser.new_context()
        page = context.new_page()
        page.set_default_timeout(10_000)
        page.on("pageerror", lambda _error: script_errors.append("uncaught_script_error"))

        def intercept(route):
            path, method = urlsplit(route.request.url).path, route.request.method
            if path.startswith("/operator/"):
                name = path.rsplit("/", 1)[1] or "index.html"
                content_type = {
                    "index.html": "text/html",
                    "app.js": "application/javascript",
                    "app.css": "text/css",
                }[name]
                route.fulfill(content_type=content_type, body=(assets / name).read_text())
                return
            status, data = 200, {}
            if path == "/api/v1/auth/login":
                data = {
                    "session_token": "generated-ui-status-session-token-000000000000000000",
                    "account": account,
                    "expires_at": "2035-01-01T00:00:00Z",
                }
            elif path == "/api/v1/auth/me":
                data = account
            elif path == "/api/v1/operator/status":
                data = {**service, "storage": None, "max_recording_bytes": 20 * 1024**3}
            elif path == "/api/v1/operator/cameras":
                data = {**service, "cameras": cameras}
            elif path == "/api/v1/operator/camera-profiles":
                calls["profiles"] += 1
                data = {
                    "probe_id": "p" * 32,
                    "expires_in_seconds": 120,
                    "profiles": [
                        {
                            "id": "s" * 32,
                            "name": "Generated main stream",
                            "codec": "H264",
                            "width": 640,
                            "height": 480,
                            "supported": True,
                        }
                    ],
                }
            elif path == "/api/v1/operator/cameras/connect":
                calls["connect"] += 1
                payload = route.request.post_data_json
                assert payload["name"] == camera["name"]
                assert payload["probe_id"] == "p" * 32 and payload["profile_id"] == "s" * 32
                camera.update(status="connecting", recording=False)
                cameras[:] = [camera]
                status, data = 201, camera
            elif path.endswith("/media-session") and path != "/api/v1/operator/media-session":
                status, data = 503, {"detail": "Camera video is not available"}
            elif path == "/api/v1/operator/cameras/" + _CAMERA and method == "DELETE":
                cameras[:] = [item for item in cameras if item["device_id"] != _CAMERA]
                status = 204
            elif path in {"/api/v1/auth/logout", "/api/v1/operator/media-session"}:
                status = 204
            else:
                status = 404
            route.fulfill(
                status=status,
                content_type="application/json",
                body="" if status == 204 else json.dumps(data),
            )

        page.route("**/*", intercept)
        page.goto("http://127.0.0.1:8000/operator/")
        page.locator("#username").fill("fixture-admin")
        page.locator("#password").fill("generated-human-account-password")
        page.locator("#sign-in-button").click()
        expect = browser_api.expect
        expect(page.locator("#workspace")).to_be_visible()
        page.locator(
            "#update-source-button" if mode == "reconnect" else "#add-camera-button"
        ).click()
        if mode == "add":
            page.locator("#new-camera-name").fill(camera["name"])
            page.locator("#new-camera-host").fill(camera["host"])
        page.locator("#new-camera-username").fill("fixture-camera-account")
        page.locator("#new-camera-password").fill("generated-camera-password")
        page.locator("#test-camera-connection").click()
        expect(page.locator("#add-camera-message")).to_contain_text("Camera account accepted.")
        expect(page.locator("#add-camera-message")).to_contain_text(
            "video is checked after connecting"
        )
        expect(page.locator("#new-camera-password")).to_have_value("")
        expect(page.locator("#save-camera")).to_be_enabled()
        assert "Connection verified" not in page.locator("#add-camera-message").inner_text()
        page.locator("#save-camera").click()
        expect(page.locator("#add-camera-dialog")).not_to_be_visible()
        notice = page.locator("#workspace-message")
        expect(notice).to_contain_text("Camera saved. Waiting for its video stream.")
        expect(notice).to_contain_text("Live video and recording are not confirmed yet.")
        expect(page.locator("#connection-badge")).to_have_text("Connecting")
        expect(page.locator("#recording-badge")).not_to_be_visible()

        def refresh(expected):
            page.locator("#refresh-cameras").click()
            expect(notice).to_contain_text(expected)

        camera.update(status="live", recording=False)
        refresh("Camera stream is reaching K5. Recording is not active.")
        expect(page.locator("#recording-badge")).not_to_be_visible()
        camera["recording"] = True
        refresh("Camera stream is reaching K5. Recording is enabled.")
        expect(page.locator("#recording-badge")).to_have_text("Recording enabled")
        service["worker_status"] = "disk_full"
        refresh("Recording is paused because storage needs attention.")
        expect(page.locator("#recording-badge")).to_have_text("Recording paused")
        service["worker_status"] = "unavailable"
        refresh("The media service is unavailable.")
        service["worker_status"] = "ready"
        camera.update(status="disconnected", recording=False)
        refresh("its video stream is disconnected.")
        camera.update(status="live", recording=True)
        refresh("Camera stream is reaching K5. Recording is enabled.")

        other = {
            **camera,
            "device_id": _OTHER,
            "name": "Other generated camera",
            "host": "127.0.0.2",
        }
        cameras.append(other)
        page.locator("#refresh-cameras").click()
        page.locator(".camera-item").filter(has_text=other["name"]).click()
        expect(notice).not_to_be_visible()
        page.locator(".camera-item").filter(has_text=camera["name"]).click()
        expect(notice).not_to_be_visible()
        page.locator("#remove-camera-button").click()
        page.locator("#confirm-remove-camera").click()
        expect(notice).to_contain_text("Camera removed.")
        page.locator("#refresh-cameras").click()
        expect(notice).to_contain_text("Camera removed.")
        page.locator("#sign-out-button").click()
        expect(page.locator("#sign-in")).to_be_visible()
        expect(notice).not_to_be_visible()
        assert calls == {"profiles": 1, "connect": 1}
        assert not script_errors
    finally:
        if context is not None:
            context.close()
        if browser is not None:
            browser.close()
        if playwright is not None:
            playwright.stop()
