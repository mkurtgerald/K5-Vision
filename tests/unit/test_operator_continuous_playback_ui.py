"""Long-history navigation and independent export state in the assembled browser UI.

Encoded media and the native Windows download lifecycle are qualified separately;
these tests exercise production controls with a deterministic media element.
"""

from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("K5_OPERATOR_BROWSER_ACCEPTANCE") != "1",
    reason="Playback interaction requires the explicit browser acceptance environment",
)
_CAMERA = "e4fd401d-ae8e-4ac2-9f92-e8287f241532"
_NOW = datetime(2026, 11, 1, 12, tzinfo=UTC)
_BEGIN = _NOW - timedelta(hours=4)
_ASSETS = Path(__file__).resolve().parents[2] / "src/k5vision/operator_web"


@contextmanager
def _history_page(export_response=None, export_handler=None, download_response=None):
    browser_api = pytest.importorskip("playwright.sync_api")
    channel = os.getenv("K5_OPERATOR_BROWSER_CHANNEL", "msedge" if os.name == "nt" else "chromium")
    with browser_api.sync_playwright() as api:
        browser = api.chromium.launch(channel=channel, headless=True)
        context = browser.new_context(
            viewport={"width": 1440, "height": 1100},
            timezone_id="America/New_York",
            accept_downloads=True,
        )
        context.add_init_script("""(() => {
          window.Hls = {isSupported: () => false};
          // These controller tests own the media state; native decoding is a separate gate.
          document.addEventListener('error', event => {
            if (event.target.id === 'playback-video') event.stopImmediatePropagation();
          }, true);
          const clocks = new WeakMap(), paused = new WeakMap();
          Object.defineProperty(HTMLMediaElement.prototype, 'currentTime', {
            get() { return clocks.get(this) || 0; }, set(value) { clocks.set(this, value); }
          });
          Object.defineProperty(HTMLMediaElement.prototype, 'paused', {
            get() { return paused.get(this) !== false; }
          });
          HTMLMediaElement.prototype.load = function () { clocks.set(this, 0); };
          HTMLMediaElement.prototype.pause = function () {
            paused.set(this, true); this.dispatchEvent(new Event('pause'));
          };
          HTMLMediaElement.prototype.play = function () {
            paused.set(this, false); this.dispatchEvent(new Event('playing'));
            return Promise.resolve();
          };
        })();""")
        server = None
        server_thread = None
        origin = "http://127.0.0.1:8000"
        if download_response is not None:

            class DownloadHandler(BaseHTTPRequestHandler):
                def do_GET(self):
                    if not self.path.startswith("/media/exports/"):
                        self.send_error(404)
                        return
                    download_response["requests"].append(self.path)
                    body = download_response["body"]
                    self.send_response(200)
                    self.send_header("Content-Type", "video/mp4")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header(
                        "Content-Disposition", 'attachment; filename="K5-controller-fixture.mp4"'
                    )
                    self.end_headers()
                    self.wfile.write(body)

                def log_message(self, _format, *_args):
                    pass

            server = ThreadingHTTPServer(("127.0.0.1", 0), DownloadHandler)
            server_thread = threading.Thread(target=server.serve_forever, daemon=True)
            server_thread.start()
            origin = f"http://127.0.0.1:{server.server_port}"
        page = context.new_page()
        errors = []
        page.on("pageerror", lambda _error: errors.append("uncaught_playback_script_error"))
        page.set_default_timeout(10000)
        camera = {
            "device_id": _CAMERA,
            "name": "Generated long history",
            "status": "live",
            "recording_enabled": True,
            "recording_state": "recording",
            "analytics_status": "disabled",
        }
        spans = [
            {
                "recording_id": f"generated-{i}",
                "started_at": (
                    _BEGIN + timedelta(minutes=i * 10 + (10 if i >= 12 else 0))
                ).isoformat(),
                "duration_seconds": 600,
            }
            for i in range(18)
        ]
        account = {
            "username": "fixture-admin",
            "display_name": "Generated admin",
            "role": "administrator",
        }

        def route(request):
            path = urlsplit(request.request.url).path
            if export_handler is not None and export_handler(request):
                return
            if download_response is not None and path.startswith("/media/exports/"):
                request.continue_()
                return
            if path.startswith("/operator/"):
                asset = _ASSETS / (path.rsplit("/", 1)[-1] or "index.html")
                if asset.is_file() and asset.parent == _ASSETS:
                    mime = {
                        ".html": "text/html",
                        ".css": "text/css",
                        ".js": "application/javascript",
                    }[asset.suffix]
                    request.fulfill(content_type=mime, body=asset.read_text())
                else:
                    request.fulfill(status=404)
                return
            status, data = 200, {}
            if path == "/api/v1/auth/login":
                data = {
                    "session_token": "generated-continuous-playback-session-000000000000000",
                    "account": account,
                    "expires_at": (_NOW + timedelta(minutes=5)).isoformat(),
                    "expires_in_seconds": 300,
                }
            elif path == "/api/v1/auth/me":
                data = account
            elif path == "/api/v1/operator/status":
                data = {
                    "worker_status": "ready",
                    "server_utc": _NOW.isoformat().replace("+00:00", "Z"),
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
            elif path.endswith("/exports") and export_response is not None:
                status, data = export_response
            else:
                status = 404
            request.fulfill(status=status, content_type="application/json", body=json.dumps(data))

        page.route("**/*", route)
        try:
            page.goto(origin + "/operator/")
            page.locator("#username").fill("fixture-admin")
            page.locator("#password").fill("generated-human-password")
            page.locator("#sign-in-button").click()
            page.locator(f'[data-camera-id="{_CAMERA}"] [data-wall-action="recordings"]').click()
            browser_api.expect(page.locator("#history-count")).to_have_text("18")
            yield page, browser_api.expect
            assert not errors
        finally:
            context.close()
            browser.close()
            if server is not None:
                server.shutdown()
                server.server_close()
                server_thread.join(timeout=2)
                assert not server_thread.is_alive()


def _jump(page, at):
    page.locator("#playback-start").fill(
        at.isoformat(timespec="seconds").replace("+00:00", "").removesuffix(":00")
    )
    page.locator("#playback-form").evaluate("form => form.requestSubmit()")


def test_full_day_zoom_and_rapid_seeks_keep_export_selection_independent():
    with _history_page() as (page, expect):
        timeline = page.locator("#recording-timeline")
        assert page.locator(".k5-recording-timeline").bounding_box()["height"] <= 190
        assert (
            int(timeline.get_attribute("data-window-end"))
            - int(timeline.get_attribute("data-window-start"))
            == 86400000
        )
        page.locator(".history-item").first.click()
        export_start = page.locator("#clip-start").input_value()
        page.locator("#playback-duration").fill("300")
        first = _BEGIN + timedelta(minutes=32)
        final = _BEGIN + timedelta(hours=1, minutes=47, seconds=12)
        _jump(page, first)
        _jump(page, final)
        expect(page.locator("#playback-video")).to_have_attribute(
            "data-utc-start", final.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        )
        assert page.locator("#clip-start").input_value() == export_start
        assert page.locator("#playback-duration").input_value() == "300"
        assert "format=fmp4" in page.locator("#playback-video").get_attribute("src")
        page.locator(".k5-timeline-scale").select_option("300000")
        assert (
            int(timeline.get_attribute("data-window-end"))
            - int(timeline.get_attribute("data-window-start"))
            == 300000
        )
        page.locator("#playback-forward").click()
        advanced = final + timedelta(seconds=10)
        expect(page.locator("#playback-video")).to_have_attribute(
            "data-utc-start", advanced.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        )
        page.locator("#playback-speed").select_option("4")
        assert page.locator("#playback-video").evaluate("video => video.playbackRate") == 4
        page.locator('[data-timeline-action="fit"]').click()
        assert (
            int(timeline.get_attribute("data-window-end"))
            - int(timeline.get_attribute("data-window-start"))
            == 86400000
        )


def test_continuation_crosses_segments_but_stops_at_a_real_gap():
    with _history_page() as (page, expect):
        # Distinct recorder intervals need a decoder handoff at their boundary.
        at = _BEGIN + timedelta(minutes=9)
        _jump(page, at)
        expect(page.locator("#playback-video")).to_have_attribute(
            "data-utc-end",
            (_BEGIN + timedelta(minutes=10))
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
        )
        next_at = _BEGIN + timedelta(minutes=10)
        page.locator("#playback-video").dispatch_event("ended")
        expect(page.locator("#playback-video")).to_have_attribute(
            "data-utc-start", next_at.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        )
        # The last chunk of the first two hours ends at an actual ten-minute gap.
        last = _BEGIN + timedelta(hours=2) - timedelta(seconds=10)
        _jump(page, last)
        page.locator("#playback-video").dispatch_event("ended")
        expect(page.locator("#playback-message")).to_contain_text("Recording gap")
        expect(page.locator("#playback-video")).to_have_attribute(
            "data-utc-start", last.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        )
        page.locator("#playback-next").click()
        after_gap = _BEGIN + timedelta(hours=2, minutes=10)
        expect(page.locator("#playback-video")).to_have_attribute(
            "data-utc-start", after_gap.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        )
        _jump(page, _BEGIN + timedelta(hours=2, minutes=5))
        expect(page.locator("#playback-message")).to_contain_text("No playable recording")
        assert page.locator("#playback-video").get_attribute("src") is None
        page.locator("#playback-next").click()
        expect(page.locator("#playback-video")).to_have_attribute(
            "data-utc-start", after_gap.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        )


def test_event_at_last_recorded_frame_uses_preroll_without_inventing_a_gap():
    with _history_page() as (page, expect):
        event_at = _BEGIN + timedelta(hours=2) - timedelta(milliseconds=500)
        page.evaluate("timestamp => window.K5Playback.seekUTC(timestamp)", event_at.isoformat())
        requested_start = _BEGIN + timedelta(hours=2) - timedelta(seconds=1)
        expect(page.locator("#playback-video")).to_have_attribute(
            "data-utc-start",
            requested_start.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        )
        page.locator("#playback-video").dispatch_event("loadedmetadata")
        actual = page.evaluate("window.K5Playback.getUTC()")
        assert actual == int(event_at.timestamp() * 1000)
        assert "No playable recording" not in page.locator("#playback-message").inner_text()
        expect(page.locator("#playback-clock")).to_have_attribute(
            "datetime", event_at.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        )


@pytest.mark.parametrize(
    ("code", "message"),
    [
        ("export_recording_gap", "crosses a recording gap"),
        ("export_busy", "Other clips are being prepared"),
        ("export_cache_unavailable", "export storage is unavailable"),
        ("export_too_large", "exceeds 256 MB"),
    ],
)
def test_export_failure_explains_the_action_and_keeps_playback_independent(code, message):
    with _history_page(export_response=(409, {"detail": code})) as (page, expect):
        page.locator(".history-item").first.click()
        preview = page.locator("#playback-video").get_attribute("src")
        page.locator("#download-recording").click()
        expect(page.locator("#export-message")).to_contain_text(message)
        assert page.locator("#playback-video").get_attribute("src") == preview
        expect(page.locator("#download-recording")).to_have_attribute("aria-disabled", "false")
        assert page.locator("#download-recording").get_attribute("download") is None


def test_failed_preparation_never_echoes_native_or_private_error_text():
    failed = {
        "id": "ffaf1b23-e287-4fe1-b460-05c79d59f1d9",
        "status": "failed",
        "error": "rtsp://private-user:private-password@192.0.2.50/stream",
    }
    with _history_page(export_response=(202, failed)) as (page, expect):
        page.locator(".history-item").first.click()
        page.locator("#download-recording").click()
        expect(page.locator("#export-message")).to_contain_text("complete, playable MP4")
        assert "private-password" not in page.locator("#workspace").inner_text()
        assert page.locator("#download-recording").get_attribute("href") == "#"


def test_ready_export_downloads_and_a_later_expired_file_is_rechecked():
    state = {"expired": False, "job": None}
    download_response = {"body": b"controller-download-fixture", "requests": []}
    identifier = "ffaf1b23-e287-4fe1-b460-05c79d59f1d9"

    def export_handler(route):
        path = urlsplit(route.request.url).path
        if path == f"/api/v1/operator/cameras/{_CAMERA}/exports":
            selection = route.request.post_data_json
            state["job"] = {
                "id": identifier,
                "status": "ready",
                "bytes": len(b"controller-download-fixture"),
                "start": selection["start"],
                "duration": selection["duration"],
                "filename": "K5-controller-fixture.mp4",
                "download_url": f"/media/exports/{_CAMERA}/{identifier}.mp4",
            }
            route.fulfill(
                status=202, content_type="application/json", body=json.dumps(state["job"])
            )
            return True
        if path == f"/api/v1/operator/cameras/{_CAMERA}/exports/{identifier}":
            route.fulfill(
                status=404 if state["expired"] else 200,
                content_type="application/json",
                body=json.dumps(
                    {"detail": "Export not found"} if state["expired"] else state["job"]
                ),
            )
            return True
        return False

    with _history_page(export_handler=export_handler, download_response=download_response) as (
        page,
        expect,
    ):
        page.locator(".history-item").first.click()
        with page.expect_download() as download:
            page.locator("#download-recording").click()
        assert download.value.suggested_filename == "K5-controller-fixture.mp4"
        assert download.value.failure() is None
        assert len(download_response["requests"]) == 1
        state["expired"] = True
        page.locator("#download-recording").click()
        expect(page.locator("#export-message")).to_contain_text("no longer available")
        assert len(download_response["requests"]) == 1
        assert page.locator("#download-recording").get_attribute("href") == "#"
