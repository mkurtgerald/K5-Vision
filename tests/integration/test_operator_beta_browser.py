"""Opt-in, bounded two-camera qualification through the real operator browser.

The native Windows gate uses installed Edge and the real donor workers. Linux
Chromium is a useful regression only. All camera content is generated; this
cannot establish physical-camera compatibility, hardware capacity or endurance.
Playwright is a development dependency, never an installed-product requirement.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import platform
import secrets
import shutil
import subprocess
import time
from importlib.metadata import version
from pathlib import Path

import pytest

from k5vision import identity_state

_TOOLS = ("K5_MEDIAMTX_DONOR_EXE", "K5_CADDY_DONOR_EXE", "K5_DONOR_FFMPEG", "K5_DONOR_FFPROBE")
_PLAYWRIGHT_VERSION = "1.58.0"
_CAMERAS = (
    ("Generated red camera", "127.0.0.1", "red"),
    ("Generated blue camera", "127.0.0.2", "blue"),
)
pytestmark = pytest.mark.skipif(
    os.getenv("K5_OPERATOR_BROWSER_ACCEPTANCE") != "1"
    or not all(os.getenv(name) for name in _TOOLS),
    reason="Browser qualification requires explicit opt-in and supplied native development tools",
)


def _require(condition, message):
    __tracebackhide__ = True
    if not condition:
        pytest.fail(message, pytrace=False)


def _runtime_helpers():
    spec = importlib.util.spec_from_file_location(
        "k5_beta_browser_runtime_helpers",
        Path(__file__).with_name("test_operator_product_runtime.py"),
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _decoded_color(page, video_id, color):
    """Require advancing decoded frames, then inspect actual same-origin pixels."""
    __tracebackhide__ = True
    page.wait_for_function(
        """id => {
          const video = document.getElementById(id);
          return video.readyState >= 2 && !video.paused && video.videoWidth === 320 &&
            video.videoHeight === 240 && video.currentTime > 0.15 &&
            video.getVideoPlaybackQuality().totalVideoFrames >= 4;
        }""",
        arg=video_id,
        timeout=25_000,
    )
    decoded = page.evaluate(
        """id => {
          const video = document.getElementById(id);
          const canvas = document.createElement('canvas');
          canvas.width = canvas.height = 1;
          const context = canvas.getContext('2d');
          context.drawImage(video, 0, 0, 1, 1);
          return {
            frames: video.getVideoPlaybackQuality().totalVideoFrames,
            rgb: Array.from(context.getImageData(0, 0, 1, 1).data).slice(0, 3)
          };
        }""",
        video_id,
    )
    red, green, blue = decoded["rgb"]
    isolated = (
        red > 180 and green < 70 and blue < 70
        if color == "red"
        else (blue > 180 and red < 70 and green < 70)
    )
    _require(isolated, "Decoded video did not match the selected generated camera")
    return decoded["frames"]


def _assert_no_browser_storage(page):
    __tracebackhide__ = True
    storage = page.evaluate(
        """() => ({local: localStorage.length, session: sessionStorage.length,
          cookie: document.cookie, password: document.getElementById('password').value,
          source: document.getElementById('new-camera-source').value})"""
    )
    _require(
        storage == {"local": 0, "session": 0, "cookie": "", "password": "", "source": ""},
        "The operator left credentials in script-visible browser storage or setup inputs",
    )


def test_real_browser_two_camera_live_history_playback_and_logout(
    tmp_path, monkeypatch, record_testsuite_property
):
    __tracebackhide__ = True
    browser_api = pytest.importorskip("playwright.sync_api")
    _require(version("playwright") == _PLAYWRIGHT_VERSION, "Unqualified Playwright version")
    channel = os.getenv("K5_OPERATOR_BROWSER_CHANNEL", "msedge" if os.name == "nt" else "chromium")
    _require(channel in {"msedge", "chromium"}, "Unqualified browser channel")
    if os.name == "nt":
        _require(channel == "msedge", "Native Windows beta qualification requires installed Edge")
    runtime = _runtime_helpers()
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

        # Only the hosted Linux regression needs this synthetic mount-owner
        # adjustment. Windows uses every production custody and ACL check.
        monkeypatch.setattr(Path, "lstat", ordinary_system_ancestor)
    private = tmp_path / "private browser fixture with spaces"
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
    for _, _, color in _CAMERAS:
        video = private / f"generated {color} fixture.mp4"
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
                "-t",
                "12",
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
        _require(result.returncode == 0, "Generated H264 camera fixture could not be created")
        _require(helpers._decoded_frames(ffprobe, video) == 180, "Generated fixture was invalid")
        videos.append(video)
    reservations, ports = helpers._reserve_ports(8)
    upstream_ports, product_ports = ports[:2], ports[2:]
    processes, publishers = [], []
    product = None
    browser = context = anonymous = None
    started = time.monotonic()
    receipt = {
        "schema_version": "1",
        "platform": platform.system(),
        "native_windows_execution": os.name == "nt",
        "browser_channel": channel,
        "playwright_version": _PLAYWRIGHT_VERSION,
        "generated_media_only": True,
        "real_camera_acceptance": False,
        "analytics_acceptance": False,
        "long_duration_acceptance": False,
        "hardware_capacity_acceptance": False,
        "camera_count": 2,
        "width": 320,
        "height": 240,
        "frames_per_second": 15,
        "portable_ancestor_adjustment": portable_adjustment,
    }
    try:
        for listener in reservations:
            listener.close()
        sources = []
        for (_, host, color), port in zip(_CAMERAS, upstream_ports, strict=True):
            source = f"rtsp://{host}:{port}/generated"
            sources.append(source)
            config = private / f"private {color} upstream.json"
            helpers._private_write(
                config,
                json.dumps(
                    {
                        "logLevel": "warn",
                        "rtspAddress": f"{host}:{port}",
                        "rtspTransports": ["tcp"],
                        "authInternalUsers": [
                            {
                                "user": "any",
                                "ips": ["127.0.0.0/8", "::1"],
                                "permissions": [
                                    {"action": action, "path": "generated"}
                                    for action in ("publish", "read")
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
                        "paths": {"generated": {"source": "publisher"}},
                    }
                ),
            )
            processes.append(helpers._start([str(media), str(config)], private))
        product = runtime._Product(
            identity, identity.directory / "recordings", media, caddy, product_ports
        )
        client = runtime._Client(product_ports[0])
        runtime._wait(
            lambda: product.server.started and client.get("/operator/")[0] == 200,
            label="Browser product startup",
            product=product,
        )
        _require(
            client.get("/api/v1/operator/status")[0] == 401,
            "Anonymous requests received operator status",
        )
        with browser_api.sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                channel=channel, headless=True, args=["--autoplay-policy=no-user-gesture-required"]
            )
            receipt["browser_version"] = browser.version
            context = browser.new_context(viewport={"width": 1440, "height": 960})
            anonymous = browser.new_context()
            page = context.new_page()
            page.set_default_timeout(10_000)
            script_errors = []
            page.on("pageerror", lambda _error: script_errors.append(True))
            page.goto(client.origin + "/operator/", wait_until="domcontentloaded")
            page.locator("#username").fill(username)
            page.locator("#password").fill(password)
            with page.expect_response(
                lambda response: response.url.endswith("/api/v1/auth/login")
            ) as login:
                page.locator("#sign-in-button").click()
            _require(login.value.status == 200, "Browser sign-in failed")
            client.token = login.value.json()["session_token"]
            page.locator("#workspace").wait_for(state="visible")
            receipt["ui_login"] = True
            page.wait_for_function("""() => {
                const status = document.getElementById('service-status').textContent;
                const storage = document.getElementById('storage-status').textContent;
                return status === 'Media service ready' && storage.includes('recording limit') &&
                  storage.includes('disk free');
            }""")
            diagnostics = client.json("/api/v1/operator/status")
            _require(
                set(diagnostics)
                == {"worker_status", "max_recording_bytes", "storage", "analytics_status"}
                and diagnostics["worker_status"] == "ready"
                and diagnostics["analytics_status"] == "disabled"
                and diagnostics["max_recording_bytes"] == 20 * 1024**3
                and diagnostics["storage"]["recording_allowed"] is True,
                "Operator service and storage status was incomplete",
            )
            page.locator("#connection-help-button").click()
            page.locator("#connection-help-dialog").wait_for(state="visible")
            _require(
                "media service is ready"
                in page.locator("#connection-help-status").inner_text().lower(),
                "Connection help did not reflect actual service readiness",
            )
            page.locator("#done-connection-help").click()
            receipt["ui_service_storage_status"] = True
            camera_ids = []
            for (name, host, _), source in zip(_CAMERAS, sources, strict=True):
                page.locator("#add-camera-button").click()
                page.locator("#new-camera-name").fill(name)
                page.locator("#new-camera-host").fill(host)
                page.locator("#new-camera-source").fill(source)
                with page.expect_response(
                    lambda response: (
                        response.request.method == "POST"
                        and response.url.endswith("/api/v1/operator/cameras")
                    )
                ) as enroll:
                    page.locator("#save-camera").click()
                _require(enroll.value.status == 201, "Browser camera enrollment failed")
                camera = enroll.value.json()
                _require(
                    "source_uri" not in camera and source not in json.dumps(camera),
                    "Camera API exposed execution-only source material",
                )
                camera_ids.append(camera["device_id"])
                page.locator("#add-camera-dialog").wait_for(state="hidden")
            _require(len(set(camera_ids)) == 2, "Distinct generated cameras were merged")
            receipt["ui_two_camera_enrollment"] = True
            for video, source in zip(videos, sources, strict=True):
                publisher = helpers._start(
                    [
                        str(ffmpeg),
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-re",
                        "-stream_loop",
                        "-1",
                        "-i",
                        str(video),
                        "-t",
                        "90",
                        "-c",
                        "copy",
                        "-f",
                        "rtsp",
                        "-rtsp_transport",
                        "tcp",
                        source,
                    ],
                    private,
                )
                processes.append(publisher)
                publishers.append(publisher)

            def both_live():
                views = client.json("/api/v1/operator/cameras")["cameras"]
                return len(views) == 2 and all(
                    v["status"] == "live" and v["recording"] for v in views
                )

            runtime._wait(
                both_live, label="Concurrent two-camera recording", product=product, timeout=20
            )
            receipt["simultaneous_recording"] = True
            live_frames, live_urls = [], []
            # Reverse order and switch repeatedly to detect stale media selection.
            for index in (1, 0, 1):
                name, _, color = _CAMERAS[index]
                media_route = f"/api/v1/operator/cameras/{camera_ids[index]}/media-session"
                with page.expect_response(
                    lambda response, route=media_route: response.url.endswith(route)
                ) as media_session:
                    page.locator(".camera-item").filter(has_text=name).click()
                _require(media_session.value.status == 200, "Camera media session failed")
                live_url = media_session.value.json()["live_url"]
                live_urls.append(live_url)
                live_frames.append(_decoded_color(page, "live-video", color))
                _require(
                    anonymous.request.get(client.origin + live_url).status == 401,
                    "An anonymous browser received camera media",
                )
                _assert_no_browser_storage(page)
            receipt["live_decoded_frames"] = live_frames
            receipt["camera_switch_pixel_isolation"] = True
            receipt["anonymous_media_denied"] = True
            helpers._stop_owned(publishers[0])

            def first_outage_only():
                views = client.json("/api/v1/operator/cameras")["cameras"]
                states = {v["device_id"]: v for v in views}
                return states[camera_ids[0]]["status"] == "disconnected" and (
                    states[camera_ids[1]]["status"] == "live" and states[camera_ids[1]]["recording"]
                )

            runtime._wait(first_outage_only, label="Independent camera outage", product=product)
            receipt["one_camera_outage_isolated"] = True
            helpers._stop_owned(publishers[1])
            runtime._wait(
                lambda: all(
                    v["status"] == "disconnected"
                    for v in client.json("/api/v1/operator/cameras")["cameras"]
                ),
                label="Recording finalization",
                product=product,
            )
            playback_frames, playback_urls = [], []
            for index, (name, _, color) in enumerate(_CAMERAS):
                page.locator(".camera-item").filter(has_text=name).click()
                if index == 0:
                    page.locator("#history-tab").click()
                page.locator(".history-item").first.wait_for(state="visible", timeout=15_000)
                page.locator(".history-item").first.click()
                page.locator("#play-recording").click()
                playback_frames.append(_decoded_color(page, "playback-video", color))
                playback_url = page.locator("#download-recording").get_attribute("href")
                _require(
                    playback_url and playback_url.startswith("/media/playback?"),
                    "Recording download left the admitted media gateway",
                )
                playback_urls.append(playback_url)
                _require(
                    anonymous.request.get(client.origin + playback_url).status == 401,
                    "An anonymous browser received a recording",
                )
                _require(
                    page.locator(".history-gap").count() >= 1,
                    "Recording history failed to expose unavailable time",
                )
                _assert_no_browser_storage(page)
            receipt["playback_decoded_frames"] = playback_frames
            receipt["recording_pixel_isolation"] = True
            receipt["ui_history_and_gaps"] = True
            receipt["script_visible_credentials_absent"] = True
            recording_root = identity.directory / "recordings"
            saved_files = sorted(recording_root.rglob("*.mp4"))
            _require(len(saved_files) >= 2, "Two-camera recording files were absent")

            def digests():
                results = []
                for path in saved_files:
                    with path.open("rb") as source:
                        results.append(hashlib.file_digest(source, "sha256").hexdigest())
                return results

            saved_digests = digests()
            saved_history = client.json(f"/api/v1/operator/cameras/{camera_ids[1]}/history")[
                "spans"
            ]
            page.locator("#remove-camera-button").click()
            page.locator("#remove-camera-dialog").wait_for(state="visible")
            _require(
                _CAMERAS[1][0] in page.locator("#remove-camera-name").inner_text(),
                "Removal confirmation named the wrong camera",
            )
            page.locator("#cancel-remove-camera").click()
            page.locator("#remove-camera-dialog").wait_for(state="hidden")
            _require(
                len(client.json("/api/v1/operator/cameras")["cameras"]) == 2,
                "Cancelling removal changed camera enrollment",
            )
            receipt["ui_removal_cancel_preserved_camera"] = True
            page.locator("#remove-camera-button").click()
            removal_route = f"/api/v1/operator/cameras/{camera_ids[1]}"
            with page.expect_response(
                lambda response: (
                    response.request.method == "DELETE" and response.url.endswith(removal_route)
                )
            ) as removed:
                page.locator("#confirm-remove-camera").click()
            _require(removed.value.status == 204, "Confirmed camera removal failed")
            page.locator("#remove-camera-dialog").wait_for(state="hidden")
            page.wait_for_function(
                "() => document.getElementById('camera-count').textContent === '1'"
            )
            _require(
                client.get(removal_route + "/history")[0] == 404,
                "Removed camera history remained exposed in the workspace",
            )
            for url in (live_urls[0], playback_urls[1]):
                _require(
                    context.request.get(client.origin + url).status == 401,
                    "Removed camera media remained authorized",
                )
            _require(
                sorted(recording_root.rglob("*.mp4")) == saved_files and digests() == saved_digests,
                "Camera removal changed retained recording files",
            )
            _require(
                context.request.get(client.origin + playback_urls[0]).status == 200,
                "Removing one camera denied the other camera's retained recording",
            )
            receipt["ui_removal_preserved_files_and_denied_media"] = True
            page.locator("#add-camera-button").click()
            page.locator("#new-camera-name").fill(_CAMERAS[1][0])
            page.locator("#new-camera-host").fill(_CAMERAS[1][1])
            page.locator("#new-camera-source").fill(sources[1])
            with page.expect_response(
                lambda response: (
                    response.request.method == "POST"
                    and response.url.endswith("/api/v1/operator/cameras")
                )
            ) as restored:
                page.locator("#save-camera").click()
            _require(
                restored.value.status == 201
                and restored.value.json()["device_id"] == camera_ids[1],
                "Re-enrolling a retained camera changed its durable identity",
            )
            page.locator("#add-camera-dialog").wait_for(state="hidden")
            _require(
                client.json(removal_route + "/history")["spans"] == saved_history,
                "Re-enrolling a camera did not restore its retained history",
            )
            page.wait_for_function(
                "name => document.getElementById('camera-title').textContent === name",
                arg=_CAMERAS[1][0],
            )
            page.locator(".history-item").first.wait_for(state="visible", timeout=15_000)
            page.locator(".history-item").first.click()
            page.locator("#play-recording").click()
            _decoded_color(page, "playback-video", "blue")
            receipt["ui_reenrollment_restored_history"] = True
            page.locator("#sign-out-button").click()
            page.locator("#sign-in").wait_for(state="visible")
            for url in (*live_urls, *playback_urls):
                _require(
                    context.request.get(client.origin + url).status == 401,
                    "Sign-out left cached camera media authorized",
                )
            _require(
                client.get("/api/v1/operator/cameras")[0] == 401,
                "Sign-out did not revoke the browser human session",
            )
            _require(
                page.locator("#live-video").get_attribute("src") is None
                and page.locator("#playback-video").get_attribute("src") is None,
                "Sign-out did not reset visible media",
            )
            _assert_no_browser_storage(page)
            _require(not script_errors, "Operator browser raised an uncaught script error")
            receipt["logout_revoked_media_and_session"] = True
            receipt["browser_script_errors"] = 0
            anonymous.close()
            anonymous = None
            context.close()
            context = None
            browser.close()
            browser = None
    finally:
        cleanup_failed = False
        for resource in (anonymous, context, browser):
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    cleanup_failed = True
        if product is not None:
            try:
                product.stop()
            except Exception:
                cleanup_failed = True
        for process in reversed(processes):
            try:
                helpers._stop_owned(process)
            except Exception:
                cleanup_failed = True
        for listener in reservations:
            listener.close()
        _require(not cleanup_failed, "Owned browser, product or generated fixture cleanup failed")
    elapsed = time.monotonic() - started
    _require(elapsed < 180, "Browser qualification exceeded its bounded three-minute duration")
    receipt["duration_seconds"] = round(elapsed, 2)
    receipt["owned_cleanup"] = True
    record_testsuite_property("operator_beta_browser_receipt", json.dumps(receipt, sort_keys=True))
