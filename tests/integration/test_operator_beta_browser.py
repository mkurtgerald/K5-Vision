"""Opt-in, bounded four-camera qualification through the real operator browser.

The native Windows gate uses installed Edge and the real donor workers. Linux
Chromium is a useful regression only. All camera content is generated; this
cannot establish physical-camera compatibility, hardware capacity or endurance.
Playwright is a development dependency, never an installed-product requirement.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
import platform
import re
import secrets
import shutil
import socket
import subprocess
import time
from datetime import UTC, datetime, timedelta
from importlib.metadata import version
from pathlib import Path
from urllib.parse import quote, urlsplit

import pytest

from k5vision import identity_state

_TOOLS = ("K5_MEDIAMTX_DONOR_EXE", "K5_CADDY_DONOR_EXE", "K5_DONOR_FFMPEG", "K5_DONOR_FFPROBE")
_PLAYWRIGHT_VERSION = "1.58.0"
_CAMERAS = (
    ("Generated red camera", "127.0.0.1", "red"),
    ("Generated blue camera", "127.0.0.2", "blue"),
    ("Generated green camera", "127.0.0.1", "lime"),
    ("Generated yellow camera", "127.0.0.2", "yellow"),
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


def _script_error_code(error):
    """Classify script faults without retaining private exception text."""
    text = str(getattr(error, "message", ""))
    for marker, code in (
        ("AbortSignal.any", "abort_signal_any"),
        ("AbortSignal.timeout", "abort_signal_timeout"),
        ("showModal", "dialog_api"),
        ("ReferenceError", "reference_error"),
        ("TypeError", "type_error"),
        ("SyntaxError", "syntax_error"),
    ):
        if marker in text:
            return code
    return "script_error"


def _request_category(request):
    """Reduce a private request URL to a finite, source-free route category."""
    path = urlsplit(request.url).path
    if path == "/api/v1/auth/login":
        category = "login"
    elif path == "/api/v1/operator/status":
        category = "status"
    elif path.endswith("/media-session"):
        category = "media_session"
    elif path.startswith("/api/v1/operator/cameras"):
        category = "camera"
    elif path.startswith("/media/live/"):
        category = "live"
    elif path == "/media/playback":
        category = "playback"
    else:
        category = "other"
    return category


def _request_failure_code(request):
    """Retain a route category and finite network code, never request URLs."""
    category = _request_category(request)
    detail = request.failure or ""
    for marker, code in (
        ("ERR_CONNECTION_REFUSED", "refused"),
        ("ERR_CONNECTION_RESET", "reset"),
        ("ERR_TIMED_OUT", "timeout"),
        ("ERR_ABORTED", "aborted"),
        ("ERR_FAILED", "failed"),
    ):
        if marker in detail:
            return category + "_" + code
    return category + "_unavailable"


def _failure_code(error):
    kind = type(error).__name__
    return {
        "TimeoutError": "browser_timeout",
        "Failed": "acceptance_assertion",
        "AssertionError": "fixture_assertion",
        "RuntimeError": "fixture_runtime",
        "Error": "browser_error",
    }.get(kind, "qualification_error")


def _runtime_helpers():
    spec = importlib.util.spec_from_file_location(
        "k5_beta_browser_runtime_helpers",
        Path(__file__).with_name("test_operator_product_runtime.py"),
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _onvif_helpers():
    spec = importlib.util.spec_from_file_location(
        "k5_generated_onvif_fixture", Path(__file__).with_name("onvif_camera_fixture.py")
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _onvif_clock_proof(fixture):
    """Require actual anonymous time read and accepted skewed WSSE timestamps."""
    proof = fixture.clock_skew_evidence()
    offset = proof.get("onvif_clock_offset_seconds")
    reads = proof.get("onvif_time_read_count")
    _require(
        proof.get("onvif_clock_skew_compensated") is True
        and type(offset) in (int, float)
        and math.isfinite(offset)
        and 600 < abs(offset) < 86400
        and type(reads) is int
        and 1 <= reads <= 128,
        "Actual ONVIF camera clock compensation was not proven",
    )
    return proof


def _assert_onvif_time_health(page, fixture, clock):
    """Check an actual ONVIF sample and its visible, deliberately limited claim."""
    __tracebackhide__ = True
    _require(
        type(clock) is dict
        and set(clock)
        == {
            "status",
            "offset_seconds",
            "measured_at",
            "uncertainty_seconds",
            "reported_mode",
            "sync_verified",
        },
        "ONVIF time response left the bounded six-field contract",
    )
    offset, uncertainty, measured = (
        clock["offset_seconds"],
        clock["uncertainty_seconds"],
        clock["measured_at"],
    )
    _require(
        clock["status"] == "measured"
        and type(offset) in (int, float)
        and math.isfinite(offset)
        and type(uncertainty) in (int, float)
        and math.isfinite(uncertainty)
        and 0 <= uncertainty <= 15
        and type(measured) is str
        and measured.endswith("Z")
        and clock["reported_mode"] == fixture.clock_mode.lower()
        and clock["sync_verified"] is False,
        "ONVIF time response did not preserve the observed offset and unverified mode",
    )
    sampled = datetime.fromisoformat(measured.replace("Z", "+00:00"))
    _require(
        sampled.utcoffset().total_seconds() == 0
        and abs((datetime.now(UTC) - sampled).total_seconds()) < 30
        and abs(offset - fixture.clock_skew_seconds) <= uncertainty + 2,
        "ONVIF clock sample was not a fresh measurement of the generated camera",
    )
    page.locator("#camera-clock-status").wait_for(state="visible")
    displayed = page.locator("#camera-clock-status").inner_text()
    if abs(fixture.clock_skew_seconds) > 5:
        direction = "ahead of" if fixture.clock_skew_seconds > 0 else "behind"
        rendered_offset = re.search(
            r"Camera is ([0-9]+(?:\.[0-9]+)?) seconds (ahead of|behind) the recorder", displayed
        )
        _require(
            rendered_offset is not None
            and rendered_offset.group(2) == direction
            and abs(float(rendered_offset.group(1)) - abs(offset)) <= 0.11,
            "Camera clock offset direction was absent from the actual setup view",
        )
    _require(
        "Time synchronization is not verified" in displayed
        and (fixture.clock_mode != "NTP" or "Camera reports NTP mode" in displayed),
        "Camera setup presented an unverified NTP report as synchronization proof",
    )


def _assert_recorder_time_health(page, client):
    """Compare the visible recorder UTC with its real server response, not browser Date."""
    __tracebackhide__ = True
    diagnostics = client.json("/api/v1/operator/status")
    clock, server_utc = diagnostics.get("clock"), diagnostics.get("server_utc")
    _require(
        type(server_utc) is str
        and server_utc.endswith("Z")
        and type(clock) is dict
        and clock.get("status") == "observing"
        and clock.get("sync_verified") is False,
        "Recorder status did not expose observed UTC without a synchronization claim",
    )
    server = datetime.fromisoformat(server_utc.replace("Z", "+00:00"))
    page.wait_for_function("""() => {
      const value = document.getElementById('recorder-time-status').textContent;
      return value.startsWith('Recorder time: ') && value.includes(' UTC') &&
        value.includes('Time synchronization is not verified');
    }""")
    displayed = page.locator("#recorder-time-status").inner_text()
    timestamp = displayed.removeprefix("Recorder time: ").split(" UTC", 1)[0]
    parsed = datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    _require(
        abs((parsed - server).total_seconds()) < 15
        and page.locator("#recorder-time-warning").is_hidden(),
        "Recorder display followed client time or showed an unobserved host-clock change",
    )
    return diagnostics


def _onvif_enroll(page, *, name, fixture, reject_wrong=False, run_discovery=False):
    """Use actual credential/profile controls without entering a stream address."""
    __tracebackhide__ = True
    source_requests = []

    def observe(request):
        material = request.url + (request.post_data or "")
        if "rtsp://" in material.lower() or '"source_uri"' in material:
            source_requests.append(True)

    page.on("request", observe)
    discovery_completed = False
    try:
        page.locator("#add-camera-button").click()
        _require(page.locator("#new-camera-source").count() == 0, "Manual stream field remained")
        if run_discovery:
            with page.expect_response(
                lambda response: (
                    response.request.method == "POST"
                    and response.url.endswith("/api/v1/operator/camera-discovery")
                ),
                timeout=25_000,
            ) as discovery:
                page.locator("#find-cameras").click()
            _require(discovery.value.status == 200, "Bounded camera discovery did not complete")
            _require("rtsp://" not in discovery.value.text().lower(), "Discovery exposed a stream")
            discovery_completed = True
        page.locator("#new-camera-name").fill(name)
        page.locator("#new-camera-host").fill(fixture.host)
        page.locator("#new-camera-port").fill(str(fixture.port))
        page.locator("#new-camera-username").fill(fixture.username)
        if reject_wrong:
            page.locator("#new-camera-password").fill("deliberately-wrong-fixture-password")
            with page.expect_response(
                lambda response: (
                    response.request.method == "POST"
                    and response.url.endswith("/api/v1/operator/camera-profiles")
                )
            ) as rejected:
                page.locator("#test-camera-connection").click()
            _require(
                rejected.value.status in {400, 401, 403, 422, 502, 503},
                "Wrong camera credentials were admitted",
            )
            _require(fixture.rejected_credentials(), "Camera did not reject the actual SOAP digest")
            _require(
                page.locator("#save-camera").is_disabled(), "Wrong credentials enabled Connect"
            )
            page.wait_for_function(
                "() => document.getElementById('new-camera-password').value === ''"
            )
        page.locator("#new-camera-password").fill(fixture.password)
        with page.expect_response(
            lambda response: (
                response.request.method == "POST"
                and response.url.endswith("/api/v1/operator/camera-profiles")
            )
        ) as profiles:
            page.locator("#test-camera-connection").click()
        _require(profiles.value.status == 200, "Actual ONVIF credential/profile request failed")
        profile_payload = profiles.value.json()
        _require(
            set(profile_payload) == {"probe_id", "expires_in_seconds", "profiles", "clock"},
            "Profile response left the bounded public contract",
        )
        _require(
            all(
                forbidden not in profiles.value.text()
                for forbidden in ("rtsp://", fixture.username, fixture.password)
            ),
            "Profile response exposed private camera execution material",
        )
        selected = [
            item
            for item in profile_payload["profiles"]
            if item.get("codec") == "H264" and item.get("supported") is True
        ]
        _require(bool(selected), "Generated H264 camera profile was not offered")
        page.locator("#camera-profile").select_option(selected[0]["id"])
        _assert_onvif_time_health(page, fixture, profile_payload["clock"])
        _require(
            page.locator("#add-camera-message").inner_text()
            == (
                "Camera account accepted. Choose an H.264 stream; "
                "video is checked after connecting."
            ),
            "ONVIF profile acceptance claimed unverified video readiness",
        )
        page.wait_for_function("() => document.getElementById('new-camera-password').value === ''")
        with page.expect_response(
            lambda response: (
                response.request.method == "POST"
                and response.url.endswith("/api/v1/operator/cameras/connect")
            )
        ) as connected:
            page.locator("#save-camera").click()
        _require(connected.value.status == 201, "Profile-based camera connection failed")
        camera = connected.value.json()
        _require(
            all(
                forbidden not in connected.value.text()
                for forbidden in ("rtsp://", fixture.username, fixture.password, '"source_uri"')
            ),
            "Connection response exposed private camera execution material",
        )
        page.locator("#add-camera-dialog").wait_for(state="hidden")
        _require(
            fixture.qualified_requests(), "Actual authenticated ONVIF SOAP flow was incomplete"
        )
        _require(not source_requests, "Browser sent a manual camera stream address")
        _require("rtsp://" not in page.content().lower(), "Camera stream address entered the DOM")
        return camera, discovery_completed
    finally:
        page.remove_listener("request", observe)


def _decoded_color(page, video_id, color):
    """Require advancing decoded frames, then inspect actual same-origin pixels."""
    return _decoded_video(page, "#" + video_id, color)


def _decoded_video(page, selector, color):
    """Check a particular visible video, including independent camera-wall tiles."""
    __tracebackhide__ = True
    page.wait_for_function(
        """selector => {
          const video = document.querySelector(selector);
          return video && video.getBoundingClientRect().width > 0 &&
            video.readyState >= 2 && !video.paused && video.videoWidth === 320 &&
            video.videoHeight === 240 && video.currentTime > 0.15 &&
            video.getVideoPlaybackQuality().totalVideoFrames >= 4;
        }""",
        arg=selector,
        timeout=25_000,
    )
    decoded = page.evaluate(
        """selector => {
          const video = document.querySelector(selector);
          const canvas = document.createElement('canvas');
          canvas.width = canvas.height = 1;
          const context = canvas.getContext('2d');
          context.drawImage(video, 0, 0, 1, 1);
          return {
            frames: video.getVideoPlaybackQuality().totalVideoFrames,
            rgb: Array.from(context.getImageData(0, 0, 1, 1).data).slice(0, 3)
          };
        }""",
        selector,
    )
    red, green, blue = decoded["rgb"]
    isolated = {
        "red": red > 180 and green < 70 and blue < 70,
        "blue": blue > 180 and red < 70 and green < 70,
        "lime": green > 180 and red < 70 and blue < 70,
        "yellow": red > 180 and green > 180 and blue < 70,
    }.get(color, False)
    _require(isolated, "Decoded video did not match the selected generated camera")
    return decoded["frames"]


def _open_camera_watch(page, device_id):
    page.locator("#wall-tab").click()
    tile = page.locator(f'#wall-panel [data-camera-id="{device_id}"]')
    tile.locator("[data-wall-action='watch']").click()
    page.locator("#live-panel").wait_for(state="visible")


def _open_camera_recordings(page, device_id):
    page.locator("#wall-tab").click()
    tile = page.locator(f'#wall-panel [data-camera-id="{device_id}"]')
    tile.locator("[data-wall-action='recordings']").click()
    page.locator("#history-panel").wait_for(state="visible")


def _wall_frames(page, device_ids, colors, *, layout=None):
    """Require all selected wall tiles visible and advancing concurrently."""
    __tracebackhide__ = True
    _require(len(device_ids) == len(colors) and 1 <= len(device_ids) <= 9, "Invalid wall proof")
    page.locator("#wall-tab").click()
    layout = layout or ("4" if len(device_ids) <= 4 else "9")
    if page.locator("#wall-layout").input_value() != layout:
        page.locator("#wall-layout").select_option(layout)
    selectors = [f'#wall-panel [data-camera-id="{device}"] video' for device in device_ids]
    if layout == "4" and len(device_ids) == 4:
        page.evaluate("""() => {
          window.scrollTo(0,0);
          document.querySelector('.main-content').scrollTop=0;
        }""")
        page.wait_for_function(
            """selectors => selectors.every(selector => {
              const video=document.querySelector(selector);
              if(!video) return false;
              const bounds=video.getBoundingClientRect();
              return bounds.width>0 && bounds.height>0 && bounds.top>=-1 && bounds.left>=-1 &&
                bounds.bottom<=innerHeight+1 && bounds.right<=innerWidth+1;
            })""",
            arg=selectors,
            timeout=15_000,
        )
    before = [
        _decoded_video(page, selector, color)
        for selector, color in zip(selectors, colors, strict=True)
    ]
    page.wait_for_function(
        """({selectors,before}) => selectors.every((selector,index) => {
          const video = document.querySelector(selector);
          return video && video.getBoundingClientRect().width > 0 && !video.paused &&
            video.readyState >= 2 &&
            video.getVideoPlaybackQuality().totalVideoFrames >= before[index]+4;
        })""",
        arg={"selectors": selectors, "before": before},
        timeout=20_000,
    )
    return [
        _decoded_video(page, selector, color)
        for selector, color in zip(selectors, colors, strict=True)
    ]


def _wall_layout_proof(page, device_ids, colors):
    """Exercise real 1/4/9 layouts without claiming nine generated cameras."""
    page.locator("#wall-tab").click()
    page.locator("#wall-layout").select_option("1")
    page.wait_for_function("() => document.querySelectorAll('#wall-panel video').length===1")
    visible_id = page.locator("#wall-panel [data-camera-id]").get_attribute("data-camera-id")
    by_id = dict(zip(device_ids, colors, strict=True))
    _require(visible_id in by_id, "Single-camera wall selected an unknown source")
    _decoded_video(page, f'#wall-panel [data-camera-id="{visible_id}"] video', by_id[visible_id])
    page.locator("#wall-layout").select_option("9")
    page.wait_for_function("() => document.querySelectorAll('#wall-panel video').length===4")
    _wall_frames(page, device_ids, colors, layout="9")
    _wall_frames(page, device_ids, colors, layout="4")
    return [1, 4, 9]


def _timeline_play(page, color):
    """Seek via actual timeline pointer coordinates, then decode selected video."""
    __tracebackhide__ = True
    available = page.locator("#recording-timeline .k5-timeline-recording").first
    available.wait_for(state="attached", timeout=15_000)
    first = float(available.get_attribute("data-start"))
    last = float(available.get_attribute("data-end"))
    _require(last - first >= 2000, "Timeline fixture has no playable two-second interval")
    # Narrow the actual UTC form to make a seconds-long fixture selectable by
    # a real pointer instead of seeking a sub-pixel range in a 24-hour window.
    start = datetime.fromtimestamp(first / 1000, UTC) - timedelta(seconds=2)
    end = datetime.fromtimestamp(last / 1000, UTC) + timedelta(seconds=2)
    page.locator("#history-start").fill(start.strftime("%Y-%m-%dT%H:%M:%S"))
    page.locator("#history-end").fill(end.strftime("%Y-%m-%dT%H:%M:%S"))
    with page.expect_response(lambda response: "/history?" in response.url) as loaded:
        page.locator("#load-history").click()
    _require(loaded.value.status == 200, "Timeline UTC window could not be loaded")
    slider = page.locator("#recording-timeline [role='slider']")
    slider.wait_for(state="visible")
    page.wait_for_function(
        """({first,last}) => {
          const slider=document.querySelector('#recording-timeline [role=slider]');
          return Number(slider?.getAttribute('aria-valuemin'))===first &&
            Number(slider?.getAttribute('aria-valuemax'))===last;
        }""",
        arg={
            "first": int(start.replace(microsecond=0).timestamp() * 1000),
            "last": int(end.replace(microsecond=0).timestamp() * 1000),
        },
    )
    minimum = float(slider.get_attribute("aria-valuemin"))
    maximum = float(slider.get_attribute("aria-valuemax"))
    chosen = first + min(1000, (last - first) / 4)
    bounds = slider.bounding_box()
    _require(bounds and maximum > minimum, "Actual timeline track is unavailable")
    slider.click(
        position={
            "x": (chosen - minimum) / (maximum - minimum) * bounds["width"],
            "y": bounds["height"] / 2,
        }
    )
    page.wait_for_function(
        """chosen => {
          const input=document.getElementById('playback-start');
          return !input.disabled && Math.abs(Date.parse(input.value+'Z')-chosen)<300;
        }""",
        arg=chosen,
    )
    page.locator("#playback-duration").fill("2")
    page.locator("#play-recording").click()
    return _decoded_color(page, "playback-video", color)


def _download_recording(page, ffprobe, export_path, helpers, color):
    """Save the actual browser download and independently decode its MP4 file."""
    __tracebackhide__ = True
    page.locator("#playback-duration").fill("2")
    with page.expect_download(timeout=20_000) as pending:
        page.locator("#download-recording").click()
    download = pending.value
    _require(download.failure() is None, "Actual recording download failed")
    download.save_as(export_path)
    _require(0 < export_path.stat().st_size <= 16 * 1024**2, "Recording export exceeded its bound")
    frames = helpers._decoded_frames(ffprobe, export_path)
    ffmpeg = helpers._tool("K5_DONOR_FFMPEG")
    pixel = subprocess.run(
        [
            str(ffmpeg),
            "-v",
            "error",
            "-i",
            str(export_path),
            "-vf",
            "scale=1:1",
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "pipe:1",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=10,
        check=False,
    )
    _require(pixel.returncode == 0 and len(pixel.stdout) == 3, "Saved export pixel decode failed")
    red, green, blue = pixel.stdout
    _require(
        {
            "red": red > 180 and green < 70 and blue < 70,
            "blue": blue > 180 and red < 70 and green < 70,
            "lime": green > 180 and red < 70 and blue < 70,
            "yellow": red > 180 and green > 180 and blue < 70,
        }.get(color, False),
        "Saved export did not match the selected camera",
    )
    return frames


def _camera_recording_bytes(product, device_ids):
    paths = {
        str(source.binding.device_id): source.binding.path_name
        for source in product.service._sources
    }
    return [
        sum(
            path.stat().st_size
            for path in (product.service.recording_root / paths[device]).glob("*.mp4")
        )
        for device in device_ids
    ]


def _publish_generated(helpers, ffmpeg, video, source, private):
    return helpers._start(
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
            "180",
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


def _credential_uri(uri, username, password):
    source = urlsplit(uri)
    _require(
        source.scheme == "rtsp" and source.username is None, "Generated RTSP source was refused"
    )
    return source._replace(
        netloc=quote(username, safe="") + ":" + quote(password, safe="") + "@" + source.netloc
    ).geturl()


def _assert_rtsp_read_auth(ffprobe, anonymous_uri, camera_username, password):
    """Exercise native RTSP read authentication without retaining private stderr."""
    __tracebackhide__ = True

    def probe(uri):
        __tracebackhide__ = True
        try:
            result = subprocess.run(
                [
                    str(ffprobe),
                    "-v",
                    "error",
                    "-rtsp_transport",
                    "tcp",
                    "-analyzeduration",
                    "1000000",
                    "-probesize",
                    "1000000",
                    "-select_streams",
                    "v:0",
                    "-show_entries",
                    "stream=codec_name,width,height",
                    "-of",
                    "json",
                    uri,
                ],
                capture_output=True,
                timeout=8,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError):
            raise RuntimeError(
                "Bounded generated RTSP authentication probe did not finish"
            ) from None
        _require(
            len(result.stdout) <= 4096 and len(result.stderr) <= 16384,
            "Generated RTSP authentication probe exceeded its output bound",
        )
        return result

    for uri in (
        anonymous_uri,
        _credential_uri(anonymous_uri, camera_username, secrets.token_urlsafe(24)),
    ):
        refused = probe(uri)
        _require(
            refused.returncode != 0 and b"401" in refused.stderr,
            "Generated camera admitted an unauthorized RTSP reader",
        )
    accepted = probe(_credential_uri(anonymous_uri, camera_username, password))
    _require(accepted.returncode == 0, "Generated camera refused its valid RTSP read account")
    streams = json.loads(accepted.stdout).get("streams")
    _require(
        isinstance(streams, list)
        and len(streams) == 1
        and (streams[0].get("codec_name"), streams[0].get("width"), streams[0].get("height"))
        == ("h264", 320, 240),
        "Authenticated camera read did not expose the generated H264 video",
    )
    return {
        "authenticated_camera_read": True,
        "rtsp_anonymous_read_rejected": True,
        "rtsp_wrong_password_rejected": True,
    }


def _toggle_camera_recording(page, camera_id, enabled):
    __tracebackhide__ = True
    control = page.locator("#toggle-recording")
    route = "/api/v1/operator/cameras/" + camera_id + "/recording"
    with page.expect_response(
        lambda response: response.request.method == "PUT" and response.url.endswith(route)
    ) as changed:
        control.click()
    _require(changed.value.status == 200, "Camera recording control did not save its choice")
    page.wait_for_function(
        """enabled => {
          const button=document.getElementById('toggle-recording');
          return !button.disabled && button.dataset.enabled===String(enabled) &&
            button.getAttribute('aria-pressed')===String(enabled);
        }""",
        arg=enabled,
    )


def _assert_no_browser_storage(page):
    __tracebackhide__ = True
    storage = page.evaluate(
        """() => ({local: localStorage.length, session: sessionStorage.length,
          cookie: document.cookie, password: document.getElementById('password').value,
          cameraUsername: document.getElementById('new-camera-username').value,
          cameraPassword: document.getElementById('new-camera-password').value})"""
    )
    _require(
        storage
        == {
            "local": 0,
            "session": 0,
            "cookie": "",
            "password": "",
            "cameraUsername": "",
            "cameraPassword": "",
        },
        "The operator left credentials in script-visible browser storage or setup inputs",
    )


def test_camera_setup_preserves_next_dialog_during_previous_media_completion():
    """Delay only the previous selection to reproduce the real enrollment race.

    This browser behavior regression uses intercepted responses. It supplements
    the independent real four-camera fixture; it is not native media acceptance.
    """
    __tracebackhide__ = True
    browser_api = pytest.importorskip("playwright.sync_api")
    channel = os.getenv("K5_OPERATOR_BROWSER_CHANNEL", "msedge" if os.name == "nt" else "chromium")
    assets = Path(__file__).parents[2] / "src" / "k5vision" / "operator_web"
    cameras, submitted, pending, probes = [], [], [], {}
    account = {"username": "fixture-admin", "display_name": "Fixture", "role": "administrator"}
    hosts = ("127.0.0.1", "127.0.0.2")
    identifiers = ("8643f7a8-4a09-4ec0-bf35-15fd728cf001", "8643f7a8-4a09-4ec0-bf35-15fd728cf002")
    playwright = browser = context = None
    failed = False
    try:
        playwright = browser_api.sync_playwright().start()
        browser = playwright.chromium.launch(channel=channel, headless=True)
        context = browser.new_context()
        page = context.new_page()
        page.set_default_timeout(10_000)

        def intercept(route):
            path = urlsplit(route.request.url).path
            data = {}
            status = 200
            if path.startswith("/operator/"):
                name = path.rsplit("/", 1)[1] or "index.html"
                content_type = {
                    "index.html": "text/html",
                    "app.js": "application/javascript",
                    "app.css": "text/css",
                    "wall.js": "application/javascript",
                    "wall.css": "text/css",
                    "timeline.js": "application/javascript",
                    "timeline.css": "text/css",
                }[name]
                route.fulfill(
                    status=200, content_type=content_type, body=(assets / name).read_text()
                )
                return
            if path == "/media/hls.js":
                route.fulfill(
                    status=200,
                    content_type="application/javascript",
                    body="window.Hls = {isSupported: () => false};",
                )
                return
            if path == "/api/v1/auth/login":
                data = {
                    "session_token": "camera-dialog-fixture-session-token-000000",
                    "account": account,
                    "expires_at": "2030-01-01T00:00:00Z",
                }
            elif path == "/api/v1/auth/me":
                data = account
            elif path == "/api/v1/operator/status":
                data = {
                    "worker_status": "ready",
                    "max_recording_bytes": 20 * 1024**3,
                    "storage": None,
                    "server_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                    "clock": {"status": "observing", "sync_verified": False},
                }
            elif path == "/api/v1/operator/camera-profiles":
                payload = route.request.post_data_json
                probe_id = "generated-dialog-probe-" + str(len(probes) + 1)
                probes[probe_id] = payload["host"]
                data = {
                    "probe_id": probe_id,
                    "expires_in_seconds": 120,
                    "clock": {
                        "status": "unavailable",
                        "offset_seconds": None,
                        "measured_at": None,
                        "uncertainty_seconds": None,
                        "reported_mode": "unknown",
                        "sync_verified": False,
                    },
                    "profiles": [
                        {
                            "id": "generated-h264-profile",
                            "name": "Generated H264 profile",
                            "codec": "H264",
                            "width": 320,
                            "height": 240,
                            "supported": True,
                        }
                    ],
                }
            elif path == "/api/v1/operator/cameras/connect":
                payload = route.request.post_data_json
                host = probes[payload["probe_id"]]
                submitted.append(host)
                data = {
                    "device_id": identifiers[len(cameras)],
                    "name": payload["name"],
                    "host": host,
                    "status": "connecting",
                    "recording": False,
                    "recording_enabled": True,
                    "recording_state": "waiting",
                    "analytics_status": "disabled",
                }
                cameras.append(data)
                status = 201
            elif path == "/api/v1/operator/cameras":
                data = {"cameras": cameras, "worker_status": "ready"}
            elif path.endswith("/media-session"):
                if identifiers[0] in path and not pending:
                    pending.append(route)
                    return
                data = {
                    "live_url": "/media/live/k5_" + "a" * 64 + "/index.m3u8",
                    "playback_url": "/media/playback?path=k5_" + "a" * 64,
                }
            else:
                status = 404
            route.fulfill(status=status, content_type="application/json", body=json.dumps(data))

        page.route("**/*", intercept)
        page.goto("http://127.0.0.1:8000/operator/")
        page.locator("#username").fill("fixture-admin")
        page.locator("#password").fill("fixture-browser-password")
        page.locator("#sign-in-button").click()
        page.locator("#workspace").wait_for(state="visible")

        def prepare_dialog(name, host):
            page.locator("#add-camera-button").click()
            page.locator("#new-camera-name").fill(name)
            page.locator("#new-camera-host").fill(host)
            page.locator("#new-camera-username").fill("generated-user")
            page.locator("#new-camera-password").fill("generated-camera-password")
            with page.expect_response(
                lambda response: (
                    response.request.method == "POST"
                    and response.url.endswith("/api/v1/operator/camera-profiles")
                )
            ) as profiles:
                page.locator("#test-camera-connection").click()
            _require(profiles.value.status == 200, "Overlap regression profile request failed")
            page.locator("#camera-profile").select_option("generated-h264-profile")

        prepare_dialog("First generated camera", hosts[0])
        page.locator("#save-camera").click()
        page.locator("#add-camera-dialog").wait_for(state="hidden")
        deadline = time.monotonic() + 5
        while not pending and time.monotonic() < deadline:
            page.wait_for_timeout(10)
        _require(bool(pending), "Previous camera selection was not held for the overlap regression")
        prepare_dialog("Second generated camera", hosts[1])
        pending[0].fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "live_url": "/media/live/k5_" + "a" * 64 + "/index.m3u8",
                    "playback_url": "/media/playback?path=k5_" + "a" * 64,
                }
            ),
        )
        page.wait_for_function("() => window.Hls !== undefined")
        _require(
            page.locator("#new-camera-host").input_value() == hosts[1]
            and page.locator("#camera-profile").input_value() == "generated-h264-profile"
            and page.locator("#save-camera").is_enabled(),
            "Previous camera completion erased the next dialog's selected camera profile",
        )
        with page.expect_response(
            lambda response: (
                response.request.method == "POST"
                and response.url.endswith("/api/v1/operator/cameras/connect")
            )
        ) as second:
            page.locator("#save-camera").click()
        _require(second.value.status == 201, "The second dialog did not enroll through the UI")
        page.locator("#add-camera-dialog").wait_for(state="hidden")
        _require(
            submitted == list(hosts),
            "Dialog submissions did not preserve independent camera probes",
        )
        _assert_no_browser_storage(page)
    except BaseException:
        failed = True
        raise
    finally:
        cleanup_failed = False
        for resource in (context, browser):
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    cleanup_failed = True
        if playwright is not None:
            try:
                playwright.stop()
            except Exception:
                cleanup_failed = True
        if not failed:
            _require(not cleanup_failed, "Overlap regression browser cleanup failed")


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
                "-vf",
                "noise=alls=8:allf=t+u",
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
    reservations, ports = helpers._reserve_ports(10)
    upstream_ports, product_ports = ports[:4], ports[4:]
    processes, publishers, onvif_cameras = [], [], []
    product = None
    browser = context = anonymous = playwright = None
    stage = "fixture_ready"
    failure = None
    script_errors = []
    request_failures = {}
    request_counts, response_counts, browser_capabilities = {}, {}, {}
    enrollment_index = 0
    history_diagnostic = {}
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
        "host_clock_step_acceptance": False,
        "camera_count": 4,
        "width": 320,
        "height": 240,
        "frames_per_second": 15,
        "portable_ancestor_adjustment": portable_adjustment,
    }
    try:
        for listener in reservations:
            listener.close()
        stage = "upstreams_starting"
        sources, publisher_sources = [], []
        for camera_index, ((_, host, color), port) in enumerate(
            zip(_CAMERAS, upstream_ports, strict=True)
        ):
            source = f"rtsp://{host}:{port}/generated"
            sources.append(source)
            fixture = _onvif_helpers().OnvifCameraFixture(
                host=host,
                stream_uri=source,
                clock_skew_seconds=1200 if camera_index == 0 else -1200,
                clock_mode="NTP",
            )
            onvif_cameras.append(fixture)
            users = [
                {
                    "user": "any",
                    "ips": ["127.0.0.0/8", "::1"],
                    "permissions": [
                        {"action": action, "path": "generated"} for action in ("publish", "read")
                    ],
                }
            ]
            publisher_source = source
            if camera_index == 0:
                publisher_password = secrets.token_urlsafe(32)
                publisher_source = _credential_uri(
                    source, "generated-publisher", publisher_password
                )
                users = [
                    {
                        "user": fixture.username,
                        "pass": fixture.password,
                        "ips": ["127.0.0.0/8", "::1"],
                        "permissions": [{"action": "read", "path": "generated"}],
                    },
                    {
                        "user": "generated-publisher",
                        "pass": publisher_password,
                        "ips": ["127.0.0.0/8", "::1"],
                        "permissions": [{"action": "publish", "path": "generated"}],
                    },
                ]
            publisher_sources.append(publisher_source)
            config = private / f"private {color} upstream.json"
            helpers._private_write(
                config,
                json.dumps(
                    {
                        "logLevel": "warn",
                        "rtspAddress": f"{host}:{port}",
                        "rtspTransports": ["tcp"],
                        "authInternalUsers": users,
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

            def upstream_ready(host=host, port=port):
                try:
                    with socket.create_connection((host, port), timeout=0.5):
                        return True
                except OSError:
                    return False

            runtime._wait(upstream_ready, label="Generated camera upstream listener", timeout=10)
        for video, source in zip(videos, publisher_sources, strict=True):
            stage = "generated_publishers_starting"
            publisher = _publish_generated(helpers, ffmpeg, video, source, private)
            processes.append(publisher)
            publishers.append(publisher)
        stage = "authenticated_rtsp_read_controls"
        rtsp_auth_proof = _assert_rtsp_read_auth(
            ffprobe, sources[0], onvif_cameras[0].username, onvif_cameras[0].password
        )
        stage = "product_starting"
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
        stage = "browser_starting"
        playwright = browser_api.sync_playwright().start()
        browser = playwright.chromium.launch(
            channel=channel, headless=True, args=["--autoplay-policy=no-user-gesture-required"]
        )
        receipt["browser_version"] = browser.version
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        anonymous = browser.new_context()
        page = context.new_page()
        page.set_default_timeout(10_000)
        # Shift only this browser's Date API. OS UTC, media workers and the actual
        # HTTP server retain their real clocks; performance.now remains monotonic.
        page.add_init_script("""(() => {
          const NativeDate = Date;
          class SkewedDate extends NativeDate {
            constructor(...args) {
              super(...(args.length ? args : [NativeDate.now() + 172800000]));
            }
            static now() { return NativeDate.now() + 172800000; }
          }
          window.Date = SkewedDate;
        })();""")

        def script_failed(error):
            if len(script_errors) < 32:
                script_errors.append(_script_error_code(error))

        def request_failed(request):
            code = _request_failure_code(request)
            request_failures[code] = min(100, request_failures.get(code, 0) + 1)

        def request_started(request):
            category = _request_category(request)
            request_counts[category] = min(100, request_counts.get(category, 0) + 1)

        def response_received(response):
            code = _request_category(response.request) + "_" + str(response.status)
            response_counts[code] = min(100, response_counts.get(code, 0) + 1)

        page.on("pageerror", script_failed)
        page.on("request", request_started)
        page.on("response", response_received)
        page.on("requestfailed", request_failed)
        stage = "sign_in_page"
        page.goto(client.origin + "/operator/", wait_until="domcontentloaded")
        browser_skew = page.evaluate("Date.now() - (performance.timeOrigin + performance.now())")
        _require(
            type(browser_skew) in (int, float) and abs(browser_skew - 172800000) < 2000,
            "Qualification did not apply the browser-only two-day Date offset",
        )
        receipt["browser_date_skew_seconds"] = 172800
        browser_capabilities = page.evaluate("""() => ({
            abort_signal_any: typeof AbortSignal.any === 'function',
            abort_signal_timeout: typeof AbortSignal.timeout === 'function',
            modal_dialog: typeof HTMLDialogElement.prototype.showModal === 'function'
        })""")
        page.locator("#username").fill(username)
        page.locator("#password").fill(password)
        stage = "login_response"
        with page.expect_response(
            lambda response: response.url.endswith("/api/v1/auth/login")
        ) as login:
            page.locator("#sign-in-button").click()
        _require(login.value.status == 200, "Browser sign-in failed")
        client.token = login.value.json()["session_token"]
        stage = "login_workspace"
        page.locator("#workspace").wait_for(state="visible")
        receipt["ui_login"] = True
        stage = "service_storage_status"
        page.wait_for_function("""() => {
            const status = document.getElementById('service-status').textContent;
            const storage = document.getElementById('storage-status').textContent;
            return status === 'Media service ready' && storage.includes('recording limit') &&
              storage.includes('disk free');
        }""")
        diagnostics = client.json("/api/v1/operator/status")
        _require(
            set(diagnostics)
            == {
                "worker_status",
                "max_recording_bytes",
                "storage",
                "analytics_status",
                "server_utc",
                "clock",
            }
            and diagnostics["worker_status"] == "ready"
            and diagnostics["analytics_status"] == "disabled"
            and diagnostics["max_recording_bytes"] == 20 * 1024**3
            and diagnostics["storage"]["recording_allowed"] is True,
            "Operator service and storage status was incomplete",
        )
        _assert_recorder_time_health(page, client)
        receipt["recorder_time_health_displayed"] = True
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
        for (name, _host, _), onvif_camera in zip(_CAMERAS, onvif_cameras, strict=True):
            enrollment_index += 1
            stage = "actual_onvif_camera_enrollment"
            camera, discovered = _onvif_enroll(
                page,
                name=name,
                fixture=onvif_camera,
                reject_wrong=enrollment_index == 1,
                run_discovery=enrollment_index == 1,
            )
            if enrollment_index == 1:
                receipt["bounded_discovery_executed"] = discovered
            camera_ids.append(camera["device_id"])
        _require(len(set(camera_ids)) == 4, "Distinct generated cameras were merged")
        receipt["ui_two_camera_enrollment"] = True
        receipt["ui_four_camera_enrollment"] = True
        receipt["onvif_credential_profile_enrollment"] = True
        receipt["onvif_wrong_password_rejected"] = True
        receipt["no_manual_stream_uri"] = True
        receipt["onvif_time_health_displayed"] = True
        receipt["generated_camera_discovered"] = False
        clock_proofs = [_onvif_clock_proof(camera) for camera in onvif_cameras]
        _require(
            clock_proofs[0]["onvif_clock_offset_seconds"] > 600
            and clock_proofs[1]["onvif_clock_offset_seconds"] < -600,
            "Opposite ONVIF camera clock offsets were not preserved",
        )
        receipt["onvif_clock_skew_compensated"] = True
        receipt["onvif_clock_offsets_seconds"] = [
            proof["onvif_clock_offset_seconds"] for proof in clock_proofs
        ]
        receipt["onvif_time_read_counts"] = [
            proof["onvif_time_read_count"] for proof in clock_proofs
        ]

        def both_live():
            views = client.json("/api/v1/operator/cameras")["cameras"]
            return len(views) == 4 and all(v["status"] == "live" and v["recording"] for v in views)

        stage = "concurrent_recording"
        runtime._wait(
            both_live, label="Concurrent four-camera recording", product=product, timeout=20
        )
        receipt["simultaneous_recording"] = True
        receipt.update(rtsp_auth_proof)
        stage = "concurrent_camera_wall_decode"
        receipt["wall_layouts"] = _wall_layout_proof(
            page, camera_ids, [camera[2] for camera in _CAMERAS]
        )
        receipt["wall_decoded_frames"] = _wall_frames(
            page, camera_ids, [camera[2] for camera in _CAMERAS]
        )
        receipt["wall_viewport_fit"] = True
        receipt["wall_viewport"] = [page.viewport_size["width"], page.viewport_size["height"]]
        screenshot = os.environ.get("K5_OPERATOR_WALL_SCREENSHOT")
        if screenshot:
            target = Path(screenshot)
            _require(
                target.is_absolute() and target.suffix.lower() == ".png" and target.parent.is_dir(),
                "Generated camera-wall screenshot path is invalid",
            )
            # Explicit local inspection only: four generated scenes, no setup
            # dialog, camera credentials, real-camera pixels or account header.
            viewport = page.viewport_size
            try:
                page.set_viewport_size({"width": 1440, "height": 900})
                page.evaluate("""() => {
                  window.scrollTo(0,0);
                  document.querySelector('.main-content').scrollTop=0;
                }""")
                page.screenshot(
                    path=str(target.with_name(target.stem + "-workspace.png")), timeout=5000
                )
                page.screenshot(
                    path=str(target.with_name(target.stem + "-workspace-full.png")),
                    full_page=True,
                    timeout=5000,
                )
            finally:
                page.set_viewport_size(viewport)
            page.locator("#wall-panel").screenshot(path=str(target), timeout=5000)
        receipt["four_camera_wall"] = True
        recording_before = _camera_recording_bytes(product, camera_ids)
        runtime._wait(
            lambda: all(
                after > before
                for after, before in zip(
                    _camera_recording_bytes(product, camera_ids), recording_before, strict=True
                )
            ),
            label="Each camera records new bytes independently",
            product=product,
            timeout=15,
        )
        receipt["per_camera_recording_bytes_before"] = recording_before
        receipt["per_camera_recording_bytes_after"] = _camera_recording_bytes(product, camera_ids)
        stage = "pause_one_camera_recording"
        _open_camera_watch(page, camera_ids[0])
        _toggle_camera_recording(page, camera_ids[0], False)

        def paused_camera_live():
            views = {v["device_id"]: v for v in client.json("/api/v1/operator/cameras")["cameras"]}
            return (
                views[camera_ids[0]]["status"] == "live"
                and views[camera_ids[0]]["recording_enabled"] is False
                and views[camera_ids[0]]["recording_state"] == "paused"
                and all(views[device]["recording"] for device in camera_ids[1:])
            )

        runtime._wait(
            paused_camera_live, label="Paused recording keeps its live source", product=product
        )
        _require(
            bool(client.json(f"/api/v1/operator/cameras/{camera_ids[0]}/history")["spans"]),
            "Pausing recording hid retained history",
        )
        paused_progress = _camera_recording_bytes(product, camera_ids)
        frames_before_pause = _decoded_color(page, "live-video", "red")
        pause_observed = time.monotonic()
        runtime._wait(
            lambda: (
                time.monotonic() - pause_observed >= 3
                and all(
                    after > before
                    for after, before in zip(
                        _camera_recording_bytes(product, camera_ids)[1:],
                        paused_progress[1:],
                        strict=True,
                    )
                )
            ),
            label="Other cameras keep recording while one is paused",
            product=product,
            timeout=15,
        )
        _require(
            _camera_recording_bytes(product, camera_ids)[0] == paused_progress[0],
            "Paused camera continued writing recording bytes",
        )
        page.wait_for_function(
            """before => document.getElementById('live-video')
              .getVideoPlaybackQuality().totalVideoFrames > before""",
            arg=frames_before_pause,
        )
        receipt["pause_live_decoded_frames_before"] = frames_before_pause
        receipt["pause_live_decoded_frames_after"] = _decoded_color(page, "live-video", "red")
        receipt["pause_recording_bytes_before"] = paused_progress[0]
        receipt["pause_recording_bytes_after"] = _camera_recording_bytes(product, camera_ids)[0]
        receipt["pause_other_recording_bytes_before"] = paused_progress[1:]
        receipt["pause_other_recording_bytes_after"] = _camera_recording_bytes(product, camera_ids)[
            1:
        ]
        receipt["recording_pause_live_continues"] = True
        receipt["recording_pause_others_continue"] = True
        stage = "recording_preference_restart"
        product.stop()
        product = runtime._Product(
            identity, identity.directory / "recordings", media, caddy, product_ports
        )
        client = runtime._Client(product_ports[0])
        runtime._wait(
            lambda: product.server.started and client.get("/operator/")[0] == 200,
            label="Product restart with paused recording",
            product=product,
        )
        page.reload(wait_until="domcontentloaded")
        page.locator("#username").fill(username)
        page.locator("#password").fill(password)
        with page.expect_response(
            lambda response: response.url.endswith("/api/v1/auth/login")
        ) as login:
            page.locator("#sign-in-button").click()
        _require(login.value.status == 200, "Returning operator could not sign in")
        client.token = login.value.json()["session_token"]
        runtime._wait(
            paused_camera_live, label="Recording choice persisted across restart", product=product
        )
        _require(
            _camera_recording_bytes(product, camera_ids)[0] == paused_progress[0],
            "Product restart resumed a camera whose recording was paused",
        )
        receipt["recording_preference_persisted"] = True
        receipt["paused_recording_bytes_after_restart"] = _camera_recording_bytes(
            product, camera_ids
        )[0]
        _open_camera_watch(page, camera_ids[0])
        _decoded_color(page, "live-video", "red")
        _toggle_camera_recording(page, camera_ids[0], True)
        runtime._wait(
            lambda: _camera_recording_bytes(product, camera_ids)[0] > paused_progress[0],
            label="Resumed camera records fresh bytes",
            product=product,
            timeout=15,
        )
        receipt["recording_resume_growth"] = True
        receipt["resumed_recording_bytes"] = _camera_recording_bytes(product, camera_ids)[0]
        receipt["ui_recording_controls"] = True
        live_frames, live_urls = [], []
        # Reverse order and switch repeatedly to detect stale media selection.
        for index in (1, 0, 1):
            stage = "camera_switch_session"
            name, _, color = _CAMERAS[index]
            media_route = f"/api/v1/operator/cameras/{camera_ids[index]}/media-session"
            with page.expect_response(
                lambda response, route=media_route: response.url.endswith(route)
            ) as media_session:
                _open_camera_watch(page, camera_ids[index])
            _require(media_session.value.status == 200, "Camera media session failed")
            live_url = media_session.value.json()["live_url"]
            live_urls.append(live_url)
            stage = "live_decode"
            live_frames.append(_decoded_color(page, "live-video", color))
            _require(
                anonymous.request.get(client.origin + live_url).status == 401,
                "An anonymous browser received camera media",
            )
            _assert_no_browser_storage(page)
        receipt["live_decoded_frames"] = live_frames
        receipt["camera_switch_pixel_isolation"] = True
        receipt["anonymous_media_denied"] = True
        stage = "independent_outage"
        _wall_frames(page, camera_ids, [camera[2] for camera in _CAMERAS])
        unaffected = [
            page.locator(f'#wall-panel [data-camera-id="{device}"] video').element_handle()
            for device in camera_ids[1:]
        ]
        unaffected_before = [
            node.evaluate("video => video.getVideoPlaybackQuality().totalVideoFrames")
            for node in unaffected
        ]
        unaffected_last = list(unaffected_before)
        outage_bytes = _camera_recording_bytes(product, camera_ids)[1:]

        def unaffected_advance():
            counts = []
            for index, node in enumerate(unaffected):
                status = node.evaluate(
                    """video => ({connected:video.isConnected,
                      frames:video.getVideoPlaybackQuality().totalVideoFrames})"""
                )
                _require(
                    status["connected"] and status["frames"] >= unaffected_last[index],
                    "One camera outage reset an unaffected wall player",
                )
                unaffected_last[index] = status["frames"]
                counts.append(status["frames"])
            return all(
                after >= before + 4 for after, before in zip(counts, unaffected_before, strict=True)
            )

        helpers._stop_owned(publishers[0])

        def first_outage_only():
            unaffected_advance()
            views = client.json("/api/v1/operator/cameras")["cameras"]
            states = {v["device_id"]: v for v in views}
            return states[camera_ids[0]]["status"] == "disconnected" and all(
                states[device]["status"] == "live" and states[device]["recording"]
                for device in camera_ids[1:]
            )

        runtime._wait(first_outage_only, label="Independent camera outage", product=product)
        runtime._wait(
            unaffected_advance,
            label="Unaffected existing wall videos advance without reset",
            product=product,
        )
        receipt["wall_outage_other_decoded_frames"] = unaffected_last
        receipt["wall_outage_other_decoded_frames_before"] = unaffected_before
        runtime._wait(
            lambda: all(
                after > before
                for after, before in zip(
                    _camera_recording_bytes(product, camera_ids)[1:], outage_bytes, strict=True
                )
            ),
            label="Camera outage preserves other recording writers",
            product=product,
            timeout=15,
        )
        receipt["wall_outage_other_recording_bytes_before"] = outage_bytes
        receipt["wall_outage_other_recording_bytes_after"] = _camera_recording_bytes(
            product, camera_ids
        )[1:]
        receipt["one_camera_outage_isolated"] = True
        stage = "wall_camera_reconnect"
        publishers[0] = _publish_generated(
            helpers, ffmpeg, videos[0], publisher_sources[0], private
        )
        processes.append(publishers[0])
        runtime._wait(
            both_live,
            label="Interrupted camera reconnects independently",
            product=product,
            timeout=20,
        )
        receipt["wall_reconnected_decoded_frames"] = _wall_frames(
            page, camera_ids, [camera[2] for camera in _CAMERAS]
        )
        receipt["wall_outage_recovery"] = True
        stage = "recording_finalization"
        for publisher in publishers:
            helpers._stop_owned(publisher)
        runtime._wait(
            lambda: all(
                v["status"] == "disconnected"
                for v in client.json("/api/v1/operator/cameras")["cameras"]
            ),
            label="Recording finalization",
            product=product,
        )
        playback_frames, playback_urls = [], []
        export_frames = []
        for index, (_name, _, color) in enumerate(_CAMERAS):
            stage = "history_inventory"
            _open_camera_recordings(page, camera_ids[index])
            page.locator(".history-item").first.wait_for(state="visible", timeout=15_000)
            page.locator("#last-24-hours").click()
            actual_time = client.json("/api/v1/operator/status")["server_utc"]
            server_now = datetime.fromisoformat(actual_time.replace("Z", "+00:00"))
            page.wait_for_function(
                """server => {
                  const first=Date.parse(document.getElementById('history-start').value+'Z');
                  const last=Date.parse(document.getElementById('history-end').value+'Z');
                  return last-first===86400000 && Math.abs(last-server)<15000;
                }""",
                arg=server_now.timestamp() * 1000,
            )
            displayed_end = datetime.fromisoformat(
                page.locator("#history-end").input_value()
            ).replace(tzinfo=UTC)
            displayed_start = datetime.fromisoformat(
                page.locator("#history-start").input_value()
            ).replace(tzinfo=UTC)
            history_diagnostic.update(
                camera_index=index,
                history_end_difference_seconds=round(
                    (displayed_end - server_now).total_seconds(), 3
                ),
                history_window_seconds=(displayed_end - displayed_start).total_seconds(),
            )
            _require(
                abs((displayed_end - server_now).total_seconds()) < 15
                and (displayed_end - displayed_start).total_seconds() == 86400,
                "Rolling history followed skewed browser Date instead of real recorder UTC",
            )
            stage = "playback_decode"
            playback_frames.append(_timeline_play(page, color))
            stage = "actual_recording_export"
            export_frames.append(
                _download_recording(
                    page, ffprobe, private / f"exported {color}.mp4", helpers, color
                )
            )
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
        receipt["timeline_seek_playback"] = True
        receipt["export_decoded_frames"] = export_frames
        receipt["actual_download_saved_and_decoded"] = True
        receipt["recording_pixel_isolation"] = True
        receipt["ui_history_and_gaps"] = True
        receipt["server_utc_rolling_history"] = True
        _require(
            page.locator("#workspace").is_visible()
            and page.locator("#sign-in").is_hidden()
            and client.get("/api/v1/operator/cameras")[0] == 200,
            "Skewed browser Date incorrectly expired the still-valid human session",
        )
        receipt["browser_clock_skew_session_preserved"] = True
        receipt["script_visible_credentials_absent"] = True
        recording_root = identity.directory / "recordings"
        saved_files = sorted(recording_root.rglob("*.mp4"))
        _require(len(saved_files) >= 4, "Four-camera recording files were absent")

        def digests():
            results = []
            for path in saved_files:
                with path.open("rb") as source:
                    results.append(hashlib.file_digest(source, "sha256").hexdigest())
            return results

        saved_digests = digests()
        saved_history = client.json(f"/api/v1/operator/cameras/{camera_ids[1]}/history")["spans"]
        stage = "removal_cancel"
        _open_camera_recordings(page, camera_ids[1])
        page.locator("#remove-camera-button").click()
        page.locator("#remove-camera-dialog").wait_for(state="visible")
        _require(
            _CAMERAS[1][0] in page.locator("#remove-camera-name").inner_text(),
            "Removal confirmation named the wrong camera",
        )
        page.locator("#cancel-remove-camera").click()
        page.locator("#remove-camera-dialog").wait_for(state="hidden")
        _require(
            len(client.json("/api/v1/operator/cameras")["cameras"]) == 4,
            "Cancelling removal changed camera enrollment",
        )
        receipt["ui_removal_cancel_preserved_camera"] = True
        stage = "removal_confirm"
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
        page.wait_for_function("() => document.getElementById('camera-count').textContent === '3'")
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
        stage = "camera_reenrollment"
        restored, _ = _onvif_enroll(page, name=_CAMERAS[1][0], fixture=onvif_cameras[1])
        _require(
            restored["device_id"] == camera_ids[1],
            "Re-enrolling a retained camera changed its durable identity",
        )
        page.locator("#add-camera-dialog").wait_for(state="hidden")
        _require(
            client.json(removal_route + "/history")["spans"] == saved_history,
            "Re-enrolling a camera did not restore its retained history",
        )
        _open_camera_recordings(page, camera_ids[1])
        page.wait_for_function(
            "name => document.getElementById('camera-title').textContent === name",
            arg=_CAMERAS[1][0],
        )
        page.locator(".history-item").first.wait_for(state="visible", timeout=15_000)
        page.locator(".history-item").first.click()
        page.locator("#play-recording").click()
        stage = "reenrollment_playback_decode"
        _decoded_color(page, "playback-video", "blue")
        receipt["ui_reenrollment_restored_history"] = True
        stage = "wall_before_logout"
        for index in range(len(_CAMERAS)):
            publishers[index] = _publish_generated(
                helpers, ffmpeg, videos[index], publisher_sources[index], private
            )
            processes.append(publishers[index])
        runtime._wait(
            both_live, label="Four live sources before wall sign-out", product=product, timeout=20
        )
        receipt["logout_wall_decoded_frames"] = _wall_frames(
            page, camera_ids, [camera[2] for camera in _CAMERAS]
        )
        logout_recording_before = _camera_recording_bytes(product, camera_ids)
        stage = "logout_revocation"
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
            and page.locator("#playback-video").get_attribute("src") is None
            and page.locator("#wall-panel video").count() == 0,
            "Sign-out did not reset visible media",
        )
        receipt["logout_destroyed_wall_tiles"] = True
        runtime._wait(
            lambda: all(
                after > before
                for after, before in zip(
                    _camera_recording_bytes(product, camera_ids),
                    logout_recording_before,
                    strict=True,
                )
            ),
            label="Signing out stops wall players and preserves background recording",
            product=product,
            timeout=15,
        )
        receipt["logout_background_recording_continues"] = True
        _assert_no_browser_storage(page)
        _require(not script_errors, "Operator browser raised an uncaught script error")
        receipt["logout_revoked_media_and_session"] = True
        receipt["browser_script_errors"] = 0
        stage = "browser_closing"
        anonymous.close()
        anonymous = None
        context.close()
        context = None
        browser.close()
        browser = None
        stage = "product_closing"
    except BaseException as error:
        failure = _failure_code(error)
    finally:
        cleanup_failures = []
        for label, resource in (
            ("anonymous_browser", anonymous),
            ("operator_browser", context),
            ("browser", browser),
        ):
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
        for fixture in onvif_cameras:
            try:
                fixture.close()
            except Exception:
                cleanup_failures.append("generated_onvif_camera")
        for listener in reservations:
            listener.close()
        diagnostic = {
            "stage": stage,
            "failure_code": failure,
            "script_error_codes": script_errors,
            "request_failure_counts": request_failures,
            "request_counts": request_counts,
            "response_counts": response_counts,
            "browser_capabilities": browser_capabilities,
            "enrollment_index": enrollment_index,
            **history_diagnostic,
            "cleanup_failure_codes": cleanup_failures,
            "product_thread_alive": bool(product and product.thread.is_alive()),
            "product_thread_failed": bool(product and product.failed),
            "product_server_started": bool(product and product.server.started),
        }
        record_testsuite_property(
            "operator_beta_browser_diagnostic", json.dumps(diagnostic, sort_keys=True)
        )
        if failure is not None or cleanup_failures:
            print(
                "Operator browser qualification diagnostic: "
                + json.dumps(diagnostic, sort_keys=True)
            )
    _require(
        failure is None,
        f"Browser qualification failed at {stage}: {failure}; "
        f"script codes={','.join(script_errors) or 'none'}; "
        f"cleanup codes={','.join(cleanup_failures) or 'none'}",
    )
    _require(not cleanup_failures, "Owned browser, product or generated fixture cleanup failed")
    elapsed = time.monotonic() - started
    _require(elapsed < 180, "Browser qualification exceeded its bounded three-minute duration")
    receipt["duration_seconds"] = round(elapsed, 2)
    receipt["owned_cleanup"] = True
    record_testsuite_property("operator_beta_browser_receipt", json.dumps(receipt, sort_keys=True))
