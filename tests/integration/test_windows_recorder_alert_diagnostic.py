"""Exact installed alert persistence component experiment; never a release receipt."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import subprocess
import time
from datetime import datetime
from importlib.metadata import version
from pathlib import Path
from uuid import UUID, uuid4

import pytest

PRODUCT_HEAD = "23a24e57679534d77a5ea3881902f24a2d8e8e5f"
SETUP_SHA = "ee8868069902cc2112b619df3240f10c6c5214acfa91846e14b295e20dd98935"
MONITOR_SHA = "0f8b62be95e772de55d2bccfc489d55e1f14e3561bba8198f1c8726a39a688d0"
PREFIX = "diagnostic_only_installed_recorder_alert_"
SOURCE_PINS = {
    "test_windows_service_recording.py": (
        "e49726affabb07a370f8c94885941ae26b62e88e0e0b8daf7a8dadfa80dba317"
    ),
    "test_windows_desktop_app.py": (
        "066a9cabf28b578cae675dc31a8dfeb04c7957a02e20a304d2cfb5b5aedcaaac"
    ),
    "test_operator_product_runtime.py": (
        "7e662e77bb3c072119d0ae545d2193c9f909bf0229478659c5698272aca20050"
    ),
    "test_operator_beta_browser.py": (
        "e2e39ef8fe0e2f23624612323022cabedb7c0d6d4da33c4a5836a71deaca54de"
    ),
    "windows_service_acceptance.py": (
        "2a1240ada1d63bb0454fa11e9f30e0591d3b1c9fd5f394857ecfc5174603764f"
    ),
    "windows_process_identity.py": (
        "8bc6bac12a9c2bb8c667c879f48fe4de51948f8d68cbcfd255a020dde575e2f4"
    ),
    "onvif_camera_fixture.py": "ec739ca0d75cebd6cf9cc13647ed96f125e63c315e3e066f223d0d02456c61f5",
}
_ROW_KEYS = {"code", "camera_id", "state", "resolution", "occurred_at"}
pytestmark = pytest.mark.skipif(
    os.name != "nt" or os.getenv("K5_NATIVE_RECORDER_ALERT_DIAGNOSTIC") != "1",
    reason="Installed alert persistence requires its isolated disposable Windows candidate VM",
)


def _alerts(client):
    try:
        value = client.json("/api/v1/operator/status")["recorder_alerts"]
        assert type(value) is dict and set(value) == {
            "visible",
            "available",
            "monitoring",
            "active",
            "history",
        }
        assert value["visible"] is True and type(value["available"]) is bool
        assert type(value["monitoring"]) is bool
        for name, limit in (("active", 66), ("history", 20)):
            assert type(value[name]) is list and len(value[name]) <= limit
            for row in value[name]:
                assert type(row) is dict and set(row) == _ROW_KEYS
                assert row["code"] in {
                    "no_new_recording",
                    "recorder_unavailable",
                    "storage_blocked",
                }
                assert row["state"] in {"active", "resolved"}
                assert row["resolution"] in {
                    None,
                    "recording_observed",
                    "worker_ready",
                    "storage_ready",
                    "camera_removed",
                }
                assert row["camera_id"] is None or str(UUID(row["camera_id"])) == row["camera_id"]
                stamp = row["occurred_at"]
                assert type(stamp) is str and len(stamp) <= 40 and stamp.endswith("Z")
                assert (
                    datetime.fromisoformat(stamp.replace("Z", "+00:00")).utcoffset().total_seconds()
                    == 0
                )
    except (AssertionError, AttributeError, KeyError, TypeError, ValueError):
        raise ValueError("Finite public recorder alert contract refused") from None
    return value


def _camera_fault(value, camera_id, *, state):
    if value["available"] is not True or value["monitoring"] is not True:
        return None
    name = "active" if state == "active" else "history"
    rows = [
        row
        for row in value[name]
        if row["code"] == "no_new_recording"
        and row["camera_id"] == camera_id
        and row["state"] == state
    ]
    return dict(rows[-1]) if rows else None


def _error_kind(error):
    name = type(error).__name__
    return (
        name
        if name
        in {
            "Failed",
            "AssertionError",
            "RuntimeError",
            "OSError",
            "TimeoutError",
            "TimeoutExpired",
            "PermissionError",
            "ValueError",
            "KeyError",
            "TypeError",
        }
        else "other"
    )


def _finite_failure(error, stage):
    allowed = {
        "graphical_install",
        "confirmed_write_before_browser_exit",
        "actual_source_stopped_and_production_no_write_threshold",
        "actual_backend_crash_and_scm_recovery",
        "actual_publisher_restart_and_new_recording",
        "completed",
    }
    stage = stage if stage in allowed else "other"
    raise pytest.fail.Exception(
        "Installed recorder alert diagnostic failed: " + stage + " / " + _error_kind(error),
        pytrace=False,
    ) from None


def test_diagnostic_only_installed_recorder_alert_persistence(record_testsuite_property):
    import test_windows_service_recording as service_gate

    harness = os.environ.get("K5_DIAGNOSTIC_HARNESS_HEAD_SHA", "")
    assert re.fullmatch("[a-f0-9]{40}", harness) and harness != PRODUCT_HEAD
    assert os.environ.get("K5_DESKTOP_HEAD_SHA") == PRODUCT_HEAD
    assert os.environ.get("K5_DESKTOP_SETUP_SHA256") == SETUP_SHA
    for name, sha in SOURCE_PINS.items():
        assert (
            hashlib.sha256(
                Path(__file__).with_name(name).read_bytes().replace(b"\r\n", b"\n")
            ).hexdigest()
            == sha
        )
    monitor_source = (
        Path(__file__).resolve().parents[2] / "src/k5vision/operator_recorder_faults.py"
    )
    assert (
        hashlib.sha256(monitor_source.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
        == MONITOR_SHA
    )
    wrapper_sha = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    assert wrapper_sha == os.environ["K5_RECORDER_ALERT_DIAGNOSTIC_SHA256"]
    setup = Path(os.environ["K5_DESKTOP_SETUP"]).resolve(strict=True)
    assert setup.stat().st_size == 286587086
    with setup.open("rb") as stream:
        assert hashlib.file_digest(stream, "sha256").hexdigest() == SETUP_SHA
    record_testsuite_property(
        PREFIX + "binding",
        json.dumps(
            {
                "schema_version": 1,
                "diagnostic_only": True,
                "product_qualification": False,
                "installer_head": PRODUCT_HEAD,
                "harness_head": harness,
                "installer_sha256": SETUP_SHA,
                "installer_bytes": 286587086,
                "candidate_artifact_id": 14745897366,
                "candidate_run_id": 45001731180,
                "candidate_run_attempt": 1,
                "candidate_producer_job_id": 137144542629,
                "wrapper_sha256": wrapper_sha,
                "product_monitor_canonical_lf_sha256": MONITOR_SHA,
                "original_source_canonical_lf_sha256": SOURCE_PINS,
                "product_source_changed": False,
                "normal_criteria_or_deadlines_changed": False,
                "public_fields_only": True,
            },
            sort_keys=True,
        ),
    )
    desktop, runtime, browser_helpers, scm, onvif = (
        service_gate._helpers(name)
        for name in (
            "test_windows_desktop_app",
            "test_operator_product_runtime",
            "test_operator_beta_browser",
            "windows_service_acceptance",
            "onvif_camera_fixture",
        )
    )
    helpers = runtime._helpers()
    media, ffmpeg = (helpers._tool(name) for name in ("K5_MEDIAMTX_DONOR_EXE", "K5_DONOR_FFMPEG"))
    browser_api = pytest.importorskip("playwright.sync_api")
    assert version("playwright") == "1.58.0"
    parent = Path(os.environ["LOCALAPPDATA"]) / ("K5 Recorder Alert Proof-" + uuid4().hex)
    parent.mkdir(mode=0o700)
    installed, profile = desktop._machine_paths()
    assert not installed.exists() and not profile.exists(), "Diagnostic VM already has a K5 site"
    api = desktop._Windows()
    cohort = desktop._Cohort(api)
    app = form = service = camera = playwright = browser = context = None
    processes, reservations = [], []
    notes, evidence = [], {}
    stage, original_error, began = "graphical_install", None, time.monotonic()
    try:
        desktop._install_gui(setup, installed, api)
        desktop._verify_installed_worker_bundle(installed)
        service = scm.InstalledService(installed)
        evidence["scm"] = service.configuration()
        app = desktop._start_app(installed, profile, 8000, cohort)
        form = desktop._wait(lambda: api.form(app.pid), "Alert first-account window", process=app)
        desktop._wait(
            lambda: api.api.IsWindowEnabled(api.control(form, desktop._CREATE)),
            "Alert first-account controls",
            process=app,
        )
        username, password = "alert-fixture-admin", secrets.token_urlsafe(32)
        for field, text in (
            (desktop._USERNAME, username),
            (desktop._PASSWORD, password),
            (desktop._CONFIRM, password),
        ):
            api.set_text(form, field, text)
        api.click(api.control(form, desktop._CREATE))
        client = runtime._Client(8000)
        desktop._wait(
            lambda: desktop._healthy(client, {}), "Alert backend startup", process=app, timeout=60
        )
        desktop._observer_reauthenticate(runtime, client, username, password, {})
        reservations, ports = helpers._reserve_ports(1)
        for reservation in reservations:
            reservation.close()
        uri = f"rtsp://127.0.0.1:{ports[0]}/generated"
        camera = onvif.OnvifCameraFixture(host="127.0.0.1", stream_uri=uri)
        publisher_credentials = ("generated-alert-publisher", secrets.token_urlsafe(24))
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
        publisher = generated[-1]
        playwright = browser_api.sync_playwright().start()
        browser = playwright.chromium.launch(channel="msedge", headless=True)
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
        ) as login:
            page.locator("#sign-in-button").click()
        assert login.value.status == 200
        client.token = login.value.json()["session_token"]
        enrolled, _ = browser_helpers._onvif_enroll(
            page, name="Generated alert camera", fixture=camera
        )
        camera_id = enrolled["device_id"]
        stage = "confirmed_write_before_browser_exit"
        evidence["before_outage"] = service_gate._growth(
            service, desktop, profile, client, camera_id
        )

        def available():
            value = _alerts(client)
            return value if value["available"] else False

        initial_alerts = desktop._wait(available, "Fresh recorder alert monitoring", timeout=10)
        assert _camera_fault(initial_alerts, camera_id, state="active") is None
        context.close()
        context = None
        browser.close()
        browser = None
        api.click(api.control(form, desktop._STOP))
        assert app.wait(timeout=15) == 0 and not api.api.IsWindow(form)
        evidence["after_client_exit"] = service_gate._growth(
            service, desktop, profile, client, camera_id, previous=evidence["before_outage"]
        )
        stage = "actual_source_stopped_and_production_no_write_threshold"
        helpers._stop_owned(publisher)
        assert publisher.poll() is not None
        outage_began = time.monotonic()
        active = desktop._wait(
            lambda: _camera_fault(_alerts(client), camera_id, state="active"),
            "Production no-new-recording alert with workspace closed",
            timeout=70,
        )
        evidence["source_stopped_to_active_seconds"] = round(time.monotonic() - outage_began, 3)
        evidence["active_before_restart"] = active
        evidence["publisher_stopped"] = True
        before = service.observe()
        stage = "actual_backend_crash_and_scm_recovery"
        service.kill_owned(before, backend=True)

        def recovered():
            current = service.observe()
            return (
                current
                if all(
                    (current[p + "_pid"], current[p + "_created"])
                    != (before[p + "_pid"], before[p + "_created"])
                    for p in ("service", "backend")
                )
                else False
            )

        evidence["after_recovery"] = desktop._wait(
            recovered, "SCM recovery for alert persistence", timeout=45
        )
        cohort.add(service.processes()[0], role="service")
        desktop._wait(lambda: desktop._healthy(client, {}), "Recovered alert backend", timeout=30)
        desktop._observer_reauthenticate(runtime, client, username, password, {})
        restored = desktop._wait(
            lambda: _camera_fault(_alerts(client), camera_id, state="active"),
            "Durable active alert loaded by recovered backend",
            timeout=15,
        )
        assert restored == active
        evidence["active_after_restart"] = restored
        evidence["active_public_tuple_preserved"] = True
        stage = "actual_publisher_restart_and_new_recording"
        recovered_before = desktop._recording_bytes(profile)
        publisher = desktop._publish_fixture(
            parent, ffmpeg, uri, helpers, credentials=publisher_credentials
        )
        processes.append(publisher)
        evidence["fresh_recording"] = service_gate._growth(
            service, desktop, profile, client, camera_id
        )
        assert desktop._recording_bytes(profile) > recovered_before

        def resolved():
            value = _alerts(client)
            row = _camera_fault(value, camera_id, state="resolved")
            return (
                row
                if row
                and row["resolution"] == "recording_observed"
                and not _camera_fault(value, camera_id, state="active")
                else False
            )

        row = desktop._wait(resolved, "New-write recovery resolves durable alert", timeout=20)
        assert datetime.fromisoformat(
            row["occurred_at"].replace("Z", "+00:00")
        ) >= datetime.fromisoformat(active["occurred_at"].replace("Z", "+00:00"))
        evidence["resolved_event"] = row
        evidence["fresh_recording_resolved_fault"] = True
        stage = "completed"
    except BaseException as error:
        original_error = error
    finally:
        for label, resource in (
            ("context", context),
            ("browser", browser),
            ("playwright", playwright),
        ):
            if resource is not None:
                try:
                    resource.stop() if label == "playwright" else resource.close()
                except BaseException as error:
                    notes.append({"phase": label, "kind": _error_kind(error)})
        try:
            service_gate._cleanup_service_registration(service, installed, desktop, cohort)
        except BaseException as error:
            notes.append({"phase": "service_stop", "kind": _error_kind(error)})
        try:
            cohort.finish()
            if cohort.failed:
                notes.append({"phase": "cohort", "kind": "ownership_failed"})
            for process in reversed(cohort.snapshot()):
                cohort.terminate_owned(process)
        except BaseException as error:
            notes.append({"phase": "owned_process_cleanup", "kind": _error_kind(error)})
        for process in reversed(processes):
            try:
                helpers._stop_owned(process)
            except BaseException as error:
                notes.append({"phase": "publisher_cleanup", "kind": _error_kind(error)})
        if camera is not None:
            try:
                camera.close()
            except BaseException as error:
                notes.append({"phase": "camera_cleanup", "kind": _error_kind(error)})
        for reservation in reservations:
            reservation.close()
        if (installed / "unins000.exe").is_file():
            try:
                result = subprocess.run(
                    [
                        str(installed / "unins000.exe"),
                        "/VERYSILENT",
                        "/SUPPRESSMSGBOXES",
                        "/NORESTART",
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=60,
                    check=False,
                )
                assert result.returncode == 0 and (service is None or service.absent())
            except BaseException as error:
                notes.append({"phase": "installed_code_cleanup", "kind": _error_kind(error)})
        try:
            desktop._remove_owned_temporary_tree(parent)
        except BaseException as error:
            notes.append({"phase": "temporary_cleanup", "kind": _error_kind(error)})
        record_testsuite_property(
            PREFIX + "observation",
            json.dumps(
                {
                    "diagnostic_only": True,
                    "product_qualification": False,
                    "stage": stage,
                    "original_error_kind": _error_kind(original_error) if original_error else None,
                    "cleanup_failures": notes[:16],
                    "cleanup_complete": not notes,
                    "elapsed_seconds": round(time.monotonic() - began, 3),
                    "evidence": evidence,
                    "physical_camera_acceptance": False,
                    "capacity_acceptance": False,
                    "endurance_acceptance": False,
                    "user_terminal_required": False,
                },
                sort_keys=True,
                allow_nan=False,
            ),
        )
    if original_error is not None:
        _finite_failure(original_error, stage)
    assert not notes, "Installed alert diagnostic did not complete owned cleanup"
    assert time.monotonic() - began < 360, (
        "Separate installed alert component experiment exceeded its budget"
    )
