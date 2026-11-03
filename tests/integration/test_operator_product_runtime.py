"""Opt-in assembled product proof with real native workers and generated media.

All browser-facing requests pass through the real gateway. No production camera,
model, private source fixture, screenshot or credential is an evidence artifact.
"""

from __future__ import annotations

import http.cookiejar
import importlib.util
import json
import os
import platform
import re
import secrets
import shutil
import subprocess
import threading
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode, urljoin
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener

import pytest
import uvicorn

from k5vision import identity_state
from k5vision.operator_product import create_operator_product_app
from k5vision.operator_product_service import OperatorProductService

_TOOL_NAMES = (
    "K5_MEDIAMTX_DONOR_EXE",
    "K5_CADDY_DONOR_EXE",
    "K5_DONOR_FFMPEG",
    "K5_DONOR_FFPROBE",
)
pytestmark = pytest.mark.skipif(
    not all(os.getenv(name) for name in _TOOL_NAMES),
    reason="Assembled native product acceptance requires explicitly supplied native tools",
)
_SECONDS = 16
_MAX_MEDIA = 16 * 1024**2


def _helpers():
    spec = importlib.util.spec_from_file_location(
        "k5_donor_acceptance_helpers", Path(__file__).with_name("test_mediamtx_donor_runtime.py")
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Client:
    def __init__(self, port):
        self.origin = f"http://127.0.0.1:{port}"
        self.cookies = http.cookiejar.CookieJar()
        self.opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(self.cookies))
        self.token = ""

    def get(self, route, *, method="GET", data=None, media=False, max_bytes=1_048_576):
        __tracebackhide__ = True
        headers = {"Origin": self.origin}
        if self.token and not media:
            headers["Authorization"] = "Bearer " + self.token
        payload = None
        if data is not None:
            payload = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        request = Request(self.origin + route, data=payload, headers=headers, method=method)
        try:
            response = self.opener.open(request, timeout=3)
        except HTTPError as error:
            response = error
        except OSError:
            raise RuntimeError("Local operator request did not complete") from None
        with response:
            status = response.code
            deadline = time.monotonic() + 15
            body = bytearray()
            while len(body) <= max_bytes:
                if time.monotonic() >= deadline:
                    pytest.fail("Local operator response exceeded its deadline")
                part = response.read1(min(65_536, max_bytes + 1 - len(body)))
                if not part:
                    break
                body.extend(part)
            if len(body) > max_bytes:
                pytest.fail("Local operator response exceeded its byte budget")
            return status, bytes(body), response.headers

    def json(self, route, *, expected=200, **kwargs):
        __tracebackhide__ = True
        status, body, _ = self.get(route, **kwargs)
        assert status == expected, "Operator API returned an unexpected status"
        return json.loads(body)


class _Product:
    def __init__(self, identity, recording_root, media, caddy, ports):
        public, control, api, playback, rtsp, hls = ports
        self.service = OperatorProductService(
            site_id=identity.site_id,
            state_root=identity.directory,
            recording_root=recording_root,
            media_executable=media,
            gateway_executable=caddy,
            public_port=public,
            control_port=control,
            api_port=api,
            playback_port=playback,
            rtsp_port=rtsp,
            hls_port=hls,
        )
        self.app = create_operator_product_app(
            identity_directory=identity.directory, service=self.service
        )
        self.server = uvicorn.Server(
            uvicorn.Config(
                self.app, host="127.0.0.1", port=control, access_log=False, log_level="critical"
            )
        )
        self.failed = False

        def run():
            try:
                self.server.run()
            except BaseException:
                self.failed = True

        self.thread = threading.Thread(target=run, name="native-product-acceptance", daemon=True)
        self.thread.start()

    def stop(self):
        self.server.should_exit = True
        self.thread.join(timeout=25)
        assert not self.thread.is_alive(), "Owned operator application did not finish cleanup"


def _wait(condition, *, label, product=None, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if product is not None and (product.failed or not product.thread.is_alive()):
            pytest.fail(label + ": operator application exited")
        try:
            result = condition()
            if result:
                return result
        except RuntimeError:
            pass
        time.sleep(0.1)
    pytest.fail(label + ": readiness deadline expired")


def _login(client, username, password):
    __tracebackhide__ = True
    result = client.json(
        "/api/v1/auth/login", method="POST", data={"username": username, "password": password}
    )
    token = result.get("session_token")
    if not isinstance(token, str) or not 32 <= len(token) <= 128:
        pytest.fail("Operator login did not return a bounded session")
    client.token = token


def _hls(client, live_url):
    status, body, _ = client.get(live_url, media=True)
    assert status == 200 and body.startswith(b"#EXTM3U"), "Live HLS playlist did not load"
    text = body.decode()
    assert "session=" not in text and "token=" not in text, "Live playlist exposed a bypass token"
    entries = [
        line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")
    ]
    if "#EXT-X-STREAM-INF" in text:
        assert entries, "HLS master playlist has no media playlist"
        media_url = urljoin(live_url, entries[0])
        status, body, _ = client.get(media_url, media=True)
        assert status == 200, "HLS media playlist did not load"
        text = body.decode()
    else:
        media_url = live_url
    assert "session=" not in text and "token=" not in text
    segments = [
        line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")
    ]
    if not segments:
        segments = re.findall(r'#EXT-X-PART:.*?URI="([^"]+)"', text)
    assert segments, "HLS media playlist has no generated segments"
    segment_url = urljoin(media_url, segments[-1])
    assert segment_url.startswith("/media/live/"), "HLS left the admitted media gateway"
    status, payload, _ = client.get(segment_url, media=True, max_bytes=_MAX_MEDIA)
    assert status == 200 and payload, "Generated HLS media bytes did not arrive"
    return segment_url


def test_assembled_operator_product_login_live_recording_logout_and_restart(
    tmp_path, monkeypatch, record_testsuite_property
):
    helpers = _helpers()
    media_input, caddy_input, ffmpeg, ffprobe = (helpers._tool(name) for name in _TOOL_NAMES)
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
    private = tmp_path / "private operator fixture with spaces"
    private.mkdir(mode=0o700)
    username, password = "fixture-admin", secrets.token_urlsafe(32)
    identity_failed = False
    try:
        identity = identity_state.initialize_identity_state(
            private / "identity", username=username, password=password
        )
    except Exception:
        identity_failed = True
    if identity_failed:
        pytest.fail("Private identity admission or initialization was refused")
    media = private / ("mediamtx.exe" if os.name == "nt" else "mediamtx")
    caddy = private / ("caddy.exe" if os.name == "nt" else "caddy")
    for supplied, admitted in ((media_input, media), (caddy_input, caddy)):
        shutil.copyfile(supplied, admitted)
        if os.name == "posix":
            admitted.chmod(0o700)
    video = private / "generated moving fixture.mp4"
    generated = subprocess.run(
        [
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x240:rate=15",
            "-t",
            str(_SECONDS),
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
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
        timeout=20,
        check=False,
    )
    assert generated.returncode == 0, "Generated H264 fixture could not be created"
    assert helpers._decoded_frames(ffprobe, video) == _SECONDS * 15
    reservations, ports = helpers._reserve_ports(7)
    upstream_port, *product_ports = ports
    upstream_config = private / "private upstream.json"
    helpers._private_write(
        upstream_config,
        json.dumps(
            {
                "logLevel": "warn",
                "rtspAddress": f"127.0.0.1:{upstream_port}",
                "rtspTransports": ["tcp"],
                "authInternalUsers": [
                    {
                        "user": "any",
                        "ips": ["127.0.0.1", "::1"],
                        "permissions": [
                            {"action": "publish", "path": "generated"},
                            {"action": "read", "path": "generated"},
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
    processes = []
    products = []
    recording_root = identity.directory / "recordings with spaces"
    source_uri = f"rtsp://127.0.0.1:{upstream_port}/generated"
    receipt = {
        "platform": platform.system(),
        "native_windows_execution": os.name == "nt",
        "generated_media_only": True,
        "browser_decode_acceptance": False,
        "analytics_acceptance": False,
        "real_camera_acceptance": False,
    }
    try:
        for listener in reservations:
            listener.close()
        upstream = helpers._start([str(media), str(upstream_config)], private)
        processes.append(upstream)
        product = _Product(identity, recording_root, media, caddy, product_ports)
        products.append(product)
        client = _Client(product_ports[0])
        _wait(
            lambda: product.server.started and client.get("/operator/")[0] == 200,
            label="Product startup",
            product=product,
        )
        print("Product phase: login and camera enrollment", flush=True)
        for asset in ("/operator/", "/operator/app.css", "/operator/app.js", "/media/hls.js"):
            status, body, _ = client.get(asset, max_bytes=2 * 1024**2)
            assert status == 200 and body, "Packaged operator asset did not load"
        _login(client, username, password)
        camera = client.json(
            "/api/v1/operator/cameras",
            method="POST",
            expected=201,
            data={
                "name": "Generated acceptance camera",
                "host": "127.0.0.1",
                "source_uri": source_uri,
            },
        )
        if source_uri in json.dumps(camera) or "source_uri" in camera:
            pytest.fail("Camera API exposed execution-only source material")
        device_id = camera["device_id"]
        route = f"/api/v1/operator/cameras/{device_id}"
        urls = client.json(route + "/media-session", method="POST")
        live_url = urls["live_url"]
        anonymous = _Client(product_ports[0])
        assert anonymous.get(live_url, media=True)[0] == 401

        def publish():
            child = helpers._start(
                [
                    str(ffmpeg),
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-re",
                    "-i",
                    str(video),
                    "-c",
                    "copy",
                    "-f",
                    "rtsp",
                    "-rtsp_transport",
                    "tcp",
                    source_uri,
                ],
                private,
            )
            processes.append(child)
            return child

        def live():
            views = client.json("/api/v1/operator/cameras")["cameras"]
            return views and views[0]["status"] == "live" and views[0]["recording"]

        def disconnected():
            views = client.json("/api/v1/operator/cameras")["cameras"]
            return views and views[0]["status"] == "disconnected"

        first = publish()
        _wait(live, label="Enrolled continuous pull", product=product)
        print("Product phase: real live HLS and closed recording", flush=True)
        chunk_url = _hls(client, live_url)
        assert first.wait(timeout=_SECONDS + 10) == 0
        _wait(disconnected, label="Source interruption", product=product)
        first_spans = client.json(route + "/history")["spans"]
        assert first_spans
        span = first_spans[0]
        playback_url = (
            urls["playback_url"]
            + "&"
            + urlencode(
                {
                    "start": span["started_at"],
                    "duration": max(1, span["duration_seconds"]),
                    "format": "mp4",
                }
            )
        )
        assert anonymous.get(playback_url, media=True)[0] == 401
        status, payload, _ = client.get(playback_url, media=True, max_bytes=_MAX_MEDIA)
        assert status == 200 and payload
        exported = private / "authenticated export.mp4"
        exported.write_bytes(payload)
        first_frames = helpers._decoded_frames(ffprobe, exported)
        assert client.get("/api/v1/auth/logout", method="POST")[0] == 204
        assert client.get(chunk_url, media=True)[0] == 401
        assert client.get(playback_url, media=True)[0] == 401
        receipt["logout_revoked_cached_media"] = True

        _login(client, username, password)
        client.json(route + "/media-session", method="POST")
        time.sleep(5.1)
        print("Product phase: upstream reconnection without application restart", flush=True)
        second = publish()
        _wait(live, label="Automatic source reconnect", product=product)
        _hls(client, live_url)
        assert second.wait(timeout=_SECONDS + 10) == 0
        _wait(disconnected, label="Second recording finalization", product=product)
        saved_spans = client.json(route + "/history")["spans"]
        assert len(saved_spans) == 2
        saved_files = sorted(recording_root.rglob("*.mp4"))
        assert len(saved_files) >= 2
        receipt["source_reconnected"] = True

        print("Product phase: whole application restart and durable recovery", flush=True)
        product.stop()
        product = _Product(identity, recording_root, media, caddy, product_ports)
        products.append(product)
        _wait(
            lambda: product.server.started and client.get("/operator/")[0] == 200,
            label="Product restart",
            product=product,
        )
        assert client.get("/api/v1/operator/cameras")[0] == 401
        assert client.get(chunk_url, media=True)[0] == 401
        assert client.get(playback_url, media=True)[0] == 401
        _login(client, username, password)
        views = client.json("/api/v1/operator/cameras")["cameras"]
        assert len(views) == 1 and views[0]["device_id"] == device_id
        assert client.json(route + "/history")["spans"] == saved_spans
        assert sorted(recording_root.rglob("*.mp4")) == saved_files
        client.json(route + "/media-session", method="POST")
        status, restored, _ = client.get(playback_url, media=True, max_bytes=_MAX_MEDIA)
        assert status == 200 and restored == payload, (
            "Restored export changed after product restart"
        )
        exported.write_bytes(restored)
        assert helpers._decoded_frames(ffprobe, exported) == first_frames
        third = publish()
        _wait(live, label="Persisted source resumed recording", product=product)
        _hls(client, live_url)
        assert third.wait(timeout=_SECONDS + 10) == 0
        _wait(disconnected, label="Restored recording finalization", product=product)
        assert len(client.json(route + "/history")["spans"]) == 3
        receipt.update(
            {
                "whole_product_restart_preserved": True,
                "session_invalid_after_restart": True,
                "live_hls_media_received": True,
                "playback_decoded_frames": first_frames,
                "recording_spans": 3,
                "portable_ancestor_adjustment": portable_adjustment,
            }
        )
        record_testsuite_property(
            "operator_product_runtime_receipt", json.dumps(receipt, sort_keys=True)
        )
    finally:
        print("Product phase: stop and reap owned application and fixtures", flush=True)
        cleanup_failures = 0
        for product in reversed(products):
            try:
                product.stop()
            except (AssertionError, OSError, RuntimeError):
                cleanup_failures += 1
        for child in reversed(processes):
            try:
                helpers._stop_owned(child)
            except (OSError, subprocess.TimeoutExpired):
                cleanup_failures += 1
        for listener in reservations:
            listener.close()
        assert cleanup_failures == 0, "Owned product and fixture cleanup did not complete"
