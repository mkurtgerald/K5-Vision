"""Actual installed SCM recording with no client, worker failure and SCM failure.

Generated cameras/accounts only. This hosted-VM lane deliberately makes no real
reboot, Windows sign-out, clock-correction, capacity or endurance claim.
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
import time
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from uuid import uuid4

import psutil
import pytest

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
    or os.getenv("K5_NATIVE_SERVICE_ACCEPTANCE") != "1"
    or not all(os.getenv(name) for name in _INPUTS),
    reason="Unattended recording qualification requires Windows and the exact installed candidate",
)


def _helpers(name):
    spec = importlib.util.spec_from_file_location(
        "k5_service_" + name, Path(__file__).with_name(name + ".py")
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _require(condition, message):
    __tracebackhide__ = True
    if not condition:
        pytest.fail(message, pytrace=False)


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _cleanup_service_registration(service, installed, desktop, cohort):
    """SCM presence, rather than a lingering uninstall directory, owns cleanup."""
    if service is None:
        if not installed.exists():
            return
        service = _helpers("windows_service_acceptance").InstalledService(installed)
    if service.absent():
        cohort.service_stopped = True
        return
    if service.status()["state"] != 1:
        desktop._service_control(installed, "stop")
        desktop._wait(
            lambda: service.status()["state"] == 1, "Cleanup SCM recorder stopped", timeout=60
        )
    cohort.service_stopped = True


def _cleanup_note(diagnostic, stage, error):
    allowed = {
        "Failed",
        "RuntimeError",
        "OSError",
        "PermissionError",
        "FileNotFoundError",
        "TimeoutExpired",
        "TimeoutError",
        "NoSuchProcess",
        "AccessDenied",
        "ProcessLookupError",
        "AssertionError",
        "Exception",
    }
    kind = type(error).__name__
    note = {"stage": stage, "exception_type": kind if kind in allowed else "Other"}
    native = getattr(error, "winerror", None)
    if type(native) is int and 0 <= native <= 65535:
        note["win32_error"] = native
    diagnostic.setdefault("cleanup_failures", [])
    if len(diagnostic["cleanup_failures"]) < 8:
        diagnostic["cleanup_failures"].append(note)


def _completed_note(receipt, observations, diagnostic):
    diagnostic["completed_checks"] = sorted(key for key, value in receipt.items() if value is True)
    diagnostic["recording_observations"] = observations
    for key in ("standard_windows_user", "startup_shutdown"):
        if key in receipt:
            diagnostic[key] = receipt[key]
    diagnostic["retained_validation"] = {
        key: receipt[key]
        for key in (
            "playback_decoded_frames",
            "export_decoded_frames",
            "export_bytes",
            "export_sha256",
        )
        if key in receipt
    }


def _failure_note(error, diagnostic, service):
    """Only public source locations, finite categories and SCM numbers leave the VM."""
    kind = type(error).__name__
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", kind):
        diagnostic["exception_type"] = kind
    allowed = {
        "test_windows_service_recording.py",
        "windows_service_acceptance.py",
        "windows_standard_user_acceptance.py",
        "windows_process_identity.py",
        "test_windows_desktop_app.py",
        "test_operator_product_runtime.py",
        "test_operator_beta_browser.py",
        "installed_settings_acceptance.py",
    }
    frames, trace = [], error.__traceback__
    while trace is not None:
        source = Path(trace.tb_frame.f_code.co_filename)
        if (
            source.name in allowed
            and source.resolve().parent == Path(__file__).resolve().parent
            and 1 <= trace.tb_lineno <= 10000
        ):
            frames.append({"file": source.name, "line": trace.tb_lineno})
        trace = trace.tb_next
    diagnostic["source_frames"] = frames[-8:]
    if service is not None:
        try:
            status = service.status()
            diagnostic["scm_at_failure"] = {
                key: status[key]
                for key in ("state", "win32_exit", "service_exit")
                if type(status.get(key)) is int and 0 <= status[key] <= 2**32 - 1
            }
        except BaseException:
            diagnostic["scm_at_failure_unavailable"] = True


def _source_fingerprint(profile):
    """Read as the elevated test administrator; do not bypass product admission."""
    path = profile / "identity" / "operator-cameras.sqlite3"
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=3) as connection:
        rows = connection.execute(
            "SELECT device_id,device_json,source_uri FROM camera_sources ORDER BY device_id LIMIT 5"
        ).fetchall()
    _require(1 <= len(rows) <= 4, "Service camera-source inventory is absent or excessive")
    return _digest(rows)


def _retained_state(client, profile):
    users = client.json("/api/v1/operator/admin/users")["users"]
    cameras = client.json("/api/v1/operator/cameras")["cameras"]
    storage = client.json("/api/v1/operator/admin/storage")
    site = client.json("/api/v1/operator/admin/activity/export")["site_id"]
    return {
        "site_hash": _digest(site),
        "source_hash": _source_fingerprint(profile),
        "users_hash": _digest(sorted(users, key=lambda row: row["id"])),
        "storage_hash": _digest(
            {
                key: storage[key]
                for key in (
                    "revision",
                    "quota_bytes",
                    "minimum_free_bytes",
                    "retention_seconds",
                    "recording_directory",
                )
            }
        ),
        "user_count": len(users),
        "camera_count": len(cameras),
    }


def _clock_health(client):
    value = client.json("/api/v1/operator/status")
    clock = value["clock"]
    _require(
        clock["status"] == "observing"
        and clock["sync_verified"] is False
        and isinstance(value["server_utc"], str)
        and value["server_utc"].endswith("Z"),
        "Service did not expose honest observed UTC after startup",
    )
    return {
        "status": clock["status"],
        "sync_verified": clock["sync_verified"],
        "server_utc": value["server_utc"],
    }


def _factor_login(client, username, password, code):
    client.token = ""
    result = client.json(
        "/api/v1/auth/login",
        method="POST",
        expected=202,
        data={"username": username, "password": password},
    )
    _require(
        result.get("mfa_required") is True and "session_token" not in result,
        "Service restart admitted a password session without the retained MFA factor",
    )
    result = client.json(
        "/api/v1/auth/mfa",
        method="POST",
        data={
            "challenge_token": result["challenge_token"],
            "code": code,
        },
    )
    token = result.get("session_token")
    _require(type(token) is str and 32 <= len(token) <= 128, "Retained MFA did not issue a session")
    client.token = token


def _growth(service, desktop, profile, client, camera_id, *, previous=None, minimum_seconds=2):
    observation = service.observe()
    if previous is not None:
        _require(
            all(
                observation[key] == previous[key]
                for key in (
                    "service_pid",
                    "service_created",
                    "backend_pid",
                    "backend_created",
                )
            ),
            "Closing the operator client replaced the independent recorder",
        )
    before = desktop._recording_bytes(profile)
    began = time.monotonic()
    desktop._wait(
        lambda: (
            time.monotonic() - began >= minimum_seconds
            and desktop._tray_recording(client, profile, camera_id, before, {})
        ),
        "Independent Session 0 camera recording progress",
        timeout=20,
    )
    after = service.observe()
    _require(
        all(after[key] == observation[key] for key in observation),
        "Recorder identity changed during the recording observation",
    )
    return {
        **after,
        "recording_bytes_before": before,
        "recording_bytes_after": desktop._recording_bytes(profile),
    }


def _stop_during_startup(service, profile, desktop, cohort, diagnostic):
    """Stop a genuine new pre-ready host, without injecting a pause or test mode."""
    diagnostic["stage"] = "normal_stop_before_startup_regression"
    _, backend = service.processes()
    old_host = psutil.Process(backend.ppid())
    previous = (old_host.pid, service.native_birth(old_host))
    desktop._service_control(service.installed, "stop")
    desktop._wait(
        lambda: service.status()["state"] == 1,
        "Normal service stop before cold restart",
        timeout=60,
    )
    cohort.service_stopped = True
    desktop._wait_owned_cleanup(cohort, diagnostic=diagnostic)
    diagnostic["stage"] = "native_restart_before_readiness"
    service.start_without_readiness()
    cohort.service_stopped = False
    snapshot_helper = _helpers("windows_service_acceptance")

    def pre_ready():
        snapshot = snapshot_helper.installer_failure_snapshot(service.installed, profile)
        envelopes = [
            envelope for log in snapshot.get("logs", []) for envelope in log.get("bootstrap", [])
        ]
        diagnostic["startup_stop_observation"] = {
            "scm": snapshot.get("scm", {}),
            "processes": snapshot.get("processes", {}),
            "bootstrap": envelopes[-3:],
        }
        if not envelopes:
            return False
        event = envelopes[-1]
        _require(
            "failure_type" not in event, "New recorder host failed before the startup-stop request"
        )
        _require(
            event["k5_recorder_bootstrap"] not in {"account_required", "serve_start", "running"},
            "New recorder reached readiness before the startup-stop observation",
        )
        if event["k5_recorder_bootstrap"] not in {"token", "code_admission", "payload_verify"}:
            return False
        _require(
            (event["host_pid"], event["host_created_100ns"]) != previous,
            "Startup-stop proof observed the previous recorder host",
        )
        cohort.add(psutil.Process(snapshot["scm"]["pid"]), role="service")
        return event

    observed = desktop._wait(pre_ready, "Native new-host pre-readiness bootstrap", timeout=15)
    diagnostic["stage"] = "scm_stop_during_startup"
    began = time.monotonic()
    service.request_stop()
    desktop._wait(
        lambda: service.status()["state"] == 1,
        "SCM stopped during host startup",
        timeout=max(0.1, 70 - (time.monotonic() - began)),
    )
    cohort.service_stopped = True
    desktop._wait_owned_cleanup(
        cohort, diagnostic=diagnostic, timeout=max(0.1, min(10, 70 - (time.monotonic() - began)))
    )
    elapsed = time.monotonic() - began
    _require(elapsed < 70, "Stopping during startup exceeded the existing stop budget")
    return {
        "native_start_without_readiness": True,
        "new_host_verified": True,
        "previous_host_pid": previous[0],
        "previous_host_created_100ns": previous[1],
        "host_pid": observed["host_pid"],
        "host_created_100ns": observed["host_created_100ns"],
        "observed_stage": observed["k5_recorder_bootstrap"],
        "scm_stopped": True,
        "owned_children_exited": True,
        "stop_seconds": round(elapsed, 3),
    }


def test_native_windows_unattended_service_recording_and_recovery(record_testsuite_property):
    __tracebackhide__ = True
    started = time.monotonic()
    desktop, runtime, browser_helpers, settings, scm = (
        _helpers(name)
        for name in (
            "test_windows_desktop_app",
            "test_operator_product_runtime",
            "test_operator_beta_browser",
            "installed_settings_acceptance",
            "windows_service_acceptance",
        )
    )
    helpers = runtime._helpers()
    browser_api = pytest.importorskip("playwright.sync_api")
    _require(version("playwright") == "1.58.0", "Reviewed Playwright version is absent")
    setup = Path(os.environ["K5_DESKTOP_SETUP"]).resolve(strict=True)
    setup_hash = hashlib.sha256(setup.read_bytes()).hexdigest()
    _require(
        setup_hash == os.environ["K5_DESKTOP_SETUP_SHA256"],
        "Service gate received a different Setup",
    )
    parent = Path(os.environ["LOCALAPPDATA"]) / ("K5 Service Proof-" + uuid4().hex)
    parent.mkdir(mode=0o700)
    installed, profile = desktop._machine_paths()
    api, cohort = desktop._Windows(), None
    cohort = desktop._Cohort(api)
    media, ffmpeg, ffprobe = (
        helpers._tool(name)
        for name in (
            "K5_MEDIAMTX_DONOR_EXE",
            "K5_DONOR_FFMPEG",
            "K5_DONOR_FFPROBE",
        )
    )
    processes, reservations = [], []
    service = camera = app = form = playwright = browser = context = None
    failed = cleanup_failed = False
    diagnostic, receipt, observations = {"stage": "graphical_install"}, {}, {}
    try:
        desktop._install_gui(setup, installed, api, diagnostic=diagnostic)
        desktop._verify_installed_worker_bundle(installed)
        service = scm.InstalledService(installed)
        receipt["scm"] = service.configuration()
        receipt.update(
            actual_gui_installer=True,
            installed_worker_bundle_verified=True,
            actual_scm_readback=True,
        )
        diagnostic["stage"] = "graphical_administrator_setup"
        app = desktop._start_app(installed, profile, 8000, cohort)
        form = desktop._wait(lambda: api.form(app.pid), "Service account setup window", process=app)
        desktop._wait(
            lambda: api.api.IsWindowEnabled(api.control(form, desktop._CREATE)),
            "Service account setup controls",
            process=app,
        )
        username, password = "service-fixture-admin", secrets.token_urlsafe(32)
        api.set_text(form, desktop._USERNAME, username)
        api.set_text(form, desktop._PASSWORD, password)
        api.set_text(form, desktop._CONFIRM, password)
        api.click(api.control(form, desktop._CREATE))
        client = runtime._Client(8000)
        desktop._wait(
            lambda: desktop._healthy(client, diagnostic),
            "SCM backend startup",
            process=app,
            timeout=60,
        )
        desktop._observer_reauthenticate(runtime, client, username, password, diagnostic)
        administrator_id = client.json("/api/v1/auth/me")["id"]
        _require(
            api.control_text(form, desktop._PASSWORD) == ""
            and api.control_text(form, desktop._CONFIRM) == "",
            "Setup retained password controls",
        )
        receipt["actual_gui_account_setup"] = True
        diagnostic["stage"] = "generated_authenticated_camera"
        reservations, ports = helpers._reserve_ports(1)
        for reservation in reservations:
            reservation.close()
        uri = f"rtsp://127.0.0.1:{ports[0]}/generated"
        camera = _helpers("onvif_camera_fixture").OnvifCameraFixture(
            host="127.0.0.1",
            stream_uri=uri,
            clock_skew_seconds=1200,
        )
        publisher_credentials = ("generated-service-publisher", secrets.token_urlsafe(24))
        generated, _ = desktop._generate_fixture(
            parent,
            media,
            ffmpeg,
            ports[0],
            helpers,
            camera_credentials=(camera.username, camera.password),
            publisher_credentials=publisher_credentials,
        )
        processes.extend(generated)
        playwright = browser_api.sync_playwright().start()
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        browser_version = browser.version
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
        _require(login.value.status == 200, "Installed service browser login failed")
        client.token = login.value.json()["session_token"]
        page.locator("#workspace").wait_for(state="visible")
        enrolled, _ = browser_helpers._onvif_enroll(
            page,
            name="Generated unattended camera",
            fixture=camera,
            diagnostic=diagnostic,
        )
        camera_id = enrolled["device_id"]
        receipt["actual_edge_ui"] = True
        diagnostic["stage"] = "persistent_authority_and_factor"
        client.json(
            "/api/v1/operator/admin/users",
            method="POST",
            expected=201,
            data={
                "username": "retained-service-viewer",
                "display_name": "Retained fixture viewer",
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
        enrollment = client.json(
            "/api/v1/operator/security/mfa/enroll",
            method="POST",
            data={"current_password": password},
        )
        secret, enrollment_counter = enrollment["secret"], int(time.time()) // 30
        confirmed = client.json(
            "/api/v1/operator/security/mfa/confirm",
            method="POST",
            data={
                "enrollment_token": enrollment["enrollment_token"],
                "code": settings._totp(secret),
            },
        )
        recovery = confirmed["recovery_codes"]
        _factor_login(client, username, password, recovery[0])
        before = _retained_state(client, profile)
        receipt["clock_health_before_restart"] = _clock_health(client)
        observations = {
            "initial": _growth(service, desktop, profile, client, camera_id, minimum_seconds=8)
        }
        # Disconnect only the generated publisher to obtain genuinely closed
        # recording files via the existing exclusive Win32 read helper.
        helpers._stop_owned(generated[-1])
        closed = desktop._wait(
            lambda: desktop._closed_hashes(profile), "Closed service recording files", timeout=20
        )
        retained_hashes = sorted(value.hex() for value in closed.values())
        retained_history = client.json(f"/api/v1/operator/cameras/{camera_id}/history")["spans"]
        retained_windows = [
            (
                datetime.fromisoformat(span["started_at"].replace("Z", "+00:00")).timestamp(),
                span["duration_seconds"],
            )
            for span in retained_history
            if span["duration_seconds"] >= 4
        ]
        _require(retained_windows, "Pre-failure closed footage had no real playable interval")
        publisher = desktop._publish_fixture(
            parent, ffmpeg, uri, helpers, credentials=publisher_credentials
        )
        processes.append(publisher)
        diagnostic["stage"] = "client_and_browser_exit"
        context.close()
        context = None
        browser.close()
        browser = None
        api.click(api.control(form, desktop._STOP))
        _require(app.wait(timeout=15) == 0, "Closing the native client failed")
        _require(not api.api.IsWindow(form), "Native client window survived explicit quit")
        observations["client_closed"] = _growth(
            service,
            desktop,
            profile,
            client,
            camera_id,
            previous=observations["initial"],
        )
        receipt["client_quit_recording_continues"] = True
        diagnostic["stage"] = "ordinary_windows_user_ipc"
        probe_before = desktop._recording_bytes(profile)
        probe = _helpers("windows_standard_user_acceptance").run_probe(
            installed, parent, diagnostic
        )
        observations["client_closed"] = _growth(
            service,
            desktop,
            profile,
            client,
            camera_id,
            previous=observations["client_closed"],
        )
        receipt["standard_windows_user"] = {
            **probe,
            "recording_bytes_before": probe_before,
            "recording_bytes_after": desktop._recording_bytes(profile),
            "service_identity_unchanged": True,
        }
        diagnostic["stage"] = "embedded_backend_failure"
        service.kill_owned(observations["client_closed"], backend=True)

        def recovered(previous):
            current = service.observe()
            return (
                current
                if all(
                    (current[p + "_pid"], current[p + "_created"])
                    != (previous[p + "_pid"], previous[p + "_created"])
                    for p in ("service", "backend")
                )
                else False
            )

        desktop._wait(
            lambda: recovered(observations["client_closed"]),
            "Actual SCM recovery after embedded backend failure",
            timeout=45,
        )
        cohort.add(service.processes()[0], role="service")
        desktop._wait(
            lambda: desktop._healthy(client, diagnostic), "Recovered backend health", timeout=30
        )
        _factor_login(client, username, password, recovery[1])
        observations["backend_recovered"] = _growth(service, desktop, profile, client, camera_id)
        receipt["backend_failure_scm_recovery"] = True
        diagnostic["stage"] = "scm_wrapper_failure"
        service.kill_owned(observations["backend_recovered"], backend=False)
        desktop._wait(
            lambda: recovered(observations["backend_recovered"]),
            "Actual SCM recovery after wrapper failure",
            timeout=60,
        )
        cohort.add(service.processes()[0], role="service")
        desktop._wait(
            lambda: desktop._healthy(client, diagnostic), "Recovered SCM health", timeout=30
        )
        desktop._wait(
            lambda: int(time.time()) // 30 > enrollment_counter,
            "Next independent TOTP counter",
            timeout=32,
        )
        _factor_login(client, username, password, settings._totp(secret))
        observations["wrapper_recovered"] = _growth(service, desktop, profile, client, camera_id)
        receipt.update(
            wrapper_failure_scm_recovery=True,
            mfa_required_after_restart=True,
            service_token_mfa_decrypted_after_restart=True,
            observations=observations,
        )
        after = _retained_state(client, profile)
        _require(
            before == after,
            "Service recovery changed site, sources, users, roles or storage policy",
        )
        _require(
            desktop._hashes_preserved(closed), "Service recovery changed retained closed footage"
        )
        receipt.update(
            retained_file_hashes_before=retained_hashes,
            retained_file_hashes_after=sorted(
                hashlib.sha256(p.read_bytes()).hexdigest() for p in closed
            ),
            retained_user_count=after["user_count"],
            retained_camera_count=after["camera_count"],
            clock_health_after_restart=_clock_health(client),
        )
        for name in ("site", "source", "users", "storage"):
            receipt[name + "_hash_before"], receipt[name + "_hash_after"] = (
                before[name + "_hash"],
                after[name + "_hash"],
            )
        receipt.update(
            retained_files_preserved=True,
            retained_site_identity=True,
            retained_camera_sources=True,
            retained_users_roles=True,
            retained_storage_policy=True,
        )
        diagnostic["stage"] = "retained_playback_and_actual_download"
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        context = browser.new_context(
            viewport={"width": 1440, "height": 900}, accept_downloads=True
        )
        page = context.new_page()
        page.set_default_timeout(25_000)
        signed_in = settings._mfa_login(
            page,
            client.origin,
            {
                "username": username,
                "password": password,
                "id": administrator_id,
            },
            recovery[2],
        )
        client.token = signed_in["session_token"]
        browser_helpers._open_camera_recordings(page, camera_id)
        receipt["playback_decoded_frames"] = browser_helpers._timeline_play(
            page,
            "red",
            device_id=camera_id,
            diagnostic=diagnostic,
        )
        selected_utc = (
            datetime.fromisoformat(page.locator("#playback-start").input_value())
            .replace(tzinfo=UTC)
            .timestamp()
        )
        _require(
            any(
                start <= selected_utc and selected_utc + 2 <= start + duration
                for start, duration in retained_windows
            ),
            "Post-recovery playback did not select the retained pre-failure footage",
        )
        export = {}
        receipt["export_decoded_frames"] = browser_helpers._download_recording(
            page,
            ffprobe,
            parent / "retained service export.mp4",
            helpers,
            "red",
            evidence=export,
        )
        receipt.update(
            export_bytes=export["bytes"],
            export_sha256=export["sha256"],
            retained_playback_decoded=True,
            retained_export_decoded=True,
        )
        context.close()
        context = None
        browser.close()
        browser = None
        receipt["startup_shutdown"] = _stop_during_startup(
            service, profile, desktop, cohort, diagnostic
        )
        diagnostic["stage"] = "service_uninstall_preserves_data"
        result = subprocess.run(
            [str(installed / "unins000.exe"), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=60,
            check=False,
        )
        _require(result.returncode == 0, "Native service uninstall failed")
        desktop._wait(service.absent, "SCM recorder registration removed", timeout=10)
        _require(
            desktop._hashes_preserved(closed) and profile.is_dir(),
            "Uninstall removed private state or retained recordings",
        )
        _require(
            _digest(desktop._profile_identity(profile).site_id) == before["site_hash"]
            and _source_fingerprint(profile) == before["source_hash"],
            "Uninstall changed retained machine identity or camera sources",
        )
        receipt.update(service_removed_on_uninstall=True, uninstall_preserves_profile=True)
    except BaseException as error:
        failed = True
        _completed_note(receipt, observations, diagnostic)
        if service is None:
            diagnostic["installation_snapshot"] = scm.installer_failure_snapshot(installed, profile)
        _failure_note(error, diagnostic, service)
        diagnostic["failure_kind"] = (
            "assertion" if isinstance(error, pytest.fail.Exception) else "exception"
        )
    finally:
        for label, resource in (
            ("browser_context", context),
            ("browser", browser),
            ("playwright", playwright),
        ):
            if resource is not None:
                try:
                    resource.stop() if resource is playwright else resource.close()
                except BaseException as error:
                    cleanup_failed = True
                    _cleanup_note(diagnostic, label, error)
        try:
            _cleanup_service_registration(service, installed, desktop, cohort)
        except BaseException as error:
            cleanup_failed = True
            _cleanup_note(diagnostic, "service_registration", error)
        cohort.finish()
        if cohort.failed:
            cleanup_failed = True
            _cleanup_note(diagnostic, "cohort_observer", RuntimeError())
        try:
            owned = cohort.snapshot()
        except BaseException as error:
            cleanup_failed = True
            _cleanup_note(diagnostic, "cohort_snapshot", error)
            owned = []
        diagnostic["process_ownership"] = cohort.ownership_diagnostic()
        for process in reversed(owned):
            try:
                cohort.terminate_owned(process)
            except psutil.NoSuchProcess:
                pass
            except BaseException as error:
                cleanup_failed = True
                _cleanup_note(diagnostic, "cohort_process", error)
        for process in reversed(processes):
            try:
                helpers._stop_owned(process)
            except BaseException as error:
                cleanup_failed = True
                _cleanup_note(diagnostic, "generated_source", error)
        if camera is not None:
            try:
                camera.close()
            except BaseException as error:
                cleanup_failed = True
                _cleanup_note(diagnostic, "generated_camera", error)
        for reservation in reservations:
            try:
                reservation.close()
            except BaseException as error:
                cleanup_failed = True
                _cleanup_note(diagnostic, "listener", error)
        try:
            desktop._remove_owned_temporary_tree(parent)
        except BaseException as error:
            cleanup_failed = True
            _cleanup_note(diagnostic, "temporary_tree", error)
    if failed or cleanup_failed:
        _completed_note(receipt, observations, diagnostic)
        diagnostic["cleanup_failed"] = cleanup_failed
    _require(
        not failed and not cleanup_failed,
        "Installed unattended service qualification failed: "
        + json.dumps(diagnostic, sort_keys=True),
    )
    receipt["process_ownership"] = cohort.ownership_receipt()
    receipt.update(
        schema_version=1,
        head_sha=os.environ["K5_DESKTOP_HEAD_SHA"],
        installer_sha256=setup_hash,
        platform=platform.system(),
        duration_seconds=round(time.monotonic() - started, 3),
        browser_channel="msedge",
        browser_version=browser_version,
        playwright_version="1.58.0",
        native_windows_execution=True,
        generated_media_only=True,
        owned_cleanup=True,
        no_console_windows=not cohort.console_seen and not cohort.failed,
    )
    for name in (
        "actual_reboot_before_login",
        "actual_windows_logoff",
        "actual_clock_corrections",
        "service_token_oidc_secret_after_restart",
        "physical_camera_acceptance",
        "capacity_acceptance",
        "endurance_acceptance",
        "mocked_service",
        "mocked_gui",
        "user_terminal_required",
    ):
        receipt[name] = False
    _require(
        receipt["no_console_windows"] and receipt["duration_seconds"] < 360,
        "Installed service qualification exceeded its native execution boundary",
    )
    record_testsuite_property("service_windows_receipt", json.dumps(receipt, sort_keys=True))
