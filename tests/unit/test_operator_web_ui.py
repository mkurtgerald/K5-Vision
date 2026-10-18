"""Real-browser behavior checks for enrollment status, using intercepted APIs.

These isolate UI promises and state transitions. Actual media, ONVIF and native
Windows acceptance remain independently qualified by the integration lanes.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("K5_OPERATOR_BROWSER_ACCEPTANCE") != "1",
    reason="UI behavior proof requires the explicit browser acceptance environment",
)
_CAMERA = "e4fd401d-ae8e-4ac2-9f92-e8287f241532"
_OTHER = "2cad3cbb-c56a-4368-b3e6-e5032576b77a"


@pytest.mark.parametrize("mode", ["add", "reconnect"])
@pytest.mark.parametrize("browser_shift_seconds", [0, 172800, -172800])
def test_profile_acceptance_and_enrollment_wait_for_actual_stream_status(
    mode, browser_shift_seconds
):
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
    service = {
        "worker_status": "ready",
        "clock": {
            "status": "observing",
            "last_step_seconds": 0,
            "change_count": 0,
            "last_change_at": None,
            "sync_verified": False,
        },
    }
    account = {"username": "fixture-admin", "role": "administrator", "display_name": "Fixture"}
    calls, script_errors = {"profiles": 0, "connect": 0}, []
    history_queries = []
    clock_response = {"failed": False, "missing": False}
    playwright = browser = context = None
    try:
        playwright = browser_api.sync_playwright().start()
        browser = playwright.chromium.launch(channel=channel, headless=True)
        context = browser.new_context()
        context.add_init_script(
            """(() => {
              const shift = __SHIFT__;
              const RealDate = Date;
              window.Date = class extends RealDate {
                constructor(...args) { super(...(args.length ? args : [RealDate.now() + shift])); }
                static now() { return RealDate.now() + shift; }
              };
            })();""".replace("__SHIFT__", str(browser_shift_seconds * 1000))
        )
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
                    "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
                    "expires_in_seconds": 300,
                }
            elif path == "/api/v1/auth/me":
                data = account
            elif path == "/api/v1/operator/status":
                data = {
                    **service,
                    "server_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                    "storage": None,
                    "max_recording_bytes": 20 * 1024**3,
                }
                if clock_response["failed"]:
                    status, data = 503, {"detail": "Status unavailable"}
                elif clock_response["missing"]:
                    data.pop("server_utc")
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
                    "clock": {
                        "status": "measured",
                        "offset_seconds": 1200,
                        "uncertainty_seconds": 1.1,
                        "measured_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                        "reported_mode": "ntp",
                        "sync_verified": False,
                    },
                }
                if calls["profiles"] == 1:
                    status = 422
                    data = {
                        "detail": {
                            "code": "camera_auth_failed",
                            "clock": {
                                "status": "unavailable",
                                "offset_seconds": None,
                                "measured_at": None,
                                "uncertainty_seconds": None,
                                "reported_mode": "unknown",
                                "sync_verified": False,
                            },
                        }
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
            elif path.endswith("/history"):
                history_queries.append(parse_qs(urlsplit(route.request.url).query))
                data = {"spans": []}
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
        expect(page.locator("#recorder-time-status")).to_contain_text("UTC")
        expect(page.locator("#recorder-time-status")).to_contain_text(
            "Time synchronization is not verified"
        )
        page.locator(
            "#update-source-button" if mode == "reconnect" else "#add-camera-button"
        ).click()
        if mode == "add":
            page.locator("#new-camera-name").fill(camera["name"])
            page.locator("#new-camera-host").fill(camera["host"])
        page.locator("#new-camera-username").fill("fixture-camera-account")
        page.locator("#new-camera-password").fill("generated-camera-password")
        page.locator("#test-camera-connection").click()
        expect(page.locator("#add-camera-message")).to_contain_text("Camera authentication failed")
        expect(page.locator("#camera-clock-status")).to_contain_text(
            "Camera time could not be checked"
        )
        expect(page.locator("#workspace")).to_be_visible()
        page.locator("#new-camera-password").fill("generated-camera-password")
        page.locator("#test-camera-connection").click()
        expect(page.locator("#add-camera-message")).to_contain_text("Camera account accepted.")
        expect(page.locator("#add-camera-message")).to_contain_text(
            "video is checked after connecting"
        )
        expect(page.locator("#new-camera-password")).to_have_value("")
        expect(page.locator("#camera-clock-status")).to_contain_text(
            "1200.0 seconds ahead of the recorder"
        )
        expect(page.locator("#camera-clock-status")).to_contain_text("Camera reports NTP mode")
        expect(page.locator("#camera-clock-status")).to_contain_text(
            "Time synchronization is not verified"
        )
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
        service["clock"].update(status="changed", last_step_seconds=-3600, change_count=1)
        page.locator("#refresh-cameras").click()
        expect(page.locator("#recorder-time-warning")).to_contain_text(
            "The recorder clock changed by 3600.0 seconds"
        )
        page.locator("#history-tab").click()
        expect(page.locator("#history-message")).to_contain_text(
            "No recorded video is available in this interval"
        )
        assert history_queries
        start = datetime.fromisoformat(history_queries[-1]["start"][0].replace("Z", "+00:00"))
        end = datetime.fromisoformat(history_queries[-1]["end"][0].replace("Z", "+00:00"))
        assert end - start == timedelta(days=1)
        assert abs((end - datetime.now(UTC)).total_seconds()) < 5
        expect(page.locator("#workspace")).to_be_visible()
        for reason in ("failed", "missing"):
            clock_response.update(failed=reason == "failed", missing=reason == "missing")
            before = len(history_queries)
            page.locator("#history-start").fill("2026-10-10T00:00")
            page.locator("#history-end").fill("2026-10-11T00:00")
            page.locator("#last-24-hours").click()
            expect(page.locator("#history-message")).to_contain_text("Recorder time is unavailable")
            assert len(history_queries) == before
            expect(page.locator("#recorder-time-warning")).to_be_visible()
            page.locator("#history-start").fill("2026-10-09T00:00")
            page.locator("#history-end").fill("2026-10-10T00:00")
            page.locator("#history-form").evaluate("form => form.requestSubmit()")
            expect(page.locator("#history-message")).to_contain_text(
                "No recorded video is available in this interval"
            )
            assert len(history_queries) == before + 1
        clock_response.update(failed=False, missing=False)

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
        assert calls == {"profiles": 2, "connect": 1}
        assert not script_errors
    finally:
        if context is not None:
            context.close()
        if browser is not None:
            browser.close()
        if playwright is not None:
            playwright.stop()
