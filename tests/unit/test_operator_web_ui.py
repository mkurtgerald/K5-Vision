"""Real-browser behavior checks for enrollment status, using intercepted APIs.

These isolate UI promises and state transitions. Actual media, ONVIF and native
Windows acceptance remain independently qualified by the integration lanes.
"""

from __future__ import annotations

import importlib.util
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


def test_datetime_local_minute_boundary_accepts_shared_formatter_and_floors_microseconds():
    """Chromium canonicalizes zero seconds, which strict fill() must preserve."""
    browser_api = pytest.importorskip("playwright.sync_api")
    helper_path = Path(__file__).resolve().parents[1] / "integration/test_operator_beta_browser.py"
    spec = importlib.util.spec_from_file_location("k5_datetime_local_browser_helper", helper_path)
    assert spec is not None and spec.loader is not None
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    channel = os.getenv("K5_OPERATOR_BROWSER_CHANNEL", "msedge" if os.name == "nt" else "chromium")
    with browser_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel=channel, headless=True)
        try:
            page = browser.new_page()
            page.set_content('<input id="time" type="datetime-local" step=".001">')
            field = page.locator("#time")
            minute = datetime(2026, 10, 22, 22, 50, 0, 987654, tzinfo=UTC)
            with pytest.raises(browser_api.Error, match="Malformed value"):
                field.fill(minute.strftime("%Y-%m-%dT%H:%M:%S"))
            nonzero = minute.replace(second=1)
            field.fill(nonzero.strftime("%Y-%m-%dT%H:%M:%S"))
            assert field.input_value() == "2026-10-22T22:50:01"
            for desired in (minute, nonzero):
                field.fill(helper._datetime_local_value(desired))
                actual = datetime.fromisoformat(field.input_value()).replace(tzinfo=UTC)
                assert actual == desired.replace(microsecond=0)
        finally:
            browser.close()


def _operator_asset(route, assets):
    """Serve the assembled production UI modules, including the default wall."""
    path = urlsplit(route.request.url).path
    if not path.startswith("/operator/"):
        return False
    name = path.rsplit("/", 1)[1] or "index.html"
    if name not in {
        "index.html",
        "app.js",
        "app.css",
        "wall.js",
        "wall.css",
        "timeline.js",
        "timeline.css",
    }:
        route.fulfill(status=404)
        return True
    content_type = {
        ".html": "text/html",
        ".js": "application/javascript",
        ".css": "text/css",
    }[Path(name).suffix]
    route.fulfill(content_type=content_type, body=(assets / name).read_text())
    return True


def test_focused_camera_switch_observes_its_own_grant_without_wall_response_ambiguity():
    """Observe the real UI requests, without manufacturing a CDP resource error."""
    browser_api = pytest.importorskip("playwright.sync_api")
    channel = os.getenv("K5_OPERATOR_BROWSER_CHANNEL", "msedge" if os.name == "nt" else "chromium")
    assets = Path(__file__).resolve().parents[2] / "src/k5vision/operator_web"
    identifiers = (
        _CAMERA,
        _OTHER,
        "23d89cf4-6d30-421e-b833-569420d1a44b",
        "54d00da5-2973-469e-a5aa-d9448944ce0f",
    )
    cameras = [
        {
            "device_id": identifier,
            "name": f"Generated observer camera {index}",
            "host": f"127.0.0.{index + 1}",
            "status": "live",
            "recording": True,
            "recording_enabled": True,
            "recording_state": "recording",
            "analytics_status": "disabled",
        }
        for index, identifier in enumerate(identifiers)
    ]
    account = {"username": "fixture-admin", "role": "administrator", "display_name": "Fixture"}
    phase = {"mode": "normal", "target": _CAMERA}
    grants, pending, errors = [], [], []
    playwright = browser = context = None
    try:
        playwright = browser_api.sync_playwright().start()
        browser = playwright.chromium.launch(channel=channel, headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        context.add_init_script("window.Hls = {isSupported: () => false};")
        page = context.new_page()
        page.set_default_timeout(10_000)
        page.on("pageerror", lambda _error: errors.append("uncaught_script_error"))

        def fulfill_grant(route, kind):
            identifier = urlsplit(route.request.url).path.split("/")[-2]
            route.fulfill(
                content_type="application/json",
                headers={"x-ui-test-grant": kind},
                body=json.dumps(
                    {
                        "live_url": f"/media/live/{identifier}/index.m3u8",
                        "playback_url": f"/media/playback?path={identifier}",
                    }
                ),
            )

        def intercept(route):
            if _operator_asset(route, assets):
                return
            path = urlsplit(route.request.url).path
            data, status = {}, 200
            if path == "/api/v1/auth/login":
                data = {
                    "session_token": "generated-observer-session-token-000000000000000000",
                    "account": account,
                    "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
                    "expires_in_seconds": 300,
                }
            elif path == "/api/v1/auth/me":
                data = account
            elif path == "/api/v1/operator/status":
                data = {"worker_status": "ready", "server_utc": datetime.now(UTC).isoformat()}
            elif path == "/api/v1/operator/cameras":
                data = {"cameras": cameras, "worker_status": "ready"}
            elif path.endswith("/media-session") and route.request.method == "POST":
                identifier = path.split("/")[-2]
                current = [grant for grant in grants if grant[0] == phase["mode"]]
                target_seen = any(grant[1] == phase["target"] for grant in current)
                kind = "normal" if phase["mode"] == "normal" else "wall"
                if phase["mode"] == "direct" or identifier == phase["target"] and target_seen:
                    kind = "focused"
                grants.append((phase["mode"], identifier, kind))
                if kind == "focused":
                    pending.append(route)
                else:
                    fulfill_grant(route, kind)
                return
            elif path.endswith("/history"):
                data = {"spans": []}
            else:
                status = 404
            route.fulfill(status=status, content_type="application/json", body=json.dumps(data))

        def target_response(response):
            return response.request.method == "POST" and urlsplit(response.url).path == (
                f"/api/v1/operator/cameras/{phase['target']}/media-session"
            )

        page.route("**/*", intercept)
        page.goto("http://127.0.0.1:8000/operator/")
        page.locator("#username").fill("fixture-admin")
        page.locator("#password").fill("generated-human-account-password")
        page.locator("#sign-in-button").click()
        expect = browser_api.expect
        expect(page.locator("#wall-panel video")).to_have_count(4)
        page.locator(".camera-item").filter(has_text=cameras[1]["name"]).click()
        expect(page.locator("#live-panel")).to_be_visible()
        phase.update(mode="wall_then_watch", target=_CAMERA)
        with page.expect_response(target_response) as ambiguous:
            with page.expect_response(
                lambda response: (
                    response.request.method == "POST"
                    and urlsplit(response.url).path.endswith("/media-session")
                    and len([grant for grant in grants if grant[0] == "wall_then_watch"]) == 4
                )
            ):
                page.locator("#wall-tab").click()
            with page.expect_request(
                lambda request: (
                    request.method == "POST"
                    and urlsplit(request.url).path
                    == f"/api/v1/operator/cameras/{_CAMERA}/media-session"
                )
            ):
                page.locator(
                    f'#wall-panel [data-camera-id="{_CAMERA}"] [data-wall-action="watch"]'
                ).click()
        assert ambiguous.value.header_value("x-ui-test-grant") == "wall"
        assert len(pending) == 1
        assert [grant[2] for grant in grants if grant[0] == "wall_then_watch"].count("wall") == 4
        with page.expect_response(target_response):
            fulfill_grant(pending.pop(), "focused")
        expect(page.locator("#live-panel")).to_be_visible()

        phase.update(mode="direct", target=_OTHER)
        with page.expect_response(target_response) as focused:
            with page.expect_request(
                lambda request: (
                    request.method == "POST"
                    and urlsplit(request.url).path
                    == f"/api/v1/operator/cameras/{_OTHER}/media-session"
                )
            ):
                page.locator(".camera-item").filter(has_text=cameras[1]["name"]).click()
            assert len(pending) == 1
            assert [grant for grant in grants if grant[0] == "direct"] == [
                ("direct", _OTHER, "focused")
            ]
            fulfill_grant(pending.pop(), "focused_direct")
        assert focused.value.header_value("x-ui-test-grant") == "focused_direct"
        assert focused.value.json()["live_url"] == f"/media/live/{_OTHER}/index.m3u8"
        expect(page.locator("#wall-panel")).to_be_hidden()
        expect(page.locator("#live-panel")).to_be_visible()
        assert not errors
    finally:
        if context is not None:
            context.close()
        if browser is not None:
            browser.close()
        if playwright is not None:
            playwright.stop()


def test_late_camera_connection_completion_preserves_explicit_wall_choice():
    """A held reconnect refresh must not override a later explicit wall choice.

    API responses isolate the UI race; this is not a decoded-media qualification.
    All lists requested after the successful connection are held until navigation.
    """
    browser_api = pytest.importorskip("playwright.sync_api")
    channel = os.getenv("K5_OPERATOR_BROWSER_CHANNEL", "msedge" if os.name == "nt" else "chromium")
    assets = Path(__file__).resolve().parents[2] / "src/k5vision/operator_web"
    identifiers = (
        _CAMERA,
        _OTHER,
        "23d89cf4-6d30-421e-b833-569420d1a44b",
        "54d00da5-2973-469e-a5aa-d9448944ce0f",
    )
    cameras = [
        {
            "device_id": identifier,
            "name": f"Generated navigation camera {index}",
            "host": f"127.0.0.{index + 1}",
            "management_port": 80,
            "management_secure": False,
            "status": "live",
            "recording": True,
            "recording_enabled": True,
            "recording_state": "recording",
            "recording_status": {"reason": "observed"},
            "analytics_status": "disabled",
        }
        for index, identifier in enumerate(identifiers)
    ]
    account = {"username": "fixture-admin", "role": "administrator", "display_name": "Fixture"}
    pending, errors = [], []
    hold = {"lists": False}
    playwright = browser = context = None
    try:
        playwright = browser_api.sync_playwright().start()
        browser = playwright.chromium.launch(channel=channel, headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        context.add_init_script("window.Hls = {isSupported: () => false};")
        page = context.new_page()
        page.set_default_timeout(10_000)
        now = datetime.now(UTC)
        page.on("pageerror", lambda _error: errors.append("uncaught_script_error"))

        def intercept(route):
            if _operator_asset(route, assets):
                return
            path = urlsplit(route.request.url).path
            status, data = 200, {}
            if path == "/api/v1/auth/login":
                data = {
                    "session_token": "generated-navigation-session-token-000000000000000000",
                    "account": account,
                    "expires_at": (now + timedelta(minutes=5)).isoformat(),
                    "expires_in_seconds": 300,
                }
            elif path == "/api/v1/auth/me":
                data = account
            elif path == "/api/v1/operator/status":
                data = {"worker_status": "ready", "server_utc": now.isoformat()}
            elif path == "/api/v1/operator/cameras":
                data = {"cameras": cameras, "worker_status": "ready"}
                if hold["lists"]:
                    pending.append(route)
                    return
            elif path == "/api/v1/operator/camera-profiles":
                data = {
                    "probe_id": "p" * 32,
                    "expires_in_seconds": 120,
                    "profiles": [
                        {
                            "id": "s" * 32,
                            "name": "Generated main stream",
                            "codec": "H264",
                            "width": 320,
                            "height": 240,
                            "supported": True,
                        }
                    ],
                }
            elif path == "/api/v1/operator/cameras/connect":
                hold["lists"] = True
                status, data = 201, cameras[0]
            elif path.endswith("/media-session"):
                identifier = path.split("/")[-2]
                data = {
                    "live_url": f"/media/live/{identifier}/index.m3u8",
                    "playback_url": f"/media/playback?path={identifier}",
                }
            elif path.endswith("/history"):
                data = {"spans": []}
            else:
                status = 404
            route.fulfill(status=status, content_type="application/json", body=json.dumps(data))

        page.route("**/*", intercept)
        page.goto("http://127.0.0.1:8000/operator/")
        page.locator("#username").fill("fixture-admin")
        page.locator("#password").fill("generated-human-account-password")
        page.locator("#sign-in-button").click()
        expect = browser_api.expect
        expect(page.locator("#wall-panel")).to_be_visible()
        expect(page.locator("#wall-panel video")).to_have_count(4)
        page.locator(f'[data-camera-id="{_CAMERA}"] [data-wall-action="watch"]').click()
        expect(page.locator("#live-panel")).to_be_visible()
        page.locator("#update-source-button").click()
        page.locator("#new-camera-username").fill("fixture-camera-account")
        page.locator("#new-camera-password").fill("generated-camera-password")
        page.locator("#test-camera-connection").click()
        expect(page.locator("#save-camera")).to_be_enabled()
        with page.expect_request(
            lambda request: (
                hold["lists"] and urlsplit(request.url).path == "/api/v1/operator/cameras"
            )
        ):
            page.locator("#save-camera").click()
        expect(page.locator("#add-camera-dialog")).to_be_hidden()
        assert pending
        page.locator("#wall-tab").click()
        expect(page.locator("#wall-panel")).to_be_visible()
        expect(page.locator("#wall-panel video")).to_have_count(4)
        players = [
            page.locator(f'#wall-panel [data-camera-id="{identifier}"] video').element_handle()
            for identifier in identifiers
        ]
        with page.expect_response(
            lambda response: urlsplit(response.url).path == "/api/v1/operator/cameras"
        ):
            hold["lists"] = False
            while pending:
                pending.pop().fulfill(
                    content_type="application/json",
                    body=json.dumps({"cameras": cameras, "worker_status": "ready"}),
                )
        # Two real animation frames run all response promise continuations and
        # deferred wall layout work, without a fixed delay to hide the race.
        page.evaluate(
            "() => new Promise(resolve => "
            "requestAnimationFrame(() => requestAnimationFrame(resolve)))"
        )
        expect(page.locator("#wall-panel")).to_be_visible()
        expect(page.locator("#live-panel")).to_be_hidden()
        assert all(
            player.evaluate(
                "(video,id) => video.isConnected && "
                'document.querySelector(`[data-camera-id="${id}"] video`)===video',
                identifier,
            )
            for identifier, player in zip(identifiers, players, strict=True)
        )
        assert not errors
    finally:
        if context is not None:
            context.close()
        if browser is not None:
            browser.close()
        if playwright is not None:
            playwright.stop()


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
        "recording_enabled": True,
        "recording_state": "recording",
        "recording_status": {"reason": "observed"},
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
    held_camera_lists = []
    hold_camera_list = {"next": False}
    clock_response = {"failed": False, "missing": False}
    playwright = browser = context = None
    try:
        playwright = browser_api.sync_playwright().start()
        browser = playwright.chromium.launch(channel=channel, headless=True)
        context = browser.new_context()
        context.add_init_script(
            """(() => {
              const shift = __SHIFT__;
              // Playback itself is qualified by the actual media acceptance.
              // Keep the authentic media-session DTO while disabling transport.
              window.Hls = {isSupported: () => false};
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
            if _operator_asset(route, assets):
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
                if hold_camera_list["next"]:
                    hold_camera_list["next"] = False
                    held_camera_lists.append(
                        (route, {**service, "cameras": [dict(item) for item in cameras]})
                    )
                    return
            elif path.endswith("/recording") and method == "PUT":
                enabled = route.request.post_data_json["enabled"]
                camera.update(
                    recording=enabled,
                    recording_enabled=enabled,
                    recording_state="recording" if enabled else "paused",
                    recording_status={"reason": "observed" if enabled else "operator_paused"},
                )
                data = camera
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
                camera.update(
                    status="connecting",
                    recording=False,
                    recording_enabled=True,
                    recording_state="waiting",
                    recording_status={"reason": "awaiting_data"},
                )
                cameras[:] = [camera]
                status, data = 201, camera
            elif path.endswith("/media-session") and path != "/api/v1/operator/media-session":
                camera_id = path.split("/")[-2]
                data = {
                    "live_url": f"/media/live/{camera_id}/index.m3u8",
                    "playback_url": f"/media/playback?path={camera_id}",
                }
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
        expect(page.locator("#wall-panel")).to_be_visible()
        expect(page.locator("#recorder-time-status")).to_contain_text("UTC")
        expect(page.locator("#recorder-time-status")).to_contain_text(
            "Time synchronization is not verified"
        )
        if mode == "reconnect":
            page.locator(f'[data-camera-id="{_CAMERA}"] [data-wall-action="watch"]').click()
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
        expect(page.locator("#recording-badge")).to_have_text("Waiting for video")

        def refresh(expected):
            page.locator("#refresh-cameras").click()
            expect(notice).to_contain_text(expected)

        camera.update(
            status="live",
            recording=False,
            recording_enabled=False,
            recording_state="paused",
            recording_status={"reason": "operator_paused"},
        )
        refresh("Recording is paused for this camera.")
        expect(page.locator("#recording-badge")).to_have_text("Recording paused")
        camera.update(
            recording=True,
            recording_enabled=True,
            recording_state="waiting",
            recording_status={"reason": "awaiting_data"},
        )
        refresh("Waiting to confirm new recording data on disk.")
        expect(page.locator("#recording-badge")).to_have_text("Checking recording")
        expect(page.locator("#summary-recording")).to_have_text("0")
        camera.update(recording_state="recording", recording_status={"reason": "observed"})
        refresh("New recording data has been observed on disk.")
        expect(page.locator("#recording-badge")).to_have_text("Recording")
        expect(page.locator("#summary-recording")).to_have_text("1")
        service["worker_status"] = "disk_full"
        refresh("Recording is paused because storage needs attention.")
        expect(page.locator("#recording-badge")).to_have_text("Storage paused")
        service["worker_status"] = "unavailable"
        refresh("The media service is unavailable.")
        service["worker_status"] = "ready"
        camera.update(status="disconnected", recording=False, recording_state="waiting")
        refresh("its video stream is disconnected.")
        camera.update(status="live", recording=True, recording_state="recording")
        refresh("New recording data has been observed on disk.")
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

        if mode == "add" and browser_shift_seconds == 0:
            # An older poll can finish after the user's pause PUT and its fresh
            # list response. Releasing it must not undo the confirmed choice.
            hold_camera_list["next"] = True
            page.locator("#refresh-cameras").click()
            page.wait_for_timeout(50)
            assert len(held_camera_lists) == 1
            page.locator("#toggle-recording").click()
            expect(page.locator("#recording-badge")).to_have_text("Recording paused")
            old_route, old_snapshot = held_camera_lists.pop()
            old_route.fulfill(content_type="application/json", body=json.dumps(old_snapshot))
            page.evaluate("""async () => {
              await new Promise(requestAnimationFrame); await new Promise(requestAnimationFrame);
            }""")
            assert page.locator("#toggle-recording").get_attribute("data-enabled") == "false"
            assert page.locator("#recording-badge").inner_text() == "Recording paused"
            page.locator("#toggle-recording").click()
            expect(page.locator("#recording-badge")).to_have_text("Recording")

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


@pytest.mark.parametrize(
    "outcome",
    ["recover_401", "recover_403", "expired", "repeated_denial", "selection_race", "logout_race"],
)
def test_live_media_cookie_recovery_preserves_authoritative_session(outcome):
    """Exercise HTTP cookie loss and HLS errors without asserting decoded media."""
    browser_api = pytest.importorskip("playwright.sync_api")
    channel = os.getenv("K5_OPERATOR_BROWSER_CHANNEL", "msedge" if os.name == "nt" else "chromium")
    assets = Path(__file__).resolve().parents[2] / "src/k5vision/operator_web"
    token = "generated-media-recovery-session-token-000000000000000000"
    account = {"username": "fixture-admin", "role": "administrator", "display_name": "Fixture"}
    cameras = [
        {"device_id": camera_id, "name": name, "status": "live", "recording": True}
        for camera_id, name in [(_CAMERA, "First camera"), (_OTHER, "Second camera")]
    ]
    media_calls, logins, errors, pending = [], [], [], []
    deny_media = {"active": False}
    recovery = {"armed": False, "calls": 0}
    playwright = browser = context = None

    def media_urls(camera_id):
        return {
            "live_url": f"/media/live/{camera_id}/index.m3u8",
            "playback_url": f"/media/playback?path={camera_id}",
        }

    try:
        playwright = browser_api.sync_playwright().start()
        browser = playwright.chromium.launch(channel=channel, headless=True)
        context = browser.new_context()
        # A small HLS adapter delivers actual intercepted HTTP status codes to
        # the production error handler. Generated-video decoding is qualified
        # independently by the media/native integration lanes.
        context.add_init_script("""(() => {
          window.__hlsPlayers = [];
          window.__hlsSources = [];
          HTMLMediaElement.prototype.play = () => Promise.reject(
            new DOMException("Generated authorization test", "NotAllowedError")
          );
          window.Hls = class {
            static Events = {
              ERROR: "error", MEDIA_ATTACHED: "attached", MANIFEST_PARSED: "parsed"
            };
            static isSupported() { return true; }
            constructor() { this.handlers = {}; window.__hlsPlayers.push(this); }
            on(event, callback) { this.handlers[event] = callback; }
            attachMedia() { queueMicrotask(() => this.handlers.attached?.()); }
            loadSource(url) { this.url = url; window.__hlsSources.push(url); void this.reload(); }
            async reload() {
              const response = await fetch(this.url, {
                credentials: "same-origin", cache: "no-store"
              });
              if (response.ok) this.handlers.parsed?.();
              else this.handlers.error?.("error", {
                fatal: true, response: { code: response.status }
              });
            }
            destroy() { this.destroyed = true; }
          };
        })();""")
        page = context.new_page()
        page.set_default_timeout(10_000)
        page.on("pageerror", lambda _error: errors.append("uncaught_script_error"))

        def intercept(route):
            path, method = urlsplit(route.request.url).path, route.request.method
            if _operator_asset(route, assets):
                return
            status, data, headers = 200, {}, {}
            if path == "/api/v1/auth/login":
                logins.append(True)
                data = {
                    "session_token": token,
                    "account": account,
                    "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
                    "expires_in_seconds": 300,
                }
            elif path == "/api/v1/auth/me":
                data = account
            elif path == "/api/v1/operator/cameras":
                data = {"worker_status": "ready", "cameras": cameras}
            elif path == "/api/v1/operator/status":
                data = {
                    "worker_status": "ready",
                    "server_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                    "clock": {"status": "observing", "sync_verified": False},
                }
            elif path == "/api/v1/auth/logout" and outcome == "logout_race":
                pending.append(route)
                return
            elif path.endswith("/media-session") and method == "POST":
                camera_id = path.split("/")[-2]
                media_calls.append((camera_id, route.request.all_headers().get("authorization")))
                if recovery["armed"]:
                    recovery["calls"] += 1
                if recovery["calls"] == 1 and outcome == "selection_race":
                    pending.append(route)
                    return
                if recovery["calls"] == 1 and outcome == "expired":
                    status, data = 401, {"detail": "Unauthorized"}
                else:
                    data = media_urls(camera_id)
                    headers["Set-Cookie"] = (
                        f"k5_operator_media={token}; Path=/media; HttpOnly; SameSite=Strict"
                    )
            elif path.startswith("/media/live/"):
                cookie = route.request.all_headers().get("cookie", "")
                if f"k5_operator_media={token}" not in cookie or deny_media["active"]:
                    status = 403 if outcome == "recover_403" else 401
            elif path == "/api/v1/operator/media-session" and method == "DELETE":
                status = 204
            else:
                status = 404
            route.fulfill(
                status=status,
                headers=headers,
                content_type="application/json",
                body="" if status == 204 else json.dumps(data),
            )

        page.route("**/*", intercept)
        page.goto("http://127.0.0.1:8000/operator/")
        page.locator("#username").fill("fixture-admin")
        page.locator("#password").fill("generated-human-account-password")
        page.locator("#sign-in-button").click()
        expect = browser_api.expect
        expect(page.locator("#wall-panel")).to_be_visible()
        page.wait_for_function("window.__hlsPlayers.length === 2")
        page.locator(f'[data-camera-id="{_CAMERA}"] [data-wall-action="watch"]').click()
        expect(page.locator("#play-live")).to_be_visible()
        baseline_calls = len(media_calls)
        baseline_players = page.evaluate("window.__hlsPlayers.length")
        baseline_sources = page.evaluate("window.__hlsSources")
        assert media_calls[-1][0] == _CAMERA
        assert baseline_sources[-1] == media_urls(_CAMERA)["live_url"]
        page.evaluate("window.__selectedHlsIndex = window.__hlsPlayers.length - 1")
        if outcome == "logout_race":
            page.locator("#sign-out-button").click()
            assert len(pending) == 1
            page.locator("#wall-tab").click()
            page.evaluate("""async () => {
              await new Promise(requestAnimationFrame); await new Promise(requestAnimationFrame);
            }""")
            assert page.locator(".camera-wall-tile").count() == 0
            assert len(media_calls) == baseline_calls
            assert page.evaluate("window.__hlsPlayers.every(player => player.destroyed)")
            pending.pop().fulfill(status=204, body="")
            expect(page.locator("#sign-in")).to_be_visible()
            assert len(logins) == 1
            assert not errors
            return
        recovery["armed"] = True
        context.clear_cookies()
        deny_media["active"] = outcome == "repeated_denial"
        page.evaluate("void window.__hlsPlayers.at(-1).reload()")
        page.wait_for_function("window.__hlsPlayers[window.__selectedHlsIndex].destroyed === true")

        if outcome == "expired":
            expect(page.locator("#sign-in")).to_be_visible()
            expect(page.locator("#workspace")).not_to_be_visible()
            expect(page.locator("#sign-in-message")).to_contain_text("Your session has ended")
        elif outcome == "repeated_denial":
            expect(page.locator("#live-placeholder-title")).to_have_text("Media access unavailable")
            expect(page.locator("#retry-live")).to_be_visible()
            expect(page.locator("#workspace")).to_be_visible()
            assert page.evaluate("window.__hlsPlayers.length") == baseline_players + 1
        elif outcome == "selection_race":
            expect(page.locator("#live-placeholder-title")).to_have_text("Restoring media access")
            page.locator(".camera-item").filter(has_text="Second camera").click()
            expect(page.locator("#play-live")).to_be_visible()
            assert len(pending) == 1
            pending.pop().fulfill(
                content_type="application/json", body=json.dumps(media_urls(_CAMERA))
            )
            page.evaluate(
                "window.__hlsPlayers[window.__selectedHlsIndex].handlers.error("
                "'error', {fatal:true, response:{code:401}})"
            )
            expect(page.locator("#camera-title")).to_have_text("Second camera")
            assert page.evaluate("window.__hlsSources") == [
                *baseline_sources,
                media_urls(_OTHER)["live_url"],
            ]
        else:
            expect(page.locator("#play-live")).to_be_visible()
            expect(page.locator("#workspace")).to_be_visible()
            assert page.evaluate("window.__hlsPlayers.length") == baseline_players + 1
        assert len(media_calls) == baseline_calls + (2 if outcome == "selection_race" else 1)
        assert all(authorization == f"Bearer {token}" for _, authorization in media_calls)
        assert len(logins) == 1
        assert not errors
    finally:
        if context is not None:
            context.close()
        if browser is not None:
            browser.close()
        if playwright is not None:
            playwright.stop()


@pytest.mark.parametrize("late_error", ["AbortError", "NotAllowedError"])
def test_timeline_gap_clears_previous_clip_and_download_without_losing_history(late_error):
    """Use assembled UI events; actual encoded playback is qualified separately."""
    browser_api = pytest.importorskip("playwright.sync_api")
    channel = os.getenv("K5_OPERATOR_BROWSER_CHANNEL", "msedge" if os.name == "nt" else "chromium")
    assets = Path(__file__).resolve().parents[2] / "src/k5vision/operator_web"
    account = {"username": "fixture-admin", "role": "administrator"}
    start = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=1)
    end = start + timedelta(minutes=1)
    spans = [
        {
            "recording_id": "generated-clip-1",
            "started_at": (start + timedelta(seconds=5)).isoformat(),
            "duration_seconds": 10,
        },
        {
            "recording_id": "generated-clip-2",
            "started_at": (start + timedelta(seconds=40)).isoformat(),
            "duration_seconds": 10,
        },
    ]
    camera = {
        "device_id": _CAMERA,
        "name": "Generated history camera",
        "status": "live",
        "recording_enabled": True,
        "recording_state": "recording",
    }
    with browser_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel=channel, headless=True)
        context = browser.new_context()
        context.add_init_script("""(() => {
          window.Hls = {isSupported: () => false};
          window.playbackStarts = 0;
          HTMLMediaElement.prototype.play = function () {
            if (this.id === 'playback-video') {
              window.playbackStarts += 1;
              return new Promise((_, reject) => { window.rejectOldPlayback = reject; });
            }
            return Promise.resolve();
          };
          HTMLMediaElement.prototype.pause = function () {};
          HTMLMediaElement.prototype.load = function () {};
        })();""")
        page = context.new_page()
        page.set_default_timeout(10_000)
        errors = []
        page.on("pageerror", lambda _error: errors.append("uncaught_history_script_error"))

        def intercept(route):
            if _operator_asset(route, assets):
                return
            path = urlsplit(route.request.url).path
            status, data = 200, {}
            if path == "/api/v1/auth/login":
                data = {
                    "session_token": "generated-gap-selection-session-token-000000000000000",
                    "account": account,
                    "expires_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
                    "expires_in_seconds": 300,
                }
            elif path == "/api/v1/auth/me":
                data = account
            elif path == "/api/v1/operator/status":
                data = {
                    "worker_status": "ready",
                    "server_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                    "clock": {"status": "observing", "sync_verified": False},
                }
            elif path == "/api/v1/operator/cameras":
                data = {"worker_status": "ready", "cameras": [camera]}
            elif path.endswith("/media-session"):
                data = {
                    "live_url": f"/media/live/{_CAMERA}/index.m3u8",
                    "playback_url": f"/media/playback?path={_CAMERA}",
                }
            elif path.endswith("/history"):
                data = {"spans": spans}
            else:
                status = 204 if path == "/media/playback" else 404
            route.fulfill(
                status=status,
                content_type="application/json",
                body="" if status == 204 else json.dumps(data),
            )

        try:
            page.route("**/*", intercept)
            page.goto("http://127.0.0.1:8000/operator/")
            page.locator("#username").fill("fixture-admin")
            page.locator("#password").fill("generated-human-account-password")
            page.locator("#sign-in-button").click()
            page.locator(f'[data-camera-id="{_CAMERA}"] [data-wall-action="recordings"]').click()
            page.locator("#history-start").fill(
                start.strftime("%Y-%m-%dT%H:%M:%S").removesuffix(":00")
            )
            page.locator("#history-end").fill(end.strftime("%Y-%m-%dT%H:%M:%S").removesuffix(":00"))
            page.locator("#history-form").evaluate("form => form.requestSubmit()")
            expect = browser_api.expect
            expect(page.locator("#history-count")).to_have_text("2")
            expect(page.locator("#recording-timeline")).to_have_attribute(
                "data-window-start", str(int(start.timestamp() * 1000))
            )
            first = page.locator(".history-item").first
            first.click()
            expect(first).to_have_attribute("aria-pressed", "true")
            expect(page.locator("#play-recording")).to_be_enabled()
            expect(page.locator("#clip-mark-in")).to_be_enabled()
            expect(page.locator("#clip-mark-out")).to_be_enabled()
            link = page.locator("#download-recording")
            old_href = link.get_attribute("href")
            assert old_href and old_href.startswith("/media/playback?")
            assert page.locator("#playback-video").get_attribute("src") == old_href
            assert page.evaluate("window.playbackStarts") == 1

            # The selected minute starts with a real five-second recording gap.
            # Keyboard Home exercises the production timeline's seek callback.
            track = page.get_by_role("slider", name="Recording time in UTC")
            track.focus()
            track.press("Home")
            expect(page.locator("#playback-message")).to_contain_text("No playable recording")
            expect(page.locator("#selected-recording")).to_have_text("No interval selected")
            for identifier in (
                "playback-start",
                "playback-duration",
                "play-recording",
                "clip-mark-in",
                "clip-mark-out",
            ):
                expect(page.locator("#" + identifier)).to_be_disabled()
            assert page.locator("#playback-start").input_value() == ""
            assert page.locator("#playback-video").get_attribute("src") is None
            assert link.get_attribute("href") is None
            assert link.get_attribute("download") is None
            expect(link).to_have_attribute("aria-disabled", "true")
            expect(link).to_have_attribute("tabindex", "-1")
            assert page.locator(".history-item[aria-pressed=true]").count() == 0
            assert page.locator(".history-item").count() == 2
            assert page.locator(".k5-timeline-recording").count() == 2
            expect(track).to_have_attribute("aria-disabled", "false")
            assert page.evaluate("window.playbackStarts") == 1
            page.evaluate(
                "name => window.rejectOldPlayback(new DOMException('Stopped old playback', name))",
                late_error,
            )
            expect(page.locator("#playback-message")).to_contain_text("No playable recording")
            assert link.get_attribute("href") is None
            assert not errors
        finally:
            context.close()
            browser.close()
