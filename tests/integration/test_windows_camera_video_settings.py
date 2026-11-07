"""Actual installed-Windows ONVIF encoder changes and automatic wall qualification.

All cameras, accounts and media are generated on loopback. SOAP acknowledgement
alone cannot pass: the fixture changes its real publisher and ffprobe plus the
browser must decode the changed dimensions and frame rate. No physical-camera,
motion-recording, zone or hardware-capacity claim is made.
"""

from __future__ import annotations

import ctypes
import hashlib
import importlib.util
import json
import math
import os
import platform
import secrets
import shutil
import subprocess
import time
from ctypes import wintypes
from datetime import UTC, datetime, timedelta
from importlib.metadata import version
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from uuid import UUID, uuid4

import psutil
import pytest

from k5vision import identity_state
from k5vision.media.mediamtx_provider import MediaMTXBinding

_INPUTS = (
    "K5_DESKTOP_SETUP",
    "K5_DESKTOP_SETUP_SHA256",
    "K5_DESKTOP_HEAD_SHA",
    "K5_MEDIAMTX_DONOR_EXE",
    "K5_CADDY_DONOR_EXE",
    "K5_DONOR_FFMPEG",
    "K5_DONOR_FFPROBE",
)
pytestmark = pytest.mark.skipif(
    os.name != "nt"
    or os.getenv("K5_NATIVE_CAMERA_VIDEO_ACCEPTANCE") != "1"
    or not all(os.getenv(name) for name in _INPUTS),
    reason="Camera video qualification requires Windows, exact Setup and supplied tools",
)
_INITIAL = {
    "codec": "H264",
    "resolution": {"width": 320, "height": 240},
    "frame_rate": 15,
    "bitrate_kbps": 512,
    "gop": 15,
    "quality": 2.0,
    "encoding_interval": 1,
}
_CHANGED = {
    "codec": "H264",
    "resolution": {"width": 640, "height": 360},
    "frame_rate": 10,
    "bitrate_kbps": 768,
    "gop": 10,
    "quality": 4.0,
    "encoding_interval": 1,
}
_SHARED = {**_CHANGED, "gop": 20, "quality": 3.0}


def _require(condition, message):
    __tracebackhide__ = True
    if not condition:
        pytest.fail(message, pytrace=False)


def _helpers(name):
    spec = importlib.util.spec_from_file_location(
        "k5_camera_video_" + name, Path(__file__).with_name(name + ".py")
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PublisherController:
    """Replace only an owned generated publisher using prepared encoder outputs."""

    def __init__(self, parent, ffmpeg, source_uri, process, processes, helpers):
        self.parent, self.ffmpeg, self.source_uri = Path(parent), ffmpeg, source_uri
        self.process, self.processes, self.helpers = process, processes, helpers
        self.prepared = {}
        for index, settings in enumerate(
            (_INITIAL, _CHANGED, {**_CHANGED, "codec": "MPEG4"}, _SHARED)
        ):
            output = self.parent / f"generated encoder state {index}.mp4"
            width, height = settings["resolution"].values()
            codec = settings["codec"]
            command = [
                str(ffmpeg),
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"color=c=red:size={width}x{height}:rate={settings['frame_rate']}",
                "-vf",
                "noise=alls=8:allf=t+u",
                "-t",
                "3",
                "-an",
                "-threads",
                "1",
                "-c:v",
                "libx264" if codec == "H264" else "mpeg4",
                "-pix_fmt",
                "yuv420p",
                "-g",
                str(settings["gop"]),
                "-bf",
                "0",
            ]
            if codec == "H264":
                command += [
                    "-preset",
                    "ultrafast",
                    "-tune",
                    "zerolatency",
                    "-keyint_min",
                    str(settings["gop"]),
                    "-crf",
                    str(30 - 2 * settings["quality"]),
                    "-b:v",
                    f"{settings['bitrate_kbps']}k",
                ]
            else:
                command += ["-q:v", str(12 - settings["quality"])]
            result = subprocess.run(
                command + [str(output)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=25,
                check=False,
            )
            _require(result.returncode == 0, "Generated encoder output could not be prepared")
            self.prepared[self._key(settings)] = output

    @staticmethod
    def _key(settings):
        return json.dumps(settings, sort_keys=True, separators=(",", ":"))

    def change(self, settings):
        key = self._key(settings)
        if key not in self.prepared:
            raise ValueError("Unprepared generated encoder state refused")
        self.helpers._stop_owned(self.process)
        self.process = self.helpers._start(
            [
                str(self.ffmpeg),
                "-hide_banner",
                "-loglevel",
                "error",
                "-re",
                "-stream_loop",
                "-1",
                "-i",
                str(self.prepared[key]),
                "-c",
                "copy",
                "-f",
                "rtsp",
                "-rtsp_transport",
                "tcp",
                self.source_uri,
            ],
            self.parent,
        )
        self.processes.append(self.process)


_PROBE_REASONS = frozenset(
    {
        "started",
        "timeout",
        "nonzero_exit",
        "oversized_output",
        "invalid_json",
        "stream_count",
        "frame_count",
        "untimed_frames",
        "nonmonotonic_pts",
        "geometry_mismatch",
        "valid",
        "rate_mismatch",
        "expected_geometry_mismatch",
        "expected_codec_mismatch",
    }
)
_PROBE_NUMBERS = frozenset(
    {
        "probe_exit_code",
        "probe_output_bytes",
        "probe_frame_count",
        "probe_timed_frames",
        "probe_untimed_frames",
        "probe_width",
        "probe_height",
        "probe_fps",
    }
)


def _probe_note(diagnostic, reason, started, **numbers):
    if diagnostic is None:
        return
    diagnostic["probe_reason"] = reason if reason in _PROBE_REASONS else "invalid_json"
    diagnostic["probe_elapsed_ms"] = min(
        900_000, max(0, round((time.monotonic() - started) * 1000))
    )
    for name, value in numbers.items():
        finite = type(value) is int or (type(value) is float and math.isfinite(value))
        if name in _PROBE_NUMBERS and finite:
            diagnostic[name] = min(2**32 - 1, max(-(2**31), value))


def _probe(ffprobe, source, *, network=True, diagnostic=None):
    started = time.monotonic()
    if diagnostic is not None:
        for name in _PROBE_NUMBERS:
            diagnostic.pop(name, None)
        diagnostic["probe_attempts"] = min(100, diagnostic.get("probe_attempts", 0) + 1)
    _probe_note(diagnostic, "started", started)
    command = [str(ffprobe), "-v", "error"]
    if network:
        command += [
            "-rtsp_transport",
            "tcp",
            "-analyzeduration",
            "1000000",
            "-probesize",
            "1048576",
            "-read_intervals",
            "%+1",
        ]
    command += [
        "-select_streams",
        "v:0",
        "-show_frames",
        "-show_entries",
        "stream=codec_name,width,height:frame=best_effort_timestamp_time,width,height",
        "-of",
        "json",
        str(source),
    ]
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=8,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        if diagnostic is not None:
            diagnostic["probe_timeouts"] = min(100, diagnostic.get("probe_timeouts", 0) + 1)
        _probe_note(
            diagnostic,
            "timeout",
            started,
            probe_output_bytes=len(error.stdout or b""),
        )
        raise
    _probe_note(
        diagnostic,
        "nonzero_exit" if result.returncode else "oversized_output",
        started,
        probe_exit_code=result.returncode,
        probe_output_bytes=len(result.stdout),
    )
    if result.returncode != 0 or len(result.stdout) > 262144:
        return None
    try:
        document = json.loads(result.stdout)
        streams = document["streams"]
        if len(streams) != 1:
            _probe_note(diagnostic, "stream_count", started)
            return None
        value = streams[0]
        frames = document["frames"]
        _probe_note(diagnostic, "frame_count", started, probe_frame_count=len(frames))
        if not 4 <= len(frames) <= 10000:
            return None
        missing = [
            index for index, frame in enumerate(frames) if "best_effort_timestamp_time" not in frame
        ]
        _probe_note(diagnostic, "untimed_frames", started, probe_untimed_frames=len(missing))
        # Joining RTSP can yield one cached decoder reference frame before the
        # first timed packet. It is decoded, but cannot measure cadence.
        if missing != list(range(len(missing))) or len(missing) > 2:
            return None
        times = [
            float(frame["best_effort_timestamp_time"])
            for frame in frames
            if "best_effort_timestamp_time" in frame
        ]
        _probe_note(diagnostic, "frame_count", started, probe_timed_frames=len(times))
        if len(times) < 4:
            return None
        if not all(end > start for start, end in zip(times[:-1], times[1:], strict=True)):
            _probe_note(diagnostic, "nonmonotonic_pts", started)
            return None
        if any(
            frame.get("width") != value["width"] or frame.get("height") != value["height"]
            for frame in frames
        ):
            _probe_note(diagnostic, "geometry_mismatch", started)
            return None
        value = {
            "codec": value["codec_name"],
            "width": int(value["width"]),
            "height": int(value["height"]),
            "fps": round((len(times) - 1) / (times[-1] - times[0]), 3),
            "frames": len(frames),
        }
        _probe_note(
            diagnostic,
            "valid",
            started,
            probe_fps=value["fps"],
            probe_width=value["width"],
            probe_height=value["height"],
        )
        return value
    except (ValueError, KeyError, TypeError, ZeroDivisionError):
        _probe_note(diagnostic, "invalid_json", started)
        return None


def _stream_proof(ffprobe, source, expected, desktop, *, app=None, diagnostic=None, phase=None):
    observed = {}
    if diagnostic is not None:
        diagnostic["probe_phase"] = (
            phase if phase in {"initial", "changed", "external", "restored"} else "unavailable"
        )
        wanted_rate = expected["fps"]
        if type(wanted_rate) in (int, float) and math.isfinite(wanted_rate):
            diagnostic["probe_expected_fps"] = wanted_rate

    def ready():
        try:
            value = _probe(ffprobe, source, diagnostic=diagnostic)
        except subprocess.TimeoutExpired:
            return False
        if value is None:
            return False
        observed.update(value)
        matches = value["frames"] >= 4 and all(value[k] == v for k, v in expected.items())
        if not matches and diagnostic is not None:
            diagnostic["probe_reason"] = (
                "rate_mismatch"
                if value["fps"] != expected["fps"]
                else "expected_geometry_mismatch"
                if any(value[name] != expected[name] for name in ("width", "height"))
                else "expected_codec_mismatch"
            )
        return matches

    desktop._wait(ready, "Generated delivered encoder output", timeout=20, process=app)
    return observed


def _open_wall(page):
    if page.locator("#admin-panel").is_visible():
        page.locator("#admin-return-button").click()
    page.locator("#wall-tab").click()
    page.locator("#wall-panel").wait_for(state="visible")


def _decoded_geometry(page, selector, color, width, height):
    """Read actual K5-delivered pixels, dimensions and new decoded frames."""
    wanted = {"selector": selector, "width": width, "height": height}
    page.wait_for_function(
        """wanted => {
      const v=document.querySelector(wanted.selector);
      return v && v.getBoundingClientRect().width > 0 && v.readyState >= 2 &&
        !v.paused && v.videoWidth === wanted.width && v.videoHeight === wanted.height &&
        v.getVideoPlaybackQuality().totalVideoFrames >= 4;
    }""",
        arg=wanted,
        timeout=25_000,
    )
    before = page.locator(selector).evaluate("v => v.getVideoPlaybackQuality().totalVideoFrames")
    page.wait_for_function(
        """wanted => {
      const v=document.querySelector(wanted.selector);
      return v && v.videoWidth === wanted.width && v.videoHeight === wanted.height &&
        v.getVideoPlaybackQuality().totalVideoFrames >= wanted.before + 4;
    }""",
        arg={**wanted, "before": before},
        timeout=10_000,
    )
    value = page.locator(selector).evaluate("""v => {
      const c=document.createElement('canvas'); c.width=c.height=1;
      const x=c.getContext('2d'); x.drawImage(v,0,0,1,1);
      return {rgb:Array.from(x.getImageData(0,0,1,1).data).slice(0,3),
        frames:v.getVideoPlaybackQuality().totalVideoFrames};
    }""")
    red, green, blue = value["rgb"]
    _require(
        (color == "red" and red > 180 and green < 70 and blue < 70)
        or (color == "blue" and blue > 180 and red < 70 and green < 70),
        "Delivered pixels do not match the generated camera",
    )
    return value["frames"]


def _closed_subset(files):
    """Hash only the exact pre-write inventory after its real writer closes."""
    _require(0 < len(files) <= 64, "Pre-change recording inventory was invalid")
    hashes = {}
    for path in files:
        info = path.lstat()
        _require(
            0 < info.st_size <= 16 * 1024**2 and info.st_nlink == 1,
            "Pre-change recording left its byte or identity budget",
        )
        if os.name == "nt":
            import msvcrt

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.CreateFileW.argtypes = [
                wintypes.LPCWSTR,
                wintypes.DWORD,
                wintypes.DWORD,
                ctypes.c_void_p,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.HANDLE,
            ]
            kernel.CreateFileW.restype = wintypes.HANDLE
            kernel.CloseHandle.argtypes, kernel.CloseHandle.restype = (
                [wintypes.HANDLE],
                wintypes.BOOL,
            )
            handle = kernel.CreateFileW(str(path), 0x80000000, 0, None, 3, 0x00200000, None)
            if handle in (None, ctypes.c_void_p(-1).value):
                raise RuntimeError("Pre-change recording writer is still open")
            try:
                descriptor = msvcrt.open_osfhandle(int(handle), os.O_RDONLY)
            except BaseException:
                kernel.CloseHandle(handle)
                raise
            with os.fdopen(descriptor, "rb") as stream:
                body = stream.read(16 * 1024**2 + 1)
        else:
            body = path.read_bytes()
        after = path.lstat()
        if len(body) != after.st_size or (info.st_ino, info.st_dev) != (after.st_ino, after.st_dev):
            raise RuntimeError("Pre-change recording identity changed while reading")
        hashes[path] = hashlib.sha256(body).digest()
    return hashes


def _wall_probe(
    page, ids, colors, *, layout, expected_size, expected_players, browser_helpers, dimensions=None
):
    _open_wall(page)
    _require(
        page.locator("#wall-layout").input_value() == str(layout), "Wall layout choice changed"
    )
    page.wait_for_function(
        """wanted => {
      const grid=document.querySelector('#wall-panel .camera-wall-grid');
      return grid && grid.dataset.layout === String(wanted.size) &&
        grid.querySelectorAll('video').length === wanted.players;
    }""",
        arg={"size": expected_size, "players": expected_players},
    )
    shown = page.locator("#wall-panel [data-camera-id]").evaluate_all(
        "nodes => nodes.map(node => node.getAttribute('data-camera-id'))"
    )
    _require(
        len(shown) == expected_players and set(shown) <= set(ids),
        "Wall player ownership was invalid",
    )
    for camera_id in shown:
        width, height = (dimensions or {}).get(camera_id, (320, 240))
        _decoded_geometry(
            page,
            f'#wall-panel [data-camera-id="{camera_id}"] video',
            colors[ids.index(camera_id)],
            width,
            height,
        )
    return len(shown)


def _settings_read(page, route, *, open_dialog=False, credentials=None):
    method = "GET" if credentials is None else "POST"
    endpoint = route if method == "GET" else route + "/read"
    if credentials is not None:
        page.locator("#camera-video-use-account").set_checked(True)
        page.locator("#camera-video-username").fill(credentials[0])
        page.locator("#camera-video-password").fill(credentials[1])
    with page.expect_response(
        lambda response: (
            response.request.method == method and urlsplit(response.url).path == endpoint
        ),
        timeout=30_000,
    ) as response:
        page.locator(
            "#camera-video-button"
            if open_dialog
            else "#camera-video-refresh"
            if method == "GET"
            else "#camera-video-read"
        ).click()
    _require(response.value.status == 200, "Camera encoder inspection failed")
    page.wait_for_function(
        "() => document.getElementById('camera-video-form').getAttribute('aria-busy') !== 'true'"
    )
    value = response.value.json()
    _require(
        "settings_handle" in value and value.get("writable") is True,
        "Supported camera controls were not writable",
    )
    return value


def _apply(page, route, desired, *, shared=False):
    for field, value in desired.items():
        selector = "#video-" + {
            "frame_rate": "frame-rate",
            "bitrate_kbps": "bitrate-kbps",
            "encoding_interval": "encoding-interval",
        }.get(field, field)
        if field == "resolution":
            page.locator(selector).select_option(f"{value['width']}x{value['height']}")
        elif field == "codec":
            page.locator(selector).select_option(value)
        elif page.locator(selector).evaluate("node => node.tagName") == "SELECT":
            page.locator(selector).select_option(str(value))
        else:
            page.locator(selector).fill(str(value))
    if shared:
        page.locator("#video-shared-ack").set_checked(True)
    with page.expect_response(
        lambda response: (
            response.request.method == "PATCH" and urlsplit(response.url).path == route
        ),
        timeout=45_000,
    ) as response:
        page.locator("#camera-video-apply").click()
    _require(response.value.status == 200, "Camera encoder configuration was not verified")
    value = response.value.json()
    _require(
        value.get("configuration_readback_verified") is True and value.get("observed_fps") is None,
        "Camera response confused settings with measured frame rate",
    )
    for key, expected in desired.items():
        _require(
            value["current"][key] == expected, "Camera readback did not retain the requested value"
        )
    page.wait_for_function(
        "() => document.getElementById('camera-video-form').getAttribute('aria-busy') !== 'true'"
    )
    _require(
        "configuration readback verified"
        in page.locator("#camera-video-message").inner_text().lower()
        and page.locator("#camera-video-apply").is_disabled(),
        "The interface did not show verified readback and consume its write authorization",
    )
    return value, set(response.value.request.post_data_json) - {
        "settings_handle",
        "shared_config_ack",
    }


def run_camera_video_journey(
    page,
    *,
    client,
    profile,
    parent,
    fixtures,
    sources,
    controller,
    desktop,
    browser_helpers,
    ffprobe,
    ffmpeg,
    helpers,
    app=None,
    diagnostic=None,
):
    """Shared real-worker journey; only the installed Windows wrapper qualifies release bytes."""
    __tracebackhide__ = True
    diagnostic = {} if diagnostic is None else diagnostic
    admin_helpers = _helpers("test_operator_admin_browser")
    evidence, camera_ids, colors = {}, [], []
    auto_counts, auto_sizes, auto_players = [], [], []
    for index in range(2):
        diagnostic["stage"] = "enroll_primary_" + str(index)
        result, _ = browser_helpers._onvif_enroll(
            page,
            name=f"Generated video camera {index + 1}",
            fixture=fixtures[index],
            reject_wrong=index == 0,
        )
        camera_ids.append(result["device_id"])
        colors.append("red" if index == 0 else "blue")
        auto_counts.append(index + 1)
        auto_sizes.append(1 if index == 0 else 4)
        auto_players.append(
            _wall_probe(
                page,
                camera_ids,
                colors,
                layout="auto",
                expected_size=auto_sizes[-1],
                expected_players=index + 1,
                browser_helpers=browser_helpers,
            )
        )
    target, other = camera_ids[:2]
    identity = identity_state.load_identity_state(Path(profile) / "identity")
    paths = [
        Path(profile) / "recordings" / MediaMTXBinding(identity.site_id, UUID(identifier)).path_name
        for identifier in camera_ids
    ]
    route = f"/api/v1/operator/cameras/{target}/video-settings"
    private = (fixtures[0].username, fixtures[0].password)
    public_responses, errors = [], []

    def response_observer(response):
        if "/video-settings" in urlsplit(response.url).path:
            try:
                text = response.text()
                if len(text) > 65536 or any(
                    value in text for value in (*private, "rtsp://", "rtsps://")
                ):
                    public_responses.append(False)
            except Exception:
                public_responses.append(False)

    page.on("response", response_observer)
    page.on("pageerror", lambda _error: errors.append(True))
    observer_context = None
    try:
        diagnostic["stage"] = "operator_observer"
        observer_name, observer_password = (
            "video-operator-" + secrets.token_hex(4),
            secrets.token_urlsafe(24),
        )
        _, temporary = admin_helpers._create_user(page, observer_name, "operator", [other])
        observer_context, observer = admin_helpers._new_user_page(page)
        observer.on("pageerror", lambda _error: errors.append(True))
        observer_token = admin_helpers._activate(
            observer, client.origin, observer_name, temporary, observer_password
        )
        temporary = ""
        browser_helpers._open_camera_watch(observer, other)
        other_frames = browser_helpers._decoded_color(observer, "live-video", "blue")
        other_video = observer.locator("#live-video").element_handle()
        _require(
            observer.locator("#camera-video-button").is_hidden(),
            "Operator received encoder controls",
        )
        permission_statuses = {}
        for method, suffix, payload, key in (
            ("GET", "", None, "operator_get"),
            ("POST", "/read", {"username": "unused", "password": "unused"}, "operator_read"),
            ("PATCH", "", {"settings_handle": "x" * 32, "frame_rate": 10}, "operator_patch"),
        ):
            observer_route = f"/api/v1/operator/cameras/{other}/video-settings"
            status, _ = admin_helpers._api(
                observer, observer_token, method, observer_route + suffix, payload
            )
            _require(status == 403, "Operator encoder permission was not enforced")
            permission_statuses[key] = status
        admin_helpers._return_to_cameras(page)
        browser_helpers._open_camera_watch(page, target)
        browser_helpers._decoded_color(page, "live-video", "red")
        diagnostic["stage"] = "initial_encoder_stream"
        evidence["streams"] = {
            "before": _stream_proof(
                ffprobe,
                sources[0],
                {"codec": "h264", "width": 320, "height": 240, "fps": 15.0},
                desktop,
                app=app,
                diagnostic=diagnostic,
                phase="initial",
            )
        }
        desktop._wait(
            lambda: (
                desktop._recording_bytes(profile, paths[0]) > 0
                and desktop._recording_bytes(profile, paths[1]) > 0
            ),
            "Initial camera recording bytes",
            process=app,
        )
        page.wait_for_timeout(5000)
        diagnostic["stage"] = "encoder_options"
        inspected = _settings_read(page, route, open_dialog=True)
        inspected = _settings_read(page, route, credentials=private)
        _require(
            page.locator("#camera-video-username").input_value() == ""
            and page.locator("#camera-video-password").input_value() == "",
            "Encoder management credentials were retained in the form",
        )
        fields = set(inspected["options"]["writable_fields"])
        _require(
            {"resolution", "frame_rate", "bitrate_kbps", "gop", "quality"} <= fields,
            "Advertised encoder controls were missing",
        )
        status, body, _ = client.get(
            route,
            method="PATCH",
            data={"settings_handle": inspected["settings_handle"], "codec": "H265"},
        )
        _require(status == 422 and len(body) < 2048, "Unsupported codec write was admitted")
        permission_statuses["invalid_codec"] = status
        evidence["other_live_frames_before"] = other_frames
        evidence["other_recording_bytes_before"] = desktop._recording_bytes(profile, paths[1])
        diagnostic["stage"] = "encoder_apply"
        first_apply_utc = datetime.now(UTC)
        prechange_files = sorted(paths[0].glob("*.mp4"))
        _require(
            bool(prechange_files), "No original recording inventory preceded the encoder write"
        )
        result, changed = _apply(
            page,
            route,
            {
                key: _CHANGED[key]
                for key in ("resolution", "frame_rate", "bitrate_kbps", "gop", "quality")
            },
        )
        _require(
            fixtures[0].current() == _CHANGED, "Actual camera did not apply encoder configuration"
        )
        page.locator("#camera-video-cancel").click()
        diagnostic["stage"] = "changed_encoder_stream"
        evidence["streams"]["changed"] = _stream_proof(
            ffprobe,
            sources[0],
            {"codec": "h264", "width": 640, "height": 360, "fps": 10.0},
            desktop,
            app=app,
            diagnostic=diagnostic,
            phase="changed",
        )
        _decoded_geometry(page, "#live-video", "red", 640, 360)
        desktop._wait(
            lambda: _closed_subset(prechange_files),
            "Closed pre-change recording",
            timeout=15,
            process=app,
        )
        retained = _closed_subset(prechange_files)
        _require(bool(retained), "No pre-change footage was finalized")
        diagnostic["stage"] = "unsupported_external_codec"
        fixtures[0].external_codec("MPEG4")
        diagnostic["stage"] = "external_encoder_stream"
        evidence["streams"]["external"] = _stream_proof(
            ffprobe,
            sources[0],
            {"codec": "mpeg4", "width": 640, "height": 360, "fps": 10.0},
            desktop,
            app=app,
            diagnostic=diagnostic,
            phase="external",
        )
        restored = _settings_read(page, route, open_dialog=True)
        _require(
            restored["current"]["codec"] == "MPEG4",
            "Externally changed unsupported codec was hidden",
        )
        result, selected = _apply(page, route, {"codec": "H264", "gop": 10})
        changed |= selected
        page.locator("#camera-video-cancel").click()
        diagnostic["stage"] = "restored_encoder_stream"
        evidence["streams"]["restored"] = _stream_proof(
            ffprobe,
            sources[0],
            {"codec": "h264", "width": 640, "height": 360, "fps": 10.0},
            desktop,
            app=app,
            diagnostic=diagnostic,
            phase="restored",
        )
        _decoded_geometry(page, "#live-video", "red", 640, 360)
        diagnostic["stage"] = "shared_encoder_acknowledgement"
        fixtures[0].shared(True)
        shared = _settings_read(page, route, open_dialog=True)
        _require(shared["shared_configuration"] is True, "Shared encoder configuration was hidden")
        status, _, _ = client.get(
            route, method="PATCH", data={"settings_handle": shared["settings_handle"], "gop": 20}
        )
        _require(status == 409, "Shared encoder change lacked an acknowledgement gate")
        permission_statuses["shared_without_ack"] = status
        _settings_read(page, route)
        result, selected = _apply(page, route, {"gop": 20, "quality": 3.0}, shared=True)
        changed |= selected
        _require(fixtures[0].current() == _SHARED, "Shared camera readback was not retained")
        evidence["server_stream_verified"] = result["stream_verified"]
        evidence["server_observed_fps"] = result["observed_fps"]
        fixtures[0].error_mode("malformed_options")
        writes = fixtures[0].evidence()["set_configuration_requests"]
        with page.expect_response(
            lambda response: (
                response.request.method == "GET" and urlsplit(response.url).path == route
            )
        ) as malformed:
            page.locator("#camera-video-refresh").click()
        _require(
            malformed.value.status == 502 and len(malformed.value.body()) < 2048,
            "Malformed camera response did not produce a bounded error",
        )
        page.wait_for_function(
            "() => document.getElementById('camera-video-form')"
            ".getAttribute('aria-busy') !== 'true'"
        )
        _require(
            page.locator("#camera-video-apply").is_disabled()
            and page.locator("#camera-video-message").evaluate(
                "node => node.classList.contains('error')"
            ),
            "Malformed camera response did not leave the interface in a safe error state",
        )
        _require(
            fixtures[0].evidence()["set_configuration_requests"] == writes,
            "Failed inspection changed the camera",
        )
        permission_statuses["malformed_camera"] = malformed.value.status
        fixtures[0].error_mode("normal")
        page.locator("#camera-video-cancel").click()
        diagnostic["stage"] = "independent_recording_progress"
        evidence["target_recording_bytes_before"] = desktop._recording_bytes(profile, paths[0])
        desktop._wait(
            lambda: (
                desktop._recording_bytes(profile, paths[0])
                > evidence["target_recording_bytes_before"]
                and desktop._recording_bytes(profile, paths[1])
                > evidence["other_recording_bytes_before"]
            ),
            "Independent camera recording after encoder change",
            timeout=20,
            process=app,
        )
        evidence["target_recording_bytes_after"] = desktop._recording_bytes(profile, paths[0])
        evidence["other_recording_bytes_after"] = desktop._recording_bytes(profile, paths[1])
        observer.wait_for_function(
            "before => document.getElementById('live-video').getVideoPlaybackQuality()"
            ".totalVideoFrames >= before + 4",
            arg=other_frames,
        )
        _require(
            other_video.evaluate(
                "video => video.isConnected && video === document.getElementById('live-video')"
            ),
            "Other camera's player was replaced",
        )
        evidence["other_live_frames_after"] = browser_helpers._decoded_color(
            observer, "live-video", "blue"
        )
        diagnostic["stage"] = "automatic_five_camera_wall"
        for index in range(2, 5):
            result, _ = browser_helpers._onvif_enroll(
                page, name=f"Generated video camera {index + 1}", fixture=fixtures[index]
            )
            camera_ids.append(result["device_id"])
            colors.append("blue")
        auto_counts.append(5)
        auto_sizes.append(9)
        auto_players.append(
            _wall_probe(
                page,
                camera_ids,
                colors,
                layout="auto",
                expected_size=9,
                expected_players=5,
                browser_helpers=browser_helpers,
                dimensions={target: (640, 360)},
            )
        )
        evidence.update(
            auto_camera_counts=auto_counts,
            auto_layout_sizes=auto_sizes,
            auto_live_player_counts=auto_players,
        )
        evidence["manual_layout_sizes"], evidence["manual_live_player_counts"] = [1, 4, 9], []
        for size, count in ((1, 1), (4, 4), (9, 5)):
            page.locator("#wall-layout").select_option(str(size))
            evidence["manual_live_player_counts"].append(
                _wall_probe(
                    page,
                    camera_ids,
                    colors,
                    layout=size,
                    expected_size=size,
                    expected_players=count,
                    browser_helpers=browser_helpers,
                    dimensions={target: (640, 360)},
                )
            )
        page.locator("#wall-layout").select_option("auto")
        _wall_probe(
            page,
            camera_ids,
            colors,
            layout="auto",
            expected_size=9,
            expected_players=5,
            browser_helpers=browser_helpers,
            dimensions={target: (640, 360)},
        )
        diagnostic["stage"] = "unsupported_direct_rtsp"
        browser_helpers._rtsp_enroll(
            page,
            name="Generated video camera 5",
            source_uri=sources[1],
            username="",
            password="",
            device_id=camera_ids[-1],
        )
        browser_helpers._open_camera_watch(page, camera_ids[-1])
        with page.expect_response(
            lambda response: (
                response.request.method == "GET"
                and response.url.endswith(f"/cameras/{camera_ids[-1]}/video-settings")
            )
        ) as unsupported:
            page.locator("#camera-video-button").click()
        unsupported_value = unsupported.value.json()
        page.wait_for_function(
            "() => document.getElementById('camera-video-form')"
            ".getAttribute('aria-busy') !== 'true'"
        )
        _require(
            unsupported.value.status == 200
            and unsupported_value.get("writable") is False
            and "settings_handle" not in unsupported_value,
            "Direct RTSP source exposed writable ONVIF controls",
        )
        _require(
            page.locator("#camera-video-values").is_hidden(),
            "Unsupported source received encoder fields",
        )
        _require(
            page.locator("#camera-video-apply").is_disabled()
            and "onvif" in page.locator("#camera-video-message").inner_text().lower(),
            "Unsupported source did not render its final read-only explanation",
        )
        permission_statuses["rtsp_read"] = unsupported.value.status
        page.locator("#camera-video-cancel").click()
        diagnostic["stage"] = "retained_utc_playback_export"
        _require(desktop._hashes_preserved(retained), "Encoder controls modified retained footage")
        browser_helpers._open_camera_watch(page, target)
        page.locator("#history-tab").click()
        retained_file = None
        retained_probe = None
        for candidate in retained:
            probe = _probe(ffprobe, candidate, network=False)
            if (
                probe is not None
                and probe["codec"] == "h264"
                and probe["width"] == 320
                and probe["height"] == 240
                and probe["frames"] >= 75
            ):
                retained_file, retained_probe = candidate, probe
                break
        _require(
            retained_file is not None, "Original encoder recording was not retained and decodable"
        )
        original_start = datetime.strptime(
            retained_file.stem, "%Y-%m-%d_%H-%M-%S-%f_%z"
        ).astimezone(UTC)
        # Select a whole-second two-second clip wholly inside this exact old file.
        chosen = datetime.fromtimestamp(int(original_start.timestamp()) + 2, UTC)
        original_end = original_start + timedelta(
            seconds=(retained_probe["frames"] - 1) / retained_probe["fps"]
        )
        _require(
            original_start < chosen
            and chosen + timedelta(seconds=2) < original_end
            and chosen + timedelta(seconds=2) < first_apply_utc,
            "Retained clip was not strictly before the first encoder write",
        )
        first, last = original_start - timedelta(seconds=1), original_end + timedelta(seconds=1)
        history = client.json(
            f"/api/v1/operator/cameras/{target}/history?"
            + urlencode({"start": first.isoformat(), "end": last.isoformat()})
        )["spans"]
        _require(
            any(
                datetime.fromisoformat(span["started_at"].replace("Z", "+00:00")) <= chosen
                and datetime.fromisoformat(span["started_at"].replace("Z", "+00:00"))
                + timedelta(seconds=span["duration_seconds"])
                >= chosen + timedelta(seconds=2)
                for span in history
            ),
            "Retained UTC inventory did not cover the original file's clip",
        )
        page.locator("#history-start").fill(browser_helpers._datetime_local_value(first))
        page.locator("#history-end").fill(browser_helpers._datetime_local_value(last))
        with page.expect_response(
            lambda response: (
                response.request.method == "GET"
                and urlsplit(response.url).path == f"/api/v1/operator/cameras/{target}/history"
            )
        ) as loaded_history:
            page.locator('#history-form button[type="submit"]').click()
        _require(loaded_history.value.status == 200, "Original recording UTC window did not load")
        page.locator(".history-item").first.wait_for(state="visible")
        page.wait_for_function("() => !document.getElementById('load-history').disabled")
        page.locator(".history-item").first.click()
        page.wait_for_function("() => !document.getElementById('playback-duration').disabled")
        page.locator("#playback-start").fill(browser_helpers._datetime_local_value(chosen))
        page.locator("#clip-start").fill(browser_helpers._datetime_local_value(chosen))
        page.locator("#playback-duration").fill("2")
        page.locator("#play-recording").click()
        evidence["retained_playback_frames"] = _decoded_geometry(
            page, "#playback-video", "red", 320, 240
        )
        export_path = Path(parent) / "generated retained encoder footage.mp4"
        export = {}
        with page.expect_request(
            lambda request: (
                request.method == "POST"
                and urlsplit(request.url).path == f"/api/v1/operator/cameras/{target}/exports"
            )
        ) as created_export:
            evidence["export_decoded_frames"] = browser_helpers._download_recording(
                page, ffprobe, export_path, helpers, "red", evidence=export, diagnostic=diagnostic
            )
        exported = created_export.value.post_data_json
        _require(
            datetime.fromisoformat(exported["start"].replace("Z", "+00:00")) == chosen
            and exported["duration"] == 2,
            "Export did not use the selected original recording's UTC interval",
        )
        exported_probe = _probe(ffprobe, export_path, network=False)
        _require(
            exported_probe is not None
            and exported_probe["codec"] == "h264"
            and exported_probe["width"] == 320
            and exported_probe["height"] == 240
            and abs(exported_probe["fps"] - 15) < 0.01,
            "Export did not contain the original pre-change encoder output",
        )
        evidence["retained_clip_start_utc"] = chosen.isoformat().replace("+00:00", "Z")
        evidence["first_encoder_apply_utc"] = first_apply_utc.isoformat().replace("+00:00", "Z")
        evidence["export_bytes"] = export_path.stat().st_size
        evidence["export_sha256"] = hashlib.sha256(export_path.read_bytes()).hexdigest()
        _require(desktop._hashes_preserved(retained), "Export changed retained footage")
        evidence["retained_file_count"] = len(retained)
        evidence["changed_fields"] = sorted(changed)
        evidence["soap_requests"] = fixtures[0].evidence()
        evidence["permission_statuses"] = permission_statuses
        evidence["writable_video_codecs"] = ["H264"]
        _require(
            all(camera.qualified_requests() for camera in fixtures),
            "Real authenticated ONVIF enrollment was incomplete",
        )
        _require(
            not public_responses and not errors,
            "Camera controls exposed private material or a script error",
        )
        browser_helpers._assert_no_browser_storage(page)
        evidence.update(
            dict.fromkeys(
                {
                    "camera_video_settings",
                    "real_wsse_authenticated",
                    "real_encoder_options",
                    "real_encoder_set",
                    "configuration_readback_verified",
                    "delivered_geometry_verified",
                    "decoded_pts_frame_rate_verified",
                    "prechange_utc_export_bound",
                    "actual_stream_changed",
                    "unsupported_codec_h264_restore",
                    "shared_configuration_ack",
                    "unsupported_rtsp_readonly",
                    "nonadmin_permission_denied",
                    "invalid_codec_rejected",
                    "invalid_camera_response_bounded",
                    "other_camera_live_continues",
                    "other_camera_recording_grows",
                    "target_recording_resumes",
                    "retained_utc_history",
                    "retained_files_preserved",
                    "retained_export_decoded",
                    "automatic_wall_layout",
                    "manual_wall_override",
                    "credentials_cleared",
                    "public_source_material_absent",
                },
                True,
            )
        )
        return evidence
    finally:
        page.remove_listener("response", response_observer)
        if observer_context is not None:
            observer_context.close()


def test_native_windows_camera_video_settings_and_automatic_wall(record_testsuite_property):
    __tracebackhide__ = True
    started = time.monotonic()
    desktop, runtime = (
        _helpers("test_windows_desktop_app"),
        _helpers("test_operator_product_runtime"),
    )
    browser_helpers, fixture_module = (
        _helpers("test_operator_beta_browser"),
        _helpers("onvif_video_camera_fixture"),
    )
    helpers = runtime._helpers()
    browser_api = pytest.importorskip("playwright.sync_api")
    _require(version("playwright") == "1.58.0", "Reviewed Playwright version is absent")
    setup = Path(os.environ["K5_DESKTOP_SETUP"]).resolve(strict=True)
    installer_sha = hashlib.sha256(setup.read_bytes()).hexdigest()
    _require(
        installer_sha == os.environ["K5_DESKTOP_SETUP_SHA256"],
        "Camera gate received a different installer",
    )
    head = os.environ["K5_DESKTOP_HEAD_SHA"]
    _require(len(head) == 40, "Camera gate source head is absent")
    media, ffmpeg, ffprobe = (
        helpers._tool(name)
        for name in ("K5_MEDIAMTX_DONOR_EXE", "K5_DONOR_FFMPEG", "K5_DONOR_FFPROBE")
    )
    parent = Path(os.environ["LOCALAPPDATA"]) / ("K5 Camera Video Proof-" + uuid4().hex)
    identity_state.validate_new_identity_directory(parent)
    parent.mkdir(mode=0o700)
    installed, profile = parent / "installed app with spaces", parent / "private profile"
    api = desktop._Windows()
    cohort = desktop._Cohort(api)
    processes, fixtures, reservations = [], [], []
    playwright = browser = context = page = app = form = None
    diagnostic = {"stage": "graphical_install"}
    receipt, failed, cleanup_failed = {}, False, False
    try:
        desktop._install_gui(setup, installed, api, diagnostic=diagnostic)
        _require((installed / "runtime/python.exe").is_file(), "Embedded runtime was not installed")
        receipt["actual_gui_installer"] = True
        reservations, ports = helpers._reserve_ports(3)
        for listener in reservations:
            listener.close()
        port = ports[0]
        app = desktop._start_app(installed, profile, port, cohort)
        form = desktop._wait(
            lambda: api.form(app.pid), "Camera gate first-account window", process=app
        )
        desktop._wait(
            lambda: (
                api.api.IsWindowVisible(api.control(form, desktop._CREATE))
                and api.api.IsWindowEnabled(api.control(form, desktop._CREATE))
            ),
            "Camera gate create-account control",
            process=app,
        )
        username, password = "video-fixture-admin", secrets.token_urlsafe(32)
        api.set_text(form, desktop._USERNAME, username)
        api.set_text(form, desktop._PASSWORD, password)
        api.set_text(form, desktop._CONFIRM, password)
        api.click(api.control(form, desktop._CREATE))
        client = runtime._Client(port)
        desktop._wait(
            lambda: desktop._healthy(client, diagnostic),
            "Camera gate embedded startup",
            process=app,
            timeout=60,
        )
        receipt["actual_gui_account_setup"] = True
        python_processes = [
            process
            for process in cohort.snapshot()
            if process.is_running() and process.name().casefold() in {"python.exe", "pythonw.exe"}
        ]
        _require(
            python_processes
            and all(
                Path(process.exe()).resolve().is_relative_to(installed)
                for process in python_processes
            ),
            "Camera gate used an external Python installation",
        )
        receipt["embedded_runtime_independent"] = True
        diagnostic["stage"] = "generated_camera_publishers"
        sources = []
        controller = None
        for index, (host, color) in enumerate((("127.0.0.1", "red"), ("127.0.0.2", "blue"))):
            generated, uri = desktop._generate_fixture(
                parent, media, ffmpeg, ports[index + 1], helpers, host=host, color=color
            )
            processes.extend(generated)
            sources.append(uri)
            if index == 0:
                controller = PublisherController(
                    parent, ffmpeg, uri, generated[-1], processes, helpers
                )
            fixtures.append(
                fixture_module.OnvifVideoCameraFixture(
                    host=host, stream_uri=uri, on_change=controller.change if index == 0 else None
                )
            )
        for _ in range(3):
            fixtures.append(
                fixture_module.OnvifVideoCameraFixture(host="127.0.0.2", stream_uri=sources[1])
            )
        diagnostic["stage"] = "actual_edge_login"
        playwright = browser_api.sync_playwright().start()
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        context = browser.new_context(
            viewport={"width": 1440, "height": 900}, accept_downloads=True
        )
        page = context.new_page()
        page.set_default_timeout(25_000)
        page.goto(client.origin + "/operator/")
        page.locator("#username").fill(username)
        page.locator("#password").fill(password)
        with page.expect_response(
            lambda response: (
                response.request.method == "POST" and response.url.endswith("/api/v1/auth/login")
            )
        ) as signed_in:
            page.locator("#sign-in-button").click()
        _require(signed_in.value.status == 200, "Installed camera-controls login failed")
        client.token = signed_in.value.json()["session_token"]
        page.locator("#workspace").wait_for(state="visible")
        receipt.update(
            native_windows_execution=True,
            generated_media_only=True,
            actual_edge_ui=True,
            schema_version=1,
            head_sha=head,
            installer_sha256=installer_sha,
            platform=platform.system(),
            browser_channel="msedge",
            browser_version=browser.version,
            playwright_version="1.58.0",
        )
        receipt.update(
            run_camera_video_journey(
                page,
                client=client,
                profile=profile,
                parent=parent,
                fixtures=fixtures,
                sources=sources,
                controller=controller,
                desktop=desktop,
                browser_helpers=browser_helpers,
                ffprobe=ffprobe,
                ffmpeg=ffmpeg,
                helpers=helpers,
                app=app,
                diagnostic=diagnostic,
            )
        )
        diagnostic["stage"] = "owned_stop_and_uninstall"
        context.close()
        context = None
        browser.close()
        browser = None
        desktop._stop_app(app, form, api, cohort, diagnostic=diagnostic)
        receipt["no_console_windows"] = not cohort.console_seen and not cohort.failed
        _require(receipt["no_console_windows"], "Installed camera controls opened a console window")
        retained = list((profile / "recordings").rglob("*.mp4"))
        _require(bool(retained), "Camera controls retained no recording files")
        identity = identity_state.load_identity_state(profile / "identity")
        result = subprocess.run(
            [str(installed / "unins000.exe"), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=45,
            check=False,
        )
        _require(
            result.returncode == 0
            and all(path.is_file() for path in retained)
            and identity_state.load_identity_state(profile / "identity").site_id
            == identity.site_id,
            "Camera gate uninstall changed retained profile or footage",
        )
        receipt["uninstall_preserves_profile"] = True
    except BaseException:
        failed = True
    finally:
        for resource in (context, browser, playwright):
            if resource is not None:
                try:
                    resource.stop() if resource is playwright else resource.close()
                except BaseException:
                    cleanup_failed = True
        cohort.finish()
        for process in reversed(cohort.snapshot()):
            try:
                if process.is_running():
                    process.kill()
                    process.wait(timeout=10)
            except psutil.NoSuchProcess:
                pass
            except BaseException:
                cleanup_failed = True
        for fixture in fixtures:
            try:
                fixture.close()
            except BaseException:
                cleanup_failed = True
        for process in reversed(processes):
            try:
                helpers._stop_owned(process)
            except BaseException:
                cleanup_failed = True
        for listener in reservations:
            listener.close()
        if parent.exists():
            try:
                shutil.rmtree(parent)
            except BaseException:
                cleanup_failed = True
        if failed or cleanup_failed:
            safe = {
                key: value
                for key, value in diagnostic.items()
                if key
                in {
                    "stage",
                    "installer_action",
                    "installer_install_posted",
                    "health_http_status",
                    "stop_identity_checks",
                    "stop_wait_seconds",
                    "stop_owned_identity_count",
                    "timeline_reason",
                    "timeline_span_count",
                    "export_stage",
                    "export_job_status",
                    "probe_phase",
                    "probe_reason",
                    "probe_attempts",
                    "probe_timeouts",
                    "probe_elapsed_ms",
                    "probe_exit_code",
                    "probe_output_bytes",
                    "probe_frame_count",
                    "probe_timed_frames",
                    "probe_untimed_frames",
                    "probe_width",
                    "probe_height",
                    "probe_fps",
                    "probe_expected_fps",
                }
            }
            safe.update(failed=failed, cleanup_failed=cleanup_failed)
            record_testsuite_property(
                "camera_video_windows_diagnostic", json.dumps(safe, sort_keys=True)
            )
            print("Camera video qualification diagnostic: " + json.dumps(safe, sort_keys=True))
    _require(not failed and not cleanup_failed, "Installed camera video qualification failed")
    duration = time.monotonic() - started
    _require(duration < 360, "Installed camera controls exceeded their bounded duration")
    receipt.update(duration_seconds=round(duration, 3), owned_cleanup=True)
    receipt.update(
        dict.fromkeys(
            {
                "physical_camera_acceptance",
                "capacity_acceptance",
                "endurance_acceptance",
                "motion_recording",
                "camera_trigger_zones",
                "other_codecs_qualified",
                "mocked_gui",
                "user_terminal_required",
            },
            False,
        )
    )
    record_testsuite_property("camera_video_windows_receipt", json.dumps(receipt, sort_keys=True))
