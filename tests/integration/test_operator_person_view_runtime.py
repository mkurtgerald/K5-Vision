"""Opt-in real browser/product/native-inference proof using the Apache OMZ image.

The RTSP camera is a local fixture made from immutable licensed public content.
K5 authenticates real browser pixels and invokes its admitted native person
broker. No model, image, recording, raw frame or credential is an evidence file.
This establishes integration, not physical-camera suitability or AI accuracy.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import platform
import secrets
import shutil
import subprocess
import threading
import time
from importlib.metadata import version
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

from k5vision import identity_state
from k5vision.operator_analytics_person import MODEL_REVISION, PERSON_PROVIDER

_INPUTS = (
    "K5_MEDIAMTX_DONOR_EXE",
    "K5_CADDY_DONOR_EXE",
    "K5_DONOR_FFMPEG",
    "K5_DONOR_FFPROBE",
    "K5_OPERATOR_PERSON_ARTIFACT_ROOT",
    "K5_OPERATOR_PERSON_FIXTURE",
)
_FIXTURE_BYTES = 553_577
_FIXTURE_SHA256 = "2be4dfa4d874e5f7929bebd1baf401e2ee630c4bf36cead7a2f8d5421d583959"
_WIDTH, _HEIGHT = 544, 320
pytestmark = pytest.mark.skipif(
    os.getenv("K5_OPERATOR_PERSON_ACCEPTANCE") != "1"
    or not all(os.getenv(name) for name in _INPUTS),
    reason="Integrated person-view proof needs explicit opt-in and verified native tools/fixtures",
)


def _require(condition, message):
    __tracebackhide__ = True
    if not condition:
        pytest.fail(message, pytrace=False)


def _runtime_helpers():
    spec = importlib.util.spec_from_file_location(
        "k5_person_view_product_helpers",
        Path(__file__).with_name("test_operator_product_runtime.py"),
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _diagnostic_helpers():
    spec = importlib.util.spec_from_file_location(
        "k5_person_view_browser_diagnostics",
        Path(__file__).with_name("test_operator_beta_browser.py"),
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _recording_bytes(root):
    __tracebackhide__ = True
    return sum(path.stat().st_size for path in root.rglob("*.mp4"))


def _decoded_frames(ffprobe, video):
    __tracebackhide__ = True
    result = subprocess.run(
        [
            str(ffprobe),
            "-v",
            "error",
            "-count_frames",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name,width,height,nb_read_frames",
            "-of",
            "json",
            str(video),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=15,
        check=False,
    )
    _require(
        result.returncode == 0 and len(result.stdout) <= 4096,
        "Licensed fixture recording could not be decoded",
    )
    streams = json.loads(result.stdout)["streams"]
    _require(
        len(streams) == 1
        and streams[0]["codec_name"] == "h264"
        and streams[0]["width"] == _WIDTH
        and streams[0]["height"] == _HEIGHT,
        "Licensed fixture recording used unexpected geometry or codec",
    )
    frames = int(streams[0]["nb_read_frames"])
    _require(frames > 0, "Licensed fixture recording contained no decoded frames")
    return frames


def _sample_contract(response):
    __tracebackhide__ = True
    _require(response.status == 200, "The real person-view request did not succeed")
    request = response.request
    query = parse_qs(urlsplit(request.url).query, strict_parsing=True)
    _require(
        set(query) == {"width", "height", "frame_id"},
        "The browser person sample used an unexpected request contract",
    )
    result = response.json()
    _require(
        set(result) == {"status", "frame_id", "boxes"}
        and result["status"] == "active"
        and result["frame_id"] == int(query["frame_id"][0])
        and int(query["width"][0]) == _WIDTH
        and int(query["height"][0]) == _HEIGHT
        and 1 <= len(result["boxes"]) <= 32,
        "The native response did not match its positive frozen browser sample",
    )
    for box in result["boxes"]:
        _require(
            set(box) == {"category", "confidence", "x_min", "y_min", "x_max", "y_max"}
            and box["category"] == "person"
            and 0.5 <= box["confidence"] <= 1
            and 0 <= box["x_min"] < box["x_max"] <= 1
            and 0 <= box["y_min"] < box["y_max"] <= 1,
            "The native person response contained invalid boxes or an identity claim",
        )
    payload = request.post_data_buffer
    _require(
        isinstance(payload, bytes) and len(payload) == _WIDTH * _HEIGHT * 4,
        "The browser did not submit its bounded actual decoded frame",
    )
    _require(
        response.headers.get("cache-control") == "no-store",
        "The transient person response was cacheable",
    )
    return result, payload


def test_assembled_person_view_browser_native_boxes_recording_and_logout(
    tmp_path, monkeypatch, record_testsuite_property
):
    __tracebackhide__ = True
    browser_api = pytest.importorskip("playwright.sync_api")
    _require(version("playwright") == "1.58.0", "Unqualified browser driver version")
    channel = os.getenv("K5_OPERATOR_BROWSER_CHANNEL", "msedge" if os.name == "nt" else "chromium")
    _require(
        channel in {"msedge", "chromium"} and (os.name != "nt" or channel == "msedge"),
        "Unqualified native browser channel",
    )
    runtime = _runtime_helpers()
    diagnostic_helpers = _diagnostic_helpers()
    helpers = runtime._helpers()
    media_input, caddy_input, ffmpeg, ffprobe = (helpers._tool(name) for name in _INPUTS[:4])
    fixture = Path(os.environ["K5_OPERATOR_PERSON_FIXTURE"]).resolve(strict=True)
    _require(
        fixture.is_file() and fixture.stat().st_size == _FIXTURE_BYTES,
        "Official positive fixture size was refused",
    )
    with fixture.open("rb") as source:
        _require(
            hashlib.file_digest(source, "sha256").hexdigest() == _FIXTURE_SHA256,
            "Official positive fixture identity was refused",
        )
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

        # Only hosted Linux synthetic mount ownership is adjusted. Windows
        # identity/volume/ACL admission follows the complete production path.
        monkeypatch.setattr(Path, "lstat", ordinary_system_ancestor)
    private = tmp_path / "private person product fixture with spaces"
    private.mkdir(mode=0o700)
    username, password = "fixture-admin", secrets.token_urlsafe(32)
    identity = identity_state.initialize_identity_state(
        private / "identity", username=username, password=password
    )
    person_config = private / "private person configuration.json"
    helpers._private_write(
        person_config,
        json.dumps(
            {
                "schema_version": 1,
                "provider": PERSON_PROVIDER,
                "model_revision": MODEL_REVISION,
                "artifact_root": str(
                    Path(os.environ["K5_OPERATOR_PERSON_ARTIFACT_ROOT"]).resolve(strict=True)
                ),
            }
        ),
    )
    monkeypatch.setenv("K5_OPERATOR_PERSON_CONFIG", str(person_config))
    media = private / ("mediamtx.exe" if os.name == "nt" else "mediamtx")
    caddy = private / ("caddy.exe" if os.name == "nt" else "caddy")
    for supplied, admitted in ((media_input, media), (caddy_input, caddy)):
        shutil.copyfile(supplied, admitted)
        if os.name == "posix":
            admitted.chmod(0o700)
    video = private / "licensed positive H264 fixture.mp4"
    generated = subprocess.run(
        [
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-loop",
            "1",
            "-i",
            str(fixture),
            "-vf",
            f"scale={_WIDTH}:{_HEIGHT}",
            "-r",
            "15",
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
    _require(
        generated.returncode == 0 and _decoded_frames(ffprobe, video) == 180,
        "Licensed positive RTSP fixture generation failed",
    )
    reservations, ports = helpers._reserve_ports(7)
    upstream_port, *product_ports = ports
    upstream_config = private / "private person upstream.json"
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
    source_uri = f"rtsp://127.0.0.1:{upstream_port}/generated"
    recording_root = identity.directory / "recordings"
    processes = []
    product = browser = context = playwright = None
    stage = "product_start"
    failure = None
    script_errors = []
    request_failures = {}
    started = time.monotonic()
    baseline_threads = {thread.ident for thread in threading.enumerate()}
    receipt = {
        "schema_version": "1",
        "platform": platform.system(),
        "native_windows_execution": os.name == "nt",
        "browser_channel": channel,
        "playwright_version": "1.58.0",
        "model_revision": MODEL_REVISION,
        "fixture_sha256": _FIXTURE_SHA256,
        "fixture_license": "Apache-2.0",
        "public_licensed_fixture_only": True,
        "private_camera_fixture": False,
        "real_camera_acceptance": False,
        "accuracy_acceptance": False,
        "long_duration_acceptance": False,
        "mock_inference": False,
        "portable_ancestor_adjustment": portable_adjustment,
    }
    try:
        for listener in reservations:
            listener.close()
        processes.append(helpers._start([str(media), str(upstream_config)], private))
        product = runtime._Product(identity, recording_root, media, caddy, product_ports)
        client = runtime._Client(product_ports[0])
        runtime._wait(
            lambda: product.server.started and client.get("/operator/")[0] == 200,
            label="Person product startup",
            product=product,
        )
        _require(
            product.app.state.operator_person_status == "configured",
            "The real product did not admit the optional native person runtime",
        )
        stage = "browser_start"
        playwright = browser_api.sync_playwright().start()
        browser = playwright.chromium.launch(
            channel=channel, headless=True, args=["--autoplay-policy=no-user-gesture-required"]
        )
        receipt["browser_version"] = browser.version
        context = browser.new_context(viewport={"width": 1440, "height": 1100})
        page = context.new_page()
        page.set_default_timeout(15_000)
        page.on(
            "pageerror",
            lambda error: script_errors.append(diagnostic_helpers._script_error_code(error)),
        )

        def failed_request(request):
            code = diagnostic_helpers._request_failure_code(request)
            request_failures[code] = min(1000, request_failures.get(code, 0) + 1)

        page.on("requestfailed", failed_request)
        stage = "page_load"
        page.goto(client.origin + "/operator/", wait_until="domcontentloaded")
        stage = "ui_login"
        page.locator("#username").fill(username)
        page.locator("#password").fill(password)
        with page.expect_response(
            lambda response: response.url.endswith("/api/v1/auth/login")
        ) as login:
            page.locator("#sign-in-button").click()
        _require(login.value.status == 200, "Integrated person UI sign-in failed")
        client.token = login.value.json()["session_token"]
        revoked_token = client.token
        page.locator("#workspace").wait_for(state="visible")
        stage = "camera_enrollment"
        page.locator("#add-camera-button").click()
        page.locator("#new-camera-name").fill("Licensed positive person fixture")
        page.locator("#new-camera-host").fill("127.0.0.1")
        page.locator("#new-camera-source").fill(source_uri)
        with page.expect_response(
            lambda response: (
                response.request.method == "POST"
                and response.url.endswith("/api/v1/operator/cameras")
            )
        ) as enrolled:
            page.locator("#save-camera").click()
        _require(enrolled.value.status == 201, "Integrated person UI camera enrollment failed")
        camera = enrolled.value.json()
        _require(
            camera["analytics_status"] == "configured"
            and "source_uri" not in camera
            and source_uri not in json.dumps(camera),
            "The real person camera did not expose a safe configured status",
        )
        device_id = camera["device_id"]
        route = f"/api/v1/operator/cameras/{device_id}"
        page.locator("#add-camera-dialog").wait_for(state="hidden")
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
                "75",
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
        processes.append(publisher)

        def recording_live():
            views = client.json("/api/v1/operator/cameras")["cameras"]
            return len(views) == 1 and views[0]["status"] == "live" and views[0]["recording"]

        stage = "continuous_recording_readiness"
        runtime._wait(
            recording_live,
            label="Person fixture continuous recording",
            product=product,
            timeout=20,
        )
        stage = "live_browser_decode"
        page.locator("#refresh-cameras").click()
        page.wait_for_function(
            """() => {
            const video = document.getElementById('live-video');
            return video.readyState >= 2 && !video.paused && video.videoWidth === 544 &&
              video.videoHeight === 320 &&
              video.getVideoPlaybackQuality().totalVideoFrames >= 4;
        }""",
            timeout=25_000,
        )
        receipt["live_decoded_frames"] = page.locator("#live-video").evaluate(
            "video => video.getVideoPlaybackQuality().totalVideoFrames"
        )
        page.locator("#person-view-panel").wait_for(state="visible")
        page.wait_for_function("() => !document.getElementById('toggle-person-view').disabled")
        before_bytes = _recording_bytes(recording_root)
        sample_route = route + "/person-view"
        stage = "person_sample_request"
        with page.expect_response(
            lambda response: urlsplit(response.url).path == sample_route
        ) as sample:
            page.locator("#toggle-person-view").click()
        result, posted_pixels = _sample_contract(sample.value)
        stage = "positive_sample_canvas"
        page.locator("#person-view-canvas").wait_for(state="visible")
        canvas = page.locator("#person-view-canvas").evaluate(
            """(canvas, encoded) => {
            const expected = atob(encoded);
            const width = canvas.width, height = canvas.height;
            if (!width || !height) return {width, height, matching: 0, added_overlay: 0};
            const shown = canvas.getContext('2d').getImageData(0, 0, width, height).data;
            let matching = 0, addedOverlay = 0;
            for (let index = 0; index < shown.length; index += 4) {
              if (Math.max(
                Math.abs(shown[index] - expected.charCodeAt(index + 2)),
                Math.abs(shown[index + 1] - expected.charCodeAt(index + 1)),
                Math.abs(shown[index + 2] - expected.charCodeAt(index))) <= 8) matching++;
              const sourceRed = expected.charCodeAt(index + 2);
              const sourceGreen = expected.charCodeAt(index + 1);
              const sourceBlue = expected.charCodeAt(index);
              // The official fixture already has green annotations. Exclude
              // source green hues so unchanged source boxes cannot qualify.
              const sourceAlreadyGreen = sourceGreen >= 150 &&
                sourceGreen >= sourceRed + 24 && sourceGreen >= sourceBlue + 24;
              if (!sourceAlreadyGreen && Math.max(Math.abs(shown[index] - 104),
                Math.abs(shown[index + 1] - 210), Math.abs(shown[index + 2] - 164)) <= 2)
                  addedOverlay++;
            }
            return {width, height, matching: matching / (width * height),
              added_overlay: addedOverlay};
        }""",
            base64.b64encode(posted_pixels).decode("ascii"),
        )
        stage = "sample_canvas_geometry"
        _require(
            canvas["width"] == _WIDTH and canvas["height"] == _HEIGHT,
            "Positive sampled frame used incorrect canvas geometry",
        )
        matching_fraction = canvas["matching"]
        overlay_pixels = canvas["added_overlay"]
        stage = "sample_frame_and_box_pixels"
        stage = "positive_sample_controls"
        _require(
            matching_fraction >= 0.90 and overlay_pixels >= 100,
            "The person canvas did not combine the submitted frozen frame and visible boxes",
        )
        _require(
            "in this sample" in page.locator("#person-view-message").inner_text()
            and page.locator("#toggle-person-view").get_attribute("aria-pressed") == "true",
            "The positive person sample was not visible in the real operator UI",
        )
        receipt.update(
            {
                "actual_inference": True,
                "ui_sample_positive": True,
                "person_boxes": len(result["boxes"]),
                "sample_frame_id": result["frame_id"],
                "response_frame_correlated": True,
                "displayed_frame_matching_fraction": round(matching_fraction, 4),
                "box_overlay_pixels": overlay_pixels,
                "box_added_overlay_pixels": overlay_pixels,
            }
        )
        stage = "recording_during_inference"
        runtime._wait(
            lambda: _recording_bytes(recording_root) > before_bytes,
            label="Recording progress during actual person sample",
            product=product,
            timeout=8,
        )
        _require(recording_live(), "Actual person inference interrupted continuous recording")
        receipt["recording_continues"] = True
        stage = "logout_canvas_clear"
        page.locator("#sign-out-button").click()
        page.locator("#sign-in").wait_for(state="visible")
        cleared = page.locator("#person-view-canvas").evaluate(
            "canvas => canvas.hidden && canvas.width === 0 && canvas.height === 0"
        )
        _require(
            cleared
            and page.locator("#toggle-person-view").get_attribute("aria-pressed") == "false",
            "Sign-out left a person frame or person boxes visible",
        )
        stage = "logout_inference_revocation"
        replay = context.request.post(
            sample.value.request.url,
            data=posted_pixels,
            headers={
                "Authorization": "Bearer " + revoked_token,
                "Origin": client.origin,
                "Content-Type": "application/octet-stream",
            },
        )
        _require(
            replay.status == 401, "Sign-out left the captured native person request authorized"
        )
        _require(
            context.request.post(
                sample.value.request.url,
                data=posted_pixels,
                headers={
                    "Origin": client.origin,
                    "Content-Type": "application/octet-stream",
                },
            ).status
            == 401,
            "Anonymous browser pixels were admitted for person inference",
        )
        _require(
            client.get("/api/v1/operator/cameras")[0] == 401,
            "Person UI sign-out did not revoke its human session",
        )
        receipt["logout_cleared"] = True
        receipt["logout_person_request_denied"] = True
        receipt["anonymous_person_request_denied"] = True
        _require(not script_errors, "Integrated person UI raised an uncaught script error")
        receipt["browser_script_errors"] = 0
        stage = "browser_closing"
        context.close()
        context = None
        browser.close()
        browser = None
        stage = "retained_recording_export"
        runtime._login(client, username, password)
        _require(recording_live(), "Closing the person browser stopped continuous recording")
        helpers._stop_owned(publisher)
        runtime._wait(
            lambda: (
                client.json("/api/v1/operator/cameras")["cameras"][0]["status"] == "disconnected"
            ),
            label="Person fixture recording finalization",
            product=product,
        )
        spans = client.json(route + "/history")["spans"]
        _require(bool(spans), "Person sample run produced no retained recording interval")
        urls = client.json(route + "/media-session", method="POST")
        span = spans[0]
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
        status, payload, _ = client.get(playback_url, media=True, max_bytes=16 * 1024**2)
        _require(status == 200 and bool(payload), "Person sample recording did not export")
        exported = private / "licensed fixture playback.mp4"
        exported.write_bytes(payload)
        receipt["recorded_playback_decoded_frames"] = _decoded_frames(ffprobe, exported)
        stage = "product_closing"
    except BaseException as error:
        failure = diagnostic_helpers._failure_code(error)
    finally:
        cleanup_failures = []
        for label, resource in (("operator_browser", context), ("browser", browser)):
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
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and any(
            thread.name.startswith("k5-analytics") and thread.ident not in baseline_threads
            for thread in threading.enumerate()
        ):
            time.sleep(0.02)
        if any(
            thread.name.startswith("k5-analytics") and thread.ident not in baseline_threads
            for thread in threading.enumerate()
        ):
            cleanup_failures.append("native_person_threads")
        diagnostic = {
            "stage": stage,
            "failure_code": failure,
            "script_error_codes": script_errors[:32],
            "request_failure_counts": request_failures,
            "cleanup_failure_codes": cleanup_failures,
            "product_thread_alive": bool(product and product.thread.is_alive()),
            "product_thread_failed": bool(product and product.failed),
            "product_server_started": bool(product and product.server.started),
        }
        record_testsuite_property(
            "operator_person_view_runtime_diagnostic", json.dumps(diagnostic, sort_keys=True)
        )
        if failure is not None or cleanup_failures:
            print(
                "Operator person qualification diagnostic: "
                + json.dumps(diagnostic, sort_keys=True)
            )
    _require(
        failure is None,
        f"Integrated person view failed at {stage}: {failure}; "
        f"script codes={','.join(script_errors[:32]) or 'none'}; "
        f"cleanup codes={','.join(cleanup_failures) or 'none'}",
    )
    _require(not cleanup_failures, "Integrated person/browser/worker cleanup failed")
    elapsed = time.monotonic() - started
    _require(elapsed < 90, "Integrated person acceptance exceeded its 90-second duration")
    receipt["duration_seconds"] = round(elapsed, 2)
    receipt["owned_cleanup"] = True
    record_testsuite_property(
        "operator_person_view_runtime_receipt", json.dumps(receipt, sort_keys=True)
    )
