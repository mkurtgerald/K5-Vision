"""Focused real Windows UTC-step acceptance composed with the full GUI witness.

The primary same-Setup GUI witness remains mandatory. This disposable VM
recreates the real local camera/recording/inference preconditions and runs the
unchanged clock phase; it does not claim the primary's administration or
lifecycle permutations as locally executed work.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import platform
import secrets
import shutil
import time
from importlib.metadata import version
from pathlib import Path
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
    "K5_OPERATOR_PERSON_FIXTURE",
    "K5_VM_CLOCK_PRIMARY_XML",
    "K5_VM_CLOCK_PRIMARY_RECEIPT",
    "K5_VM_CLOCK_PRIMARY_ARTIFACT_ID",
    "K5_VM_CLOCK_PRIMARY_RUN_ID",
    "K5_VM_CLOCK_PRIMARY_RUN_ATTEMPT",
    "K5_VM_CLOCK_SETUP_ARTIFACT_ID",
)
pytestmark = pytest.mark.skipif(
    os.name != "nt"
    or os.getenv("K5_VM_CLOCK_ACCEPTANCE") != "1"
    or not all(os.getenv(name) for name in _INPUTS),
    reason="Dedicated clock acceptance requires an opted-in Windows VM and exact primary evidence",
)


def _module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _storage_policy(client, require):
    value = client.json("/api/v1/operator/admin/storage")
    expected = {
        "revision": 0,
        "quota_bytes": 21474836480,
        "minimum_free_bytes": 1073741824,
        "retention_seconds": 86400,
    }
    require(
        all(type(value.get(key)) is int and value[key] == item for key, item in expected.items()),
        "VM clock default storage policy changed",
    )
    return {key: value[key] for key in expected}


def _clock_entry_state(client, camera_ids, require):
    cameras = client.json("/api/v1/operator/cameras")["cameras"]
    require(
        len(cameras) == 4 and {camera["device_id"] for camera in cameras} == set(camera_ids),
        "VM clock local inventory is not the four enrolled sources",
    )
    by_id = {camera["device_id"]: camera for camera in cameras}
    require(
        all(
            by_id[identifier].get("status") == "live"
            and by_id[identifier].get("recording") is True
            and by_id[identifier].get("recording_enabled") is True
            for identifier in camera_ids[:3]
        )
        and by_id[camera_ids[3]].get("status") == "live"
        and by_id[camera_ids[3]].get("recording") is False
        and by_id[camera_ids[3]].get("recording_enabled") is False,
        "VM clock local live/recording preconditions changed",
    )
    require(
        by_id[camera_ids[0]].get("connection_type") == "rtsp",
        "VM clock primary source protocol changed",
    )
    return [by_id[identifier]["recording_enabled"] for identifier in camera_ids]


def test_native_windows_vm_clock_qualification(record_testsuite_property):
    __tracebackhide__ = True
    started = time.monotonic()
    desktop = _module("k5_clock_desktop", Path(__file__).with_name("test_windows_desktop_app.py"))
    admission = _module(
        "k5_clock_admission",
        Path(__file__).parents[2] / "scripts" / "verify_windows_vm_clock_receipt.py",
    )
    require = desktop._require
    setup = Path(os.environ["K5_DESKTOP_SETUP"]).resolve(strict=True)
    with setup.open("rb") as stream:
        setup_sha256 = hashlib.file_digest(stream, "sha256").hexdigest()
    require(
        setup_sha256 == os.environ["K5_DESKTOP_SETUP_SHA256"],
        "VM clock exact installer identity was not admitted",
    )
    # Admission happens before creating any installed state or changing time.
    primary_binding = admission.admit_primary(
        primary_report=Path(os.environ["K5_VM_CLOCK_PRIMARY_XML"]),
        primary_receipt=Path(os.environ["K5_VM_CLOCK_PRIMARY_RECEIPT"]),
        package=setup.parent / "desktop-package.json",
        expected_head=os.environ["K5_DESKTOP_HEAD_SHA"],
        installer_sha256=setup_sha256,
        expected_run_id=int(os.environ["K5_VM_CLOCK_PRIMARY_RUN_ID"]),
        expected_run_attempt=int(os.environ["K5_VM_CLOCK_PRIMARY_RUN_ATTEMPT"]),
        expected_primary_artifact_id=int(os.environ["K5_VM_CLOCK_PRIMARY_ARTIFACT_ID"]),
        expected_setup_artifact_id=int(os.environ["K5_VM_CLOCK_SETUP_ARTIFACT_ID"]),
    )
    api = desktop._Windows()
    runtime = desktop._helpers("test_operator_product_runtime")
    browser_helpers = desktop._helpers("test_operator_beta_browser")
    helpers = runtime._helpers()
    browser_api = pytest.importorskip("playwright.sync_api")
    require(version("playwright") == "1.58.0", "Expected reviewed Playwright version is absent")
    media, ffmpeg, ffprobe = (
        helpers._tool(name)
        for name in ("K5_MEDIAMTX_DONOR_EXE", "K5_DONOR_FFMPEG", "K5_DONOR_FFPROBE")
    )
    parent = Path(os.environ["LOCALAPPDATA"]) / ("K5 Clock Proof-" + uuid4().hex)
    identity_state.validate_new_identity_directory(parent)
    parent.mkdir(mode=0o700)
    installed, profile = parent / "installed app with spaces", parent / "profile with spaces"
    username, password = "fixture-admin", secrets.token_urlsafe(32)
    processes, reservations = [], []
    onvif_cameras, publishers, sources = [], [], []
    publisher_credentials = ("generated-fixture-publisher", secrets.token_urlsafe(24))
    cohort = desktop._Cohort(api)
    playwright = browser = context = page = form = app = None
    stage, failure, cleanup_failed = "installer", None, False
    diagnostic = {}
    readiness = {"health_http_status": 0}
    receipt = {
        "schema_version": 2,
        "scope": "vm-clock-dedicated",
        "platform": platform.system(),
        "head_sha": os.environ["K5_DESKTOP_HEAD_SHA"],
        "harness_head_sha": os.environ["K5_DESKTOP_HEAD_SHA"],
        "installer_sha256": setup_sha256,
        "payload_manifest_sha256": primary_binding["manifest_sha256"],
        "native_windows_execution": True,
        "generated_media_only": True,
        "physical_camera_acceptance": False,
        "capacity_acceptance": False,
        "endurance_acceptance": False,
        "windows_service": False,
        "mocked_gui": False,
        "user_terminal_required": False,
        "browser_channel": "msedge",
        "playwright_version": "1.58.0",
        "primary_binding": primary_binding,
    }
    try:
        desktop._install_gui(setup, installed, api, diagnostic=readiness)
        require((installed / "K5Vision.exe").is_file(), "Installed desktop executable is absent")
        require((installed / "runtime/python.exe").is_file(), "Embedded Python was not installed")
        receipt["actual_gui_installer"] = True
        reservations, ports = helpers._reserve_ports(5)
        port, upstream_ports = ports[0], ports[1:]
        for listener in reservations:
            listener.close()
        stage = "first_account_screen"
        app = desktop._start_app(installed, profile, port, cohort)
        form = desktop._wait(lambda: api.form(app.pid), "First account window", process=app)
        desktop._wait(
            lambda: (
                api.api.IsWindowVisible(api.control(form, desktop._CREATE))
                and api.api.IsWindowEnabled(api.control(form, desktop._CREATE))
            ),
            "Create account button",
            process=app,
        )
        api.set_text(form, desktop._USERNAME, username)
        api.set_text(form, desktop._PASSWORD, password)
        api.set_text(form, desktop._CONFIRM, password)
        api.click(api.control(form, desktop._CREATE))
        client = runtime._Client(port)
        stage = "embedded_product_startup"
        desktop._wait(
            lambda: desktop._healthy(client, readiness),
            "Desktop embedded product startup",
            process=app,
            timeout=60,
        )
        runtime._login(client, username, password)
        require(
            client.json("/api/v1/auth/me").get("role") == "administrator",
            "GUI account did not authorize an administrator",
        )
        require(
            api.control_text(form, desktop._PASSWORD) == ""
            and api.control_text(form, desktop._CONFIRM) == "",
            "Desktop left the initial password in its input controls",
        )
        receipt.update(actual_gui_account_setup=True, administrator_login=True)
        identity = identity_state.load_identity_state(profile / "identity")
        python_processes = [
            process
            for process in cohort.snapshot()
            if process.is_running() and process.name().casefold() in {"python.exe", "pythonw.exe"}
        ]
        require(bool(python_processes), "Desktop did not start its embedded Python process")
        require(
            all(
                Path(process.exe()).resolve().is_relative_to(installed)
                for process in python_processes
            ),
            "Desktop used an external Python installation",
        )
        receipt["embedded_python_independent_of_path"] = True
        stage = "generated_camera_fixture"
        for index, ((_, host, color), upstream_port) in enumerate(
            zip(desktop._DESKTOP_CAMERAS, upstream_ports, strict=True)
        ):
            camera_fixture = desktop._helpers("onvif_camera_fixture").OnvifCameraFixture(
                host=host,
                stream_uri=f"rtsp://{host}:{upstream_port}/generated",
                clock_skew_seconds=1200 if index % 2 == 0 else -1200,
                clock_mode="NTP",
            )
            onvif_cameras.append(camera_fixture)
            generated, source = desktop._generate_fixture(
                parent,
                media,
                ffmpeg,
                upstream_port,
                helpers,
                host=host,
                color=color,
                camera_credentials=(camera_fixture.username, camera_fixture.password)
                if index == 0
                else None,
                publisher_credentials=publisher_credentials if index == 0 else None,
            )
            processes.extend(generated)
            publishers.append(generated[-1])
            sources.append(source)
        source_uri, onvif_camera = sources[0], onvif_cameras[0]
        stage = "actual_browser_enrollment"
        playwright = browser_api.sync_playwright().start()
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        receipt["browser_version"] = browser.version
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()
        page.set_default_timeout(25_000)
        page.goto(client.origin + "/operator/")
        page.locator("#username").fill(username)
        page.locator("#password").fill(password)
        with page.expect_response(
            lambda response: (
                response.request.method == "POST" and response.url.endswith("/api/v1/auth/login")
            )
        ) as browser_login:
            page.locator("#sign-in-button").click()
        require(browser_login.value.status == 200, "Desktop browser login failed")
        browser_token = browser_login.value.json().get("session_token")
        require(
            isinstance(browser_token, str) and 32 <= len(browser_token) <= 128,
            "Desktop browser login did not return a bounded session",
        )
        client.token = browser_token
        page.locator("#workspace").wait_for(state="visible")
        browser_helpers._assert_recorder_time_health(page, client)
        receipt["recorder_time_health_displayed"] = True
        camera_ids = []
        for (name, _, _), camera_fixture in zip(
            desktop._DESKTOP_CAMERAS, onvif_cameras, strict=True
        ):
            enrolled, _ = browser_helpers._onvif_enroll(
                page,
                name=name,
                fixture=camera_fixture,
                reject_wrong=False,
                run_discovery=False,
            )
            camera_ids.append(enrolled["device_id"])
        require(len(set(camera_ids)) == 4, "Installed NVR merged distinct camera sources")
        camera_id = camera_ids[0]
        camera_directories = [
            profile / "recordings" / MediaMTXBinding(identity.site_id, UUID(device)).path_name
            for device in camera_ids
        ]
        receipt.update(
            camera_count=4,
            onvif_credential_profile_enrollment=True,
            no_manual_stream_uri=True,
        )
        # Preserve the original clock phase's primary source protocol, without
        # repeating the primary GUI's separate fifth-channel permutations.
        stage = "primary_source_reconnect"
        reconnected = browser_helpers._rtsp_enroll(
            page,
            name=desktop._DESKTOP_CAMERAS[0][0],
            source_uri=source_uri,
            username=onvif_camera.username,
            password=onvif_camera.password,
            device_id=camera_id,
            reject_wrong=False,
        )
        require(
            reconnected.get("device_id") == camera_id
            and reconnected.get("connection_type") == "rtsp",
            "Clock primary reconnect changed its identity or protocol",
        )
        stage = "four_camera_preconditions"
        receipt["wall_decoded_frames"] = browser_helpers._wall_frames(
            page, camera_ids, [color for _, _, color in desktop._DESKTOP_CAMERAS]
        )
        receipt["four_camera_wall"] = True
        receipt["actual_edge_login_enrollment_live"] = True
        before = [desktop._recording_bytes(profile, root) for root in camera_directories]
        desktop._wait(
            lambda: all(
                desktop._recording_bytes(profile, root) > previous
                for root, previous in zip(camera_directories, before, strict=True)
            ),
            "Installed four-camera recording progress",
            process=app,
        )
        receipt["camera_recording_bytes_before"] = before
        receipt["camera_recording_bytes_after"] = [
            desktop._recording_bytes(profile, root) for root in camera_directories
        ]
        receipt["four_camera_recording_progress"] = True
        # This is the original phase-entry topology: all live, fourth paused.
        browser_helpers._open_camera_watch(page, camera_ids[3])
        preference_route = f"/api/v1/operator/cameras/{camera_ids[3]}/recording"
        with page.expect_response(
            lambda response: (
                response.request.method == "PUT" and response.url.endswith(preference_route)
            )
        ) as preference:
            page.locator("#toggle-recording").click()
        require(preference.value.status == 200, "Installed clock recording choice failed")
        desktop._wait(
            lambda: page.locator("#toggle-recording").get_attribute("data-enabled") == "false",
            "Installed recording choice is paused",
            process=app,
        )
        browser_helpers._decoded_color(page, "live-video", "yellow")
        browser_helpers._open_camera_watch(page, camera_id)
        browser_helpers._decoded_color(page, "live-video", "red")
        _clock_entry_state(client, camera_ids, require)
        receipt["storage_policy"] = _storage_policy(client, require)
        stage = "installed_continuous_person_analytics"
        analytics, event = desktop._installed_analytics_journey(
            page=page,
            client=client,
            camera_id=camera_id,
            parent=parent,
            processes=processes,
            publishers=publishers,
            source_uri=source_uri,
            ffmpeg=ffmpeg,
            helpers=helpers,
            browser_helpers=browser_helpers,
            app=app,
            cohort=cohort,
            publisher_credentials=publisher_credentials,
            diagnostic=diagnostic,
        )
        receipt.update(analytics)
        receipt["recording_enabled_before_clock"] = _clock_entry_state(client, camera_ids, require)
        stage = "actual_vm_clock_changes"
        receipt["vm_clock"] = desktop._vm_clock_phase(
            page=page,
            context=context,
            client=client,
            profile=profile,
            camera_id=camera_id,
            parent=parent,
            processes=processes,
            source_uri=source_uri,
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
            helpers=helpers,
            browser_helpers=browser_helpers,
            app=app,
            cohort=cohort,
            setup_sha256=setup_sha256,
            diagnostic=diagnostic,
            publisher=publishers[0],
            camera_directory=camera_directories[0],
            publisher_credentials=publisher_credentials,
            analytics_event=event,
        )
        record_testsuite_property(
            "vm_clock_windows_phase", json.dumps(receipt["vm_clock"], sort_keys=True)
        )
        receipt["host_clock_step_acceptance"] = True
        require(
            _storage_policy(client, require) == receipt["storage_policy"],
            "VM clock changed its default storage policy",
        )
        stage = "graceful_owned_process_cleanup"
        context.close()
        context = None
        browser.close()
        browser = None
        desktop._stop_app(app, form, api, cohort, diagnostic=diagnostic)
        require(not cohort.console_seen and not cohort.failed, "Desktop opened a console window")
        receipt["no_console_windows"] = True
        stage = "complete"
    except BaseException as error:
        try:
            diagnostic["failure_frames"] = desktop._native_failure_frames(error)
        except BaseException:
            diagnostic["failure_frame_probe_failed"] = True
        if page is not None and not any(
            key in diagnostic
            for key in ("analytics_event_failure_ui", "analytics_event_ui_probe_failed")
        ):
            try:
                diagnostic["failure_ui"] = desktop._native_failure_ui(page)
            except BaseException:
                diagnostic["failure_ui_probe_failed"] = True
        failure = {
            "TimeoutExpired": "process_timeout",
            "TimeoutError": "browser_timeout",
            "Failed": "acceptance_assertion",
            "AssertionError": "fixture_assertion",
            "Error": "browser_error",
        }.get(type(error).__name__, "qualification_error")
    finally:
        cohort.finish()
        for resource in (context, browser, playwright):
            if resource is not None:
                try:
                    resource.stop() if resource is playwright else resource.close()
                except Exception:
                    cleanup_failed = True
        for process in reversed(cohort.snapshot()):
            try:
                if process.is_running():
                    process.kill()
                    process.wait(timeout=10)
            except psutil.NoSuchProcess:
                pass
            except Exception:
                cleanup_failed = True
        for process in reversed(processes):
            try:
                helpers._stop_owned(process)
            except Exception:
                cleanup_failed = True
        for camera_fixture in onvif_cameras:
            try:
                camera_fixture.close()
            except Exception:
                cleanup_failed = True
        for listener in reservations:
            listener.close()
        if parent.exists():
            try:
                shutil.rmtree(parent)
            except Exception:
                cleanup_failed = True
        diagnostic.update(
            stage=stage, failure_code=failure, cleanup_failed=cleanup_failed, **readiness
        )
        record_testsuite_property(
            "vm_clock_windows_diagnostic", json.dumps(diagnostic, sort_keys=True)
        )
        if failure is not None or cleanup_failed:
            print("VM clock qualification diagnostic: " + json.dumps(diagnostic, sort_keys=True))
    require(failure is None, "VM clock qualification failed at " + stage + ": " + str(failure))
    require(not cleanup_failed, "Owned VM clock qualification cleanup failed")
    require(not cohort.console_seen and not cohort.failed, "Desktop opened a console window")
    elapsed = time.monotonic() - started
    require(elapsed < 360, "VM clock qualification exceeded its bounded duration")
    receipt["duration_seconds"] = round(elapsed, 2)
    receipt["owned_cleanup"] = True
    record_testsuite_property("vm_clock_windows_receipt", json.dumps(receipt, sort_keys=True))
