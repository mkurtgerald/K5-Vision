"""Actual native old-user to service migration and stopped manual reversal.

Generated accounts, cameras and actual closed MP4 footage only. The fixed old
profile is imported through the installed native wizard into the real service.
The source is preserved; cross-user import and automatic media reuse are not claimed.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import platform
import re
import secrets
import sqlite3
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request
from uuid import uuid4

import psutil
import pytest

from k5vision import identity_state

_INPUTS = (
    "K5_DESKTOP_BASELINE_SETUP",
    "K5_DESKTOP_BASELINE_HEAD_SHA",
    "K5_DESKTOP_BASELINE_SETUP_SHA256",
    "K5_DESKTOP_BASELINE_LAUNCHER_SHA256",
    "K5_DESKTOP_SETUP",
    "K5_DESKTOP_HEAD_SHA",
    "K5_DESKTOP_SETUP_SHA256",
    "K5_DESKTOP_LAUNCHER_SHA256",
)
pytestmark = pytest.mark.skipif(
    os.name != "nt", reason="Cross-revision smoke requires actual Windows execution"
)


def _desktop_helpers():
    script = Path(__file__).with_name("test_windows_desktop_app.py")
    spec = importlib.util.spec_from_file_location("k5_upgrade_desktop_helpers", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _admit_setup(path: Path, expected_hash: str) -> None:
    assert path.is_file() and path.stat().st_size > 100_000_000
    assert len(expected_hash) == 64 and _digest(path) == expected_hash


def _assert_installed(installed: Path, head: str, launcher_hash: str) -> None:
    manifest = json.loads((installed / "payload-manifest.json").read_text(encoding="utf-8"))
    assert manifest["head_sha"] == head
    assert _digest(installed / "K5Vision.exe") == launcher_hash


def _installed_payload_check(installed: Path) -> int:
    """Run the actual installed supervisor without starting backend or emitting artifacts."""
    return subprocess.run(
        [str(installed / "K5Vision.exe"), "--installation-payload-check"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=90,
    ).returncode


def _uninstall_through_clone_completion(uninstaller: Path) -> tuple[int, bool]:
    """Wait for the actual Inno uninstall clone, not just its original EXE.

    Inno's TEMP clone may still be completing its deletion after the original
    uninstaller returns success. Keep the same 45-second total deadline for
    both processes, and never inspect or persist their paths or arguments.
    """
    process = subprocess.Popen(
        [str(uninstaller), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 45
    children: dict[int, psutil.Process] = {}
    try:
        while process.poll() is None:
            try:
                for child in psutil.Process(process.pid).children(recursive=True)[:32]:
                    children[child.pid] = child
            except psutil.Error:
                pass
            if time.monotonic() >= deadline:
                pytest.fail("Candidate uninstaller exceeded its 45-second deadline", pytrace=False)
            time.sleep(0.05)

        # A successful return code alone does not mean Inno's clone has exited.
        # Do not retry, terminate early, or skip a still-running clone.
        if process.returncode == 0:
            for child in children.values():
                while child.is_running():
                    if time.monotonic() >= deadline:
                        pytest.fail(
                            "Candidate uninstall clone exceeded its 45-second deadline",
                            pytrace=False,
                        )
                    time.sleep(0.05)
        return process.returncode, bool(children)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        for child in children.values():
            try:
                if child.is_running():
                    child.kill()
                    child.wait(timeout=5)
            except psutil.Error:
                pass


def _hash_value(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _configure_unicode_provider(client, diagnostic):
    """Use the historical browser's UTF-8 transport within its exact body cap."""
    value = {
        "enabled": False,
        "display_name": "Retained migration issuer",
        "issuer": "https://id.example.test",
        "client_id": "migration-fixture",
        "client_secret": "\U0001f600" * 3500,
        "groups_claim": "groups",
    }
    payload = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    assert len(payload) < 16 * 1024
    # This legitimate 14 KiB secret also requires more than a 32 KiB private
    # migration verify frame once cleartext and DPAPI ciphertext are base64.
    assert len(value["client_secret"].encode("utf-8")) * 8 // 3 > 32768
    request = Request(
        client.origin + "/api/v1/operator/admin/sso/provider",
        data=payload,
        method="PUT",
        headers={
            "Origin": client.origin,
            "Content-Type": "application/json",
            "Authorization": "Bearer " + client.token,
        },
    )
    try:
        response = client.opener.open(request, timeout=3)
    except HTTPError as error:
        response = error
    with response:
        status = response.code
        diagnostic["baseline_sso_http_status"] = (
            status if status in {200, 400, 401, 403, 413, 422, 500, 503} else 0
        )
        assert status == 200, "Historical Unicode provider request was refused"
        result = json.loads(response.read(16385))
        assert result.get("has_client_secret") is True


def _source_authorities(profile):
    from k5vision.legacy_identity_snapshot import _AUTHORITIES

    return {
        name: _digest(profile / "identity" / name)
        for name in sorted(_AUTHORITIES)
        if (profile / "identity" / name).is_file()
    }


def _security(profile):
    path = profile / "identity" / "operator-account-security.sqlite3"
    with sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True, timeout=3) as connection:
        rows = connection.execute(
            "SELECT user_id,secret,generation FROM mfa ORDER BY user_id"
        ).fetchall()
        oidc = connection.execute("SELECT client_secret FROM oidc_provider WHERE id=1").fetchone()
    assert len(rows) == 1 and oidc is not None
    ciphers = [bytes(rows[0][1]), bytes(oidc[0])]
    assert all(value.startswith(b"dpapi-v1:") for value in ciphers)
    return {
        "cipher_hash": _hash_value([hashlib.sha256(value).hexdigest() for value in ciphers]),
        "generation_hash": _hash_value(rows[0][2]),
        "ciphers": ciphers,
    }


def _retained(client, profile):
    users = client.json("/api/v1/operator/admin/users")["users"]
    cameras = client.json("/api/v1/operator/cameras")["cameras"]
    storage = client.json("/api/v1/operator/admin/storage")
    site = client.json("/api/v1/operator/admin/activity/export")["site_id"]
    with sqlite3.connect(
        (profile / "identity" / "operator-cameras.sqlite3").as_uri() + "?mode=ro",
        uri=True,
        timeout=3,
    ) as connection:
        sources = connection.execute(
            "SELECT device_id,device_json,source_uri FROM camera_sources ORDER BY device_id"
        ).fetchall()
    assert 2 <= len(users) <= 10 and 1 <= len(cameras) <= 4
    users = sorted(users, key=lambda row: row["id"])
    grants = [{"id": row["id"], "camera_access": row["camera_access"]} for row in users]
    return {
        "site_hash": _hash_value(site),
        "users_hash": _hash_value(users),
        "grants_hash": _hash_value(grants),
        "cameras_hash": _hash_value(sources),
        "storage_policy_hash": _hash_value(
            {
                key: storage[key]
                for key in ("quota_bytes", "minimum_free_bytes", "retention_seconds")
            }
        ),
        "storage_revision": storage["revision"],
        "user_count": len(users),
        "camera_count": len(cameras),
    }


def _export_hashes(profile):
    root = profile / "identity" / "exports"
    files = sorted(root.iterdir())
    assert 2 <= len(files) <= 32 and all(path.is_file() for path in files)
    assert any(path.suffix == ".mp4" for path in files) and any(
        path.suffix == ".json" for path in files
    )
    return sorted(_digest(path) for path in files)


def _stage_payload(stage):
    return {
        path.relative_to(stage).as_posix(): _digest(path)
        for area in ("identity", "recordings", "exports")
        if (stage / area).is_dir()
        for path in (stage / area).rglob("*")
        if path.is_file()
    }


def _status_text(api, form):
    return api.control_text(form, 1106).casefold()


def _retained_timeline_play(browser_helpers, page, camera_id):
    # The shared navigation helper admits four decoded frames. This witness
    # requires eight, observed on the same player inside one existing budget.
    deadline = time.monotonic() + 25
    browser_helpers._timeline_play(page, "red", device_id=camera_id)
    remaining = deadline - time.monotonic()
    assert remaining > 0, "Retained playback exhausted its existing decode budget"
    decoded = page.wait_for_function(
        """() => {
          const video=document.getElementById('playback-video');
          if(!video || video.getBoundingClientRect().width<=0 || video.paused ||
            video.readyState<2 || video.videoWidth!==320 || video.videoHeight!==240 ||
            video.currentTime<=0.15 || video.getVideoPlaybackQuality().totalVideoFrames<8)
            return false;
          const canvas=document.createElement('canvas'); canvas.width=canvas.height=1;
          const context=canvas.getContext('2d'); context.drawImage(video,0,0,1,1);
          return {frames:video.getVideoPlaybackQuality().totalVideoFrames,
            rgb:Array.from(context.getImageData(0,0,1,1).data).slice(0,3)};
        }""",
        timeout=max(1, int(remaining * 1000)),
    ).json_value()
    assert time.monotonic() <= deadline, "Retained playback exceeded its existing decode budget"
    assert type(decoded["frames"]) is int and 8 <= decoded["frames"] <= 10000
    red, green, blue = decoded["rgb"]
    assert red > 180 and green < 70 and blue < 70, "Retained playback decoded a different camera"
    return decoded["frames"]


def _wizard(desktop, api, app_form, cohort):
    api.click(api.control(app_form, 1012))

    def find():
        for window in api.windows():
            if api.api.IsWindowVisible(window) and api.control(window, 1104):
                process = psutil.Process(api.pid(window))
                birth = cohort.native.birth(process.pid)
                details = cohort.native.details((process.pid, birth))
                current = cohort.native.details((os.getpid(), cohort.native.birth(os.getpid())))
                assert details["sid"] == current["sid"] and details["elevated"] is True
                assert details["session"] == current["session"] and details["session"] > 0
                cohort.add(process, role="gui", expected_identity=(process.pid, birth))
                return window, process
        return None

    return desktop._wait(find, "Actual same-user elevated migration window", timeout=30)


def _authorize(api, window, username, password, code):
    api.set_text(window, 1101, username)
    api.set_text(window, 1102, password)
    api.set_text(window, 1103, code)
    api.click(api.control(window, 1104))


def _refused_signin(desktop, api, window):
    desktop._wait(
        lambda: (
            "sign-in was not accepted" in _status_text(api, window)
            and api.api.IsWindowEnabled(api.control(window, 1104))
        ),
        "Actual native migration credential refusal",
        timeout=45,
    )
    assert api.control_text(window, 1102) == api.control_text(window, 1103) == ""


def _native_copy_test(diagnostic):
    target = Path.cwd() / "legacy-native-copy-windows.xml"
    assert not target.exists(), "Native copy witness already exists"
    environment = dict(os.environ)
    environment["K5_NATIVE_MIGRATION_COPY_ACCEPTANCE"] = "1"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/unit/test_legacy_migration_copy_windows.py",
            "-o",
            "addopts=",
            "--no-cov",
            "--tb=short",
            "--junitxml=" + str(target),
        ],
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=90,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    diagnostic["native_copy_exit_code"] = result.returncode
    assert target.is_file(), "Actual native copy witness was not produced"
    root = ET.parse(target).getroot()
    suite = root.find("testsuite")
    assert suite is not None and suite.attrib["tests"] == "1"
    assert all(suite.attrib[name] == "0" for name in ("failures", "errors", "skipped"))
    tests = root.findall(".//testcase")
    assert (
        len(tests) == 1
        and tests[0].attrib["name"] == ("test_native_migration_copy_windows_actual_security_and_io")
        and result.returncode == 0
    )
    return {
        "test_name": tests[0].attrib["name"],
        "executed": True,
        "skipped": False,
        "passed": True,
    }


def _hold_copy(desktop, service, profile, window_process, cohort):
    """Pause only the exact owned source helper to make real cancellation causal."""
    machine = desktop._machine_paths()[1]
    current = cohort.native.details((os.getpid(), cohort.native.birth(os.getpid())))

    def hold():
        parent = machine / ".migration"
        if not parent.is_dir():
            return None
        for stage in parent.iterdir():
            state = stage / "target-state.json"
            if not stage.is_dir() or not state.is_file():
                continue
            value = json.loads(state.read_text())
            if value["phase"] != "copying" or value["entry_count"] < 1:
                continue
            for process in window_process.children(recursive=False):
                if (
                    Path(process.exe()).resolve()
                    != desktop._machine_paths()[0] / "runtime" / "python.exe"
                ):
                    continue
                birth = cohort.native.birth(process.pid)
                details = cohort.native.details((process.pid, birth))
                assert details["parent_pid"] == window_process.pid
                assert details["sid"] == current["sid"] and details["session"] == current["session"]
                assert details["elevated"] is True
                process.suspend()
                try:
                    assert cohort.native.birth(process.pid) == birth
                    value = json.loads(state.read_text())
                    assert value["phase"] != "committed" and not (machine / "identity").exists()
                    observed = service.observe()
                    assert (
                        observed["service_account_verified"]
                        and observed["backend_account_verified"]
                    )
                    assert observed["service_session"] == observed["backend_session"] == 0
                    payload = _stage_payload(stage)
                    assert payload
                    return process, stage, payload, _source_authorities(profile)
                except BaseException:
                    process.resume()
                    raise
        return None

    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        observed = hold()
        if observed is not None:
            return observed
        assert window_process.is_running(), (
            "Migration window exited before cancellation observation"
        )
        # Observe promptly without adding a product delay or allowing an
        # already committed site to qualify as a cancelled transaction.
        time.sleep(0.005)
    raise AssertionError("No owned pending copy was observed within the native cancellation bound")


def test_native_windows_legacy_service_migration_and_manual_reversal(record_testsuite_property):
    assert all(os.environ.get(name) for name in _INPUTS), "Missing exact legacy migration input"
    assert os.environ.get("GITHUB_ACTIONS") == "true"
    assert os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted"
    d = _desktop_helpers()
    runtime = d._helpers("test_operator_product_runtime")
    helpers = runtime._helpers()
    settings = d._helpers("installed_settings_acceptance")
    browser_helpers = d._helpers("test_operator_beta_browser")
    service_helpers = d._helpers("windows_service_acceptance")
    service_test = d._helpers("test_windows_service_recording")
    baseline = Path(os.environ["K5_DESKTOP_BASELINE_SETUP"]).resolve(strict=True)
    candidate = Path(os.environ["K5_DESKTOP_SETUP"]).resolve(strict=True)
    old_head, new_head = (
        os.environ["K5_DESKTOP_BASELINE_HEAD_SHA"],
        os.environ["K5_DESKTOP_HEAD_SHA"],
    )
    assert re.fullmatch("[a-f0-9]{40}", old_head) and re.fullmatch("[a-f0-9]{40}", new_head)
    assert old_head != new_head
    old_hash, new_hash = (
        os.environ["K5_DESKTOP_BASELINE_SETUP_SHA256"],
        os.environ["K5_DESKTOP_SETUP_SHA256"],
    )
    _admit_setup(baseline, old_hash)
    _admit_setup(candidate, new_hash)
    installed, machine_profile = d._machine_paths()
    profile = Path(os.environ["LOCALAPPDATA"]) / "K5VisionOperatorState"
    assert not installed.exists() and not machine_profile.exists() and not profile.exists(), (
        "Legacy migration requires unoccupied fixed disposable site roots"
    )
    parent = Path(os.environ["LOCALAPPDATA"]) / ("K5 Legacy Migration Proof-" + uuid4().hex)
    identity_state.validate_new_identity_directory(parent)
    parent.mkdir(mode=0o700)
    owned_trees = {parent: (parent.stat().st_dev, parent.stat().st_ino)}
    old_installed = parent / "historical app with spaces"
    username, password = "legacy-fixture-admin", secrets.token_urlsafe(32)
    api, cohort = d._Windows(), None
    cohort = d._Cohort(api)
    old_app = app = wizard_process = source_held = None
    service = camera = playwright = browser = context = page = None
    fixture_processes = []
    receipt, diagnostic, cleanup_errors = {}, {"phase": "baseline_install"}, []
    began = time.monotonic()
    copy_witness = None
    try:
        d._install_gui(baseline, old_installed, api, diagnostic=diagnostic)
        _assert_installed(
            old_installed, old_head, os.environ["K5_DESKTOP_BASELINE_LAUNCHER_SHA256"]
        )
        client = runtime._Client(8000)
        old_app = d._start_app(old_installed, profile, 8000, cohort)
        old_form = d._wait(
            lambda: api.form(old_app.pid), "Historical native account window", process=old_app
        )
        d._wait(
            lambda: api.api.IsWindowEnabled(api.control(old_form, d._CREATE)),
            "Historical native account setup",
            process=old_app,
        )
        api.set_text(old_form, d._USERNAME, username)
        api.set_text(old_form, d._PASSWORD, password)
        api.set_text(old_form, d._CONFIRM, password)
        api.click(api.control(old_form, d._CREATE))
        d._wait(
            lambda: d._healthy(client, {}),
            "Historical installed backend",
            process=old_app,
            timeout=60,
        )
        owned_trees[profile] = (profile.stat().st_dev, profile.stat().st_ino)
        runtime._login(client, username, password)
        receipt["actual_graphical_baseline_account"] = True
        reservations, ports = helpers._reserve_ports(1)
        for reservation in reservations:
            reservation.close()
        camera = d._helpers("onvif_camera_fixture").OnvifCameraFixture(
            host="127.0.0.1", stream_uri=f"rtsp://127.0.0.1:{ports[0]}/generated"
        )
        publisher_credentials = ("generated-migration-publisher", secrets.token_urlsafe(24))
        media, ffmpeg, ffprobe = (
            helpers._tool(name)
            for name in ("K5_MEDIAMTX_DONOR_EXE", "K5_DONOR_FFMPEG", "K5_DONOR_FFPROBE")
        )
        generated, uri = d._generate_fixture(
            parent,
            media,
            ffmpeg,
            ports[0],
            helpers,
            camera_credentials=(camera.username, camera.password),
            publisher_credentials=publisher_credentials,
        )
        fixture_processes.extend(generated)
        from playwright.sync_api import sync_playwright

        playwright = sync_playwright().start()
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
            lambda r: r.request.method == "POST" and r.url.endswith("/api/v1/auth/login")
        ) as login:
            page.locator("#sign-in-button").click()
        assert login.value.status == 200
        client.token = login.value.json()["session_token"]
        administrator_id = client.json("/api/v1/auth/me")["id"]
        page.locator("#workspace").wait_for(state="visible")
        enrolled, _ = browser_helpers._onvif_enroll(
            page, name="Generated migration camera", fixture=camera
        )
        camera_id = enrolled["device_id"]
        client.json(
            "/api/v1/operator/admin/users",
            method="POST",
            expected=201,
            data={
                "username": "retained-disabled-viewer",
                "display_name": "Preserved selected-camera viewer",
                "role": "viewer",
                "enabled": False,
                "camera_access": {"mode": "selected", "camera_ids": [camera_id]},
            },
        )
        storage = client.json("/api/v1/operator/admin/storage")
        client.json(
            "/api/v1/operator/admin/storage",
            method="PATCH",
            data={
                "expected_revision": storage["revision"],
                "retention_seconds": 86400,
            },
        )
        diagnostic["phase"] = "baseline_unicode_sso"
        _configure_unicode_provider(client, diagnostic)
        diagnostic["phase"] = "baseline_recording_export"
        before_bytes = d._recording_bytes(profile)
        d._wait(
            lambda: d._tray_recording(client, profile, camera_id, before_bytes, {}),
            "Actual historical camera recording growth",
            process=old_app,
            timeout=30,
        )
        time.sleep(8)
        helpers._stop_owned(generated[-1])
        closed = d._wait(
            lambda: d._closed_hashes(profile), "Closed historical MP4 footage", timeout=20
        )
        closed_hashes = sorted(value.hex() for value in closed.values())
        browser_helpers._open_camera_recordings(page, camera_id)
        assert _retained_timeline_play(browser_helpers, page, camera_id) >= 8
        old_export = parent / "historical actual export.mp4"
        assert browser_helpers._download_recording(page, ffprobe, old_export, helpers, "red") >= 8
        exports_before = _export_hashes(profile)
        enrollment = client.json(
            "/api/v1/operator/security/mfa/enroll",
            method="POST",
            data={"current_password": password},
        )
        confirmed = client.json(
            "/api/v1/operator/security/mfa/confirm",
            method="POST",
            data={
                "enrollment_token": enrollment["enrollment_token"],
                "code": settings._totp(enrollment["secret"]),
            },
        )
        recovery = confirmed["recovery_codes"]
        assert len(recovery) >= 6
        service_test._factor_login(client, username, password, recovery[0])
        before = _retained(client, profile)
        context.close()
        context = page = None
        browser.close()
        browser = None
        d._stop_app(old_app, old_form, api, cohort)
        old_app = None
        old_security = _security(profile)
        original_identity = json.loads((profile / "identity" / "identity.json").read_text())[
            "site_id"
        ]
        diagnostic["phase"] = "candidate_install"
        d._install_gui(candidate, installed, api, diagnostic=diagnostic)
        _assert_installed(installed, new_head, os.environ["K5_DESKTOP_LAUNCHER_SHA256"])
        assert _installed_payload_check(installed) == 0
        owned_trees[machine_profile] = (
            machine_profile.stat().st_dev,
            machine_profile.stat().st_ino,
        )
        service = service_helpers.InstalledService(installed)
        assert service.configuration()["account"].casefold() == "nt service\\k5visionrecorder"
        assert not (machine_profile / "identity").exists()
        diagnostic["phase"] = "native_copy_security_and_io"
        copy_witness = _native_copy_test(diagnostic)
        app = d._start_app(installed, machine_profile, 8000, cohort)
        form = d._wait(lambda: api.form(app.pid), "Service client migration window", process=app)
        d._wait(
            lambda: api.api.IsWindowEnabled(api.control(form, 1012)),
            "Native existing-site migration control",
            process=app,
        )
        window, wizard_process = _wizard(d, api, form, cohort)
        diagnostic["phase"] = "native_authorization_controls"
        _authorize(api, window, username, "incorrect-generated-password", "")
        _refused_signin(d, api, window)
        assert not (machine_profile / "identity").exists()
        receipt["existing_admin_password_required"] = True
        _authorize(api, window, username, password, "")
        _refused_signin(d, api, window)
        assert not (machine_profile / "identity").exists()
        receipt["existing_mfa_required"] = True
        diagnostic["phase"] = "actual_native_cancel"
        _authorize(api, window, username, password, recovery[1])
        source_held, cancelled_stage, cancelled_payload, sealed_before_cancel = _hold_copy(
            d, service, profile, wizard_process, cohort
        )
        api.click(api.control(window, 1105))
        d._wait(lambda: not api.api.IsWindow(window), "Cancelled native wizard retired", timeout=45)
        d._wait(
            lambda: not source_held.is_running(),
            "Cancelled Job-owned source helper exited",
            timeout=10,
        )
        source_held = None
        assert not (machine_profile / "identity").exists()
        assert _source_authorities(profile) == sealed_before_cancel
        cancelled_state = json.loads((cancelled_stage / "target-state.json").read_text())
        assert (
            cancelled_state["phase"] != "committed"
            and _stage_payload(cancelled_stage) == cancelled_payload
        )
        receipt["cancel_refused_activation"] = True
        receipt["interruption"] = {
            "cancelled_stage_count": 1,
            "pending_before_retry": True,
            "identity_absent_after_cancel": True,
            "source_unchanged_after_cancel": True,
            "recovered_stage_count": 1,
            "retired_stage_bytes_preserved": True,
        }
        diagnostic["phase"] = "authenticated_pending_recovery"
        d._wait(
            lambda: api.api.IsWindowEnabled(api.control(form, 1012)),
            "Native retry after cancelled pending transaction",
            process=app,
        )
        window, wizard_process = _wizard(d, api, form, cohort)
        _authorize(api, window, username, password, recovery[2])
        d._wait(
            lambda: not api.api.IsWindow(window), "Verified native migration completed", timeout=120
        )
        d._wait(lambda: d._healthy(client, {}), "Real migrated service backend", timeout=60)
        assert (
            json.loads((cancelled_stage / "target-state.json").read_text())["phase"]
            == "rolled_back"
        )
        assert _stage_payload(cancelled_stage) == cancelled_payload
        assert (
            json.loads((machine_profile / "identity" / "identity.json").read_text())["site_id"]
            == original_identity
        )
        # Authentication consumes a recovery factor before snapshotting; all
        # other source authorities remain unchanged throughout the new copy.
        committed_stages = [
            path
            for path in (machine_profile / ".migration").iterdir()
            if path.is_dir()
            and (path / "target-state.json").is_file()
            and json.loads((path / "target-state.json").read_text())["phase"] == "committed"
        ]
        assert len(committed_stages) == 1
        source_receipt = json.loads((committed_stages[0] / "source-receipt.json").read_text())
        assert source_receipt["site_id"] == original_identity
        sealed_after_migration = {
            name: entry["sha256"] for name, entry in source_receipt["files"].items()
        }
        assert _source_authorities(profile) == sealed_after_migration
        source_security = _security(profile)
        target_security = _security(machine_profile)
        assert source_security["ciphers"] == old_security["ciphers"]
        assert all(
            a != b
            for a, b in zip(source_security["ciphers"], target_security["ciphers"], strict=True)
        )
        assert source_security["generation_hash"] == target_security["generation_hash"]
        client.token = ""
        challenge = client.json(
            "/api/v1/auth/login",
            method="POST",
            expected=202,
            data={"username": username, "password": password},
        )
        assert challenge["mfa_required"] is True and "session_token" not in challenge
        client.json(
            "/api/v1/auth/mfa",
            method="POST",
            expected=401,
            data={"challenge_token": challenge["challenge_token"], "code": recovery[2]},
        )
        service_test._factor_login(client, username, password, recovery[3])
        assert client.json("/api/v1/operator/admin/sso/provider")["has_client_secret"] is True
        after = _retained(client, machine_profile)
        for name in ("site", "users", "grants", "cameras", "storage_policy"):
            assert before[name + "_hash"] == after[name + "_hash"]
        assert after["storage_revision"] == before["storage_revision"] + 1
        target_storage = client.json("/api/v1/operator/admin/storage")
        assert Path(target_storage["recording_directory"]) == machine_profile / "recordings"
        copied_closed = d._closed_hashes(machine_profile)
        assert sorted(value.hex() for value in copied_closed.values()) == closed_hashes
        assert _export_hashes(machine_profile) == exports_before
        retained_export_metadata = next((profile / "identity" / "exports").glob("*.json"))
        retained_export_id = retained_export_metadata.stem
        export_status, export_body, _ = client.get(
            f"/api/v1/operator/cameras/{camera_id}/exports/{retained_export_id}/file",
            max_bytes=64 * 1024**2,
        )
        assert export_status == 200
        assert hashlib.sha256(export_body).hexdigest() == _digest(
            retained_export_metadata.with_suffix(".mp4")
        )
        retained_export_download = parent / "retained original export downloaded.mp4"
        retained_export_download.write_bytes(export_body)
        assert helpers._decoded_frames(ffprobe, retained_export_download) >= 8
        assert _source_authorities(profile) == sealed_after_migration
        receipt["retained"] = {
            "user_count": before["user_count"],
            "camera_count": before["camera_count"],
            "storage_revision_before": before["storage_revision"],
            "storage_revision_after": after["storage_revision"],
            "closed_hashes_before": closed_hashes,
            "closed_hashes_after": sorted(value.hex() for value in copied_closed.values()),
            "export_hashes_before": exports_before,
            "export_hashes_after": _export_hashes(machine_profile),
        }
        for name in ("site", "users", "grants", "cameras", "storage_policy"):
            receipt["retained"][name + "_hash_before"] = before[name + "_hash"]
            receipt["retained"][name + "_hash_after"] = after[name + "_hash"]
        receipt["security"] = {
            "source_cipher_hash_before": old_security["cipher_hash"],
            "source_cipher_hash_after": source_security["cipher_hash"],
            "service_cipher_hash": target_security["cipher_hash"],
            "mfa_generation_hash_before": old_security["generation_hash"],
            "mfa_generation_hash_after": target_security["generation_hash"],
            "migration_factor_replay_refused": True,
            "service_mfa_login_succeeded": True,
            "old_profile_mfa_login_after_reversal": False,
        }
        diagnostic["phase"] = "retained_playback_export"
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        context = browser.new_context(
            viewport={"width": 1440, "height": 900}, accept_downloads=True
        )
        page = context.new_page()
        page.set_default_timeout(25_000)
        settings._mfa_login(
            page,
            client.origin,
            {"username": username, "password": password, "id": administrator_id},
            recovery[4],
        )
        browser_helpers._open_camera_recordings(page, camera_id)
        playback = _retained_timeline_play(browser_helpers, page, camera_id)
        export_path = parent / "migrated actual export.mp4"
        decoded = browser_helpers._download_recording(page, ffprobe, export_path, helpers, "red")
        assert playback >= 8 and decoded >= 8
        receipt["media"] = {
            "playback_decoded_frames": playback,
            "export_decoded_frames": decoded,
            "export_bytes": export_path.stat().st_size,
            "export_sha256": _digest(export_path),
        }
        context.close()
        context = page = None
        browser.close()
        browser = None
        diagnostic["phase"] = "client_closed_recording"
        publisher = d._publish_fixture(
            parent, ffmpeg, uri, helpers, credentials=publisher_credentials
        )
        fixture_processes.append(publisher)
        observation = service.observe()
        root_process, backend_process = service.processes()
        root_birth, backend_birth = (
            service.native_birth(root_process),
            service.native_birth(backend_process),
        )
        assert observation["service_account_verified"] and observation["backend_account_verified"]
        assert observation["service_session"] == observation["backend_session"] == 0
        previous_bytes = d._recording_bytes(machine_profile)
        api.click(api.control(form, d._STOP))
        assert app.wait(timeout=40) == 0
        app = None
        d._wait(
            lambda: d._tray_recording(client, machine_profile, camera_id, previous_bytes, {}),
            "Service recordings grow after all clients close",
            timeout=30,
        )
        root_after, backend_after = service.processes()
        assert (root_after.pid, service.native_birth(root_after)) == (root_process.pid, root_birth)
        assert (backend_after.pid, service.native_birth(backend_after)) == (
            backend_process.pid,
            backend_birth,
        )
        receipt["recording"] = {
            "before_bytes": previous_bytes,
            "after_bytes": d._recording_bytes(machine_profile),
            "service_pid_before": root_process.pid,
            "service_pid_after": root_after.pid,
            "backend_pid_before": backend_process.pid,
            "backend_pid_after": backend_after.pid,
            "service_created_100ns_before": root_birth,
            "service_created_100ns_after": service.native_birth(root_after),
            "backend_created_100ns_before": backend_birth,
            "backend_created_100ns_after": service.native_birth(backend_after),
            "session": service.session(root_after),
        }
        helpers._stop_owned(publisher)
        diagnostic["phase"] = "manual_reversal_uninstall"
        assert _uninstall_through_clone_completion(installed / "unins000.exe")[0] == 0
        assert service.absent() and not (installed / "K5Vision.exe").exists()
        cohort.service_stopped = True
        assert _source_authorities(profile) == sealed_after_migration
        assert _security(profile)["cipher_hash"] == old_security["cipher_hash"]
        assert sorted(value.hex() for value in d._closed_hashes(profile).values()) == closed_hashes
        diagnostic["phase"] = "manual_reversal_old_gui"
        d._install_gui(baseline, old_installed, api, diagnostic=diagnostic)
        _assert_installed(
            old_installed, old_head, os.environ["K5_DESKTOP_BASELINE_LAUNCHER_SHA256"]
        )
        old_app = d._start_app(old_installed, profile, 8000, cohort)
        old_form = d._wait(
            lambda: api.form(old_app.pid),
            "Manually reversed historical native client",
            process=old_app,
        )
        d._wait(
            lambda: d._healthy(client, {}),
            "Historical original backend after service uninstall",
            timeout=60,
        )
        service_test._factor_login(client, username, password, recovery[5])
        assert client.json("/api/v1/auth/me")["role"] == "administrator"
        assert _retained(client, profile)["site_hash"] == before["site_hash"]
        receipt["security"]["old_profile_mfa_login_after_reversal"] = True
        d._stop_app(old_app, old_form, api, cohort)
        old_app = None
        receipt.update(
            {
                key: True
                for key in (
                    "generated_media_only",
                    "actual_native_migration_wizard",
                    "same_windows_owner_bound",
                    "actual_service_token_verified",
                    "service_token_dpapi_rewrapped",
                    "original_profile_preserved_after_authorization",
                    "retained_site_identity",
                    "retained_users_and_grants",
                    "retained_camera_sources",
                    "storage_policy_rebased",
                    "retained_closed_footage",
                    "retained_completed_export",
                    "retained_playback_decoded",
                    "retained_export_decoded",
                    "client_closed_recording_continues",
                    "authenticated_pending_recovery",
                    "candidate_uninstall_preserved_original",
                    "manual_reversal_requires_candidate_uninstall",
                    "baseline_reversal_mfa_login",
                )
            }
        )
        receipt.update(
            {
                key: False
                for key in (
                    "mocked_gui",
                    "mocked_service",
                    "user_terminal_required",
                    "physical_camera_acceptance",
                    "actual_reboot_before_login",
                    "actual_windows_logoff",
                    "actual_clock_corrections",
                    "endurance_acceptance",
                    "cross_user_import_qualified",
                    "automatic_cross_binding_media_resume",
                    "automatic_failed_activation_rollback",
                )
            }
        )
        receipt.update(
            schema_version=1,
            baseline_head=old_head,
            candidate_head=new_head,
            baseline_installer_sha256=old_hash,
            candidate_installer_sha256=new_hash,
            platform={
                "system": platform.system(),
                "release": platform.release(),
                "architecture": platform.machine(),
            },
            actual_graphical_installations=3,
            native_copy=copy_witness,
        )
    finally:
        for resource in (context, browser, playwright):
            if resource is not None:
                try:
                    resource.close() if hasattr(resource, "close") else resource.stop()
                except BaseException:
                    cleanup_errors.append("browser_cleanup")
        if source_held is not None:
            try:
                if source_held.is_running():
                    source_held.resume()
            except psutil.Error:
                pass
        if service is not None:
            try:
                if not service.absent():
                    d._service_control(installed, "stop")
                    cohort.service_stopped = True
                    if (installed / "unins000.exe").is_file():
                        assert (
                            _uninstall_through_clone_completion(installed / "unins000.exe")[0] == 0
                        )
                    assert service.absent()
            except BaseException:
                cleanup_errors.append("service_cleanup")
        cohort.finish()
        for process in reversed(cohort.snapshot()):
            try:
                if cohort.is_owned_running(process):
                    process.kill()
                    process.wait(timeout=10)
            except psutil.Error:
                cleanup_errors.append("owned_process_cleanup")
        for process in reversed(fixture_processes):
            try:
                helpers._stop_owned(process)
            except BaseException:
                cleanup_errors.append("generated_fixture_cleanup")
        if (old_installed / "unins000.exe").is_file():
            try:
                assert _uninstall_through_clone_completion(old_installed / "unins000.exe")[0] == 0
                assert not (old_installed / "K5Vision.exe").exists()
            except BaseException:
                cleanup_errors.append("historical_registration_cleanup")
        if camera is not None:
            try:
                camera.close()
            except BaseException:
                cleanup_errors.append("camera_fixture_cleanup")
        if not cleanup_errors:
            for tree in (machine_profile, profile, parent):
                if tree.exists():
                    try:
                        assert (
                            tree in owned_trees
                            and (tree.stat().st_dev, tree.stat().st_ino) == owned_trees[tree]
                        )
                        d._remove_owned_temporary_tree(tree)
                    except BaseException:
                        cleanup_errors.append("owned_files_cleanup")
        diagnostic["cleanup_failures"] = cleanup_errors[:8]
        diagnostic["no_console_windows"] = not cohort.console_seen
        record_testsuite_property(
            "legacy_migration_windows_diagnostic", json.dumps(diagnostic, sort_keys=True)
        )
    assert not cleanup_errors and not cohort.failed and not cohort.console_seen
    receipt.update(
        owned_cleanup=True,
        no_console_windows=True,
        duration_seconds=round(time.monotonic() - began, 3),
    )
    verifier_path = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "verify_windows_legacy_migration_receipt.py"
    )
    spec = importlib.util.spec_from_file_location(
        "legacy_migration_receipt_verifier", verifier_path
    )
    assert spec is not None and spec.loader is not None
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    verifier.validate_receipt(
        receipt,
        baseline_head=old_head,
        head_sha=new_head,
        installer_sha256=new_hash,
        baseline_installer_sha256=old_hash,
    )
    record_testsuite_property(
        "legacy_migration_windows_receipt", json.dumps(receipt, sort_keys=True)
    )
