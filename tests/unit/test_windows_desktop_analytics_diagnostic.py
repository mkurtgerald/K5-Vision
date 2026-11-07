"""Failure diagnostics preserve the actual gate and exclude private runtime values."""

from __future__ import annotations

import importlib.util
import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def desktop():
    source = Path(__file__).parents[2] / "tests/integration/test_windows_desktop_app.py"
    spec = importlib.util.spec_from_file_location("desktop_analytics_failure_unit", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _cohort(*processes):
    return SimpleNamespace(
        lock=threading.Lock(),
        processes={(index, 1.0): process for index, process in enumerate(processes)},
    )


def _forbid_fresh_lookup(monkeypatch, desktop):
    looked_up = []

    def lookup(pid):
        looked_up.append(pid)
        raise AssertionError("Fresh process lookup is forbidden in failure diagnostics")

    monkeypatch.setattr(desktop.psutil, "Process", lookup)
    return looked_up


def test_failure_snapshot_retains_boundary_state_without_private_values(desktop, monkeypatch):
    private = "PRIVATE-USER-PATH-rtsp://user:password@camera.example"
    identity = "PRIVATE-CAMERA-IDENTITY"
    future = (datetime.now(UTC) + timedelta(seconds=10)).isoformat()
    statuses = {
        "status": "waiting",
        "processed_frames": 1,
        "event_count": 0,
        "last_sample_at": future,
        "last_event_at": private,
        "secret": private,
    }
    camera = {
        "device_id": identity,
        "name": private,
        "host": private,
        "source_revision": private,
        "status": "live",
        "recording": True,
        "recording_enabled": True,
        "recording_state": "recording",
        "recording_status": {
            "reason": "disk_progress",
            "observed_bytes": 1234,
            "last_progress_age_seconds": 17.0,
            "secret": private,
        },
    }
    calls = []

    def json_response(route):
        calls.append(route)
        client.transport_failure = None
        return statuses if route.endswith("/analytics") else {"cameras": [camera]}

    client = SimpleNamespace(json=json_response, transport_failure=private)
    decoder = SimpleNamespace(
        cmdline=lambda: [private, "-m", "k5vision.operator_events_decoder"],
        is_running=lambda: True,
    )
    unrelated = SimpleNamespace(cmdline=lambda: [private], is_running=lambda: True)
    looked_up = _forbid_fresh_lookup(monkeypatch, desktop)
    diagnostic = {}
    desktop._installed_analytics_failure_snapshot(
        client=client,
        camera_id=identity,
        publishers=[SimpleNamespace(poll=lambda: 3221225477)],
        cohort=_cohort(decoder, unrelated),
        diagnostic=diagnostic,
    )
    snapshot = diagnostic["analytics_failure_snapshot"]
    assert snapshot["analytics"]["status"] == "waiting"
    assert snapshot["analytics"]["processed_frames"] == 1
    assert snapshot["analytics"]["event_count"] == 0
    assert -11 < snapshot["analytics"]["last_sample_age_seconds"] < 0
    assert snapshot["analytics"]["last_event_age_seconds"] is None
    assert snapshot["camera"]["observed_bytes"] == 1234
    assert snapshot["camera"]["last_progress_age_seconds"] == 17.0
    assert snapshot["decoder_running_count"] == 1
    assert looked_up == []
    assert snapshot["publisher_running"] is False
    assert snapshot["publisher_exit_code"] == 3221225477
    assert len(calls) == 2
    assert client.transport_failure == private
    serialized = json.dumps(diagnostic, allow_nan=False)
    assert private not in serialized and identity not in serialized and future not in serialized
    assert "9876" not in serialized


def test_failure_snapshot_sanitizes_malformed_public_values(desktop, monkeypatch):
    private = "PRIVATE-UNEXPECTED-PAYLOAD"
    status = {
        "status": private,
        "processed_frames": True,
        "event_count": 2**80,
        "last_sample_at": private,
        "last_event_at": "2026-01-01T00:00:00",
    }
    camera = {
        "device_id": private,
        "status": private,
        "recording": private,
        "recording_enabled": 1,
        "recording_state": private,
        "recording_status": {
            "reason": private,
            "observed_bytes": -1,
            "last_progress_age_seconds": float("nan"),
        },
    }
    client = SimpleNamespace(
        json=lambda route: status if route.endswith("/analytics") else {"cameras": [camera]},
        transport_failure=None,
    )
    looked_up = _forbid_fresh_lookup(monkeypatch, desktop)
    diagnostic = {}
    desktop._installed_analytics_failure_snapshot(
        client=client,
        camera_id=private,
        publishers=[SimpleNamespace(poll=lambda: None)],
        cohort=_cohort(),
        diagnostic=diagnostic,
    )
    snapshot = diagnostic["analytics_failure_snapshot"]
    assert snapshot["analytics"]["status"] == "unknown"
    assert snapshot["analytics"]["processed_frames"] is None
    assert snapshot["analytics"]["event_count"] is None
    assert snapshot["analytics"]["last_event_age_seconds"] is None
    assert snapshot["camera"]["status"] == "unknown"
    assert snapshot["camera"]["reason"] == "unknown"
    assert snapshot["camera"]["observed_bytes"] is None
    assert snapshot["camera"]["last_progress_age_seconds"] is None
    assert snapshot["camera"]["recording"] is False
    assert snapshot["camera"]["recording_enabled"] is False
    assert private not in json.dumps(diagnostic, allow_nan=False)
    assert looked_up == []


def test_failure_snapshot_reports_probe_failure_without_exception_text(desktop, monkeypatch):
    private = "PRIVATE-EXCEPTION-URL-OR-PATH"

    def broken(*args, **kwargs):
        raise RuntimeError(private)

    looked_up = _forbid_fresh_lookup(monkeypatch, desktop)
    diagnostic = {}
    desktop._installed_analytics_failure_snapshot(
        client=SimpleNamespace(json=broken, transport_failure=None),
        camera_id=private,
        publishers=[SimpleNamespace(poll=broken)],
        cohort=None,
        diagnostic=diagnostic,
    )
    assert diagnostic["analytics_failure_snapshot"] == {
        "analytics_probe_failed": True,
        "camera_probe_failed": True,
        "publisher_probe_failed": True,
        "decoder_probe_failed": True,
    }
    assert private not in json.dumps(diagnostic, allow_nan=False)
    assert looked_up == []


@pytest.mark.parametrize("probe_raises", [False, True])
@pytest.mark.parametrize("gate_fails", [False, True])
def test_original_person_deadline_failure_survives_diagnostics(
    desktop, monkeypatch, probe_raises, gate_fails
):
    original_error = RuntimeError("PRIVATE-ORIGINAL-ERROR")
    monkeypatch.delenv("K5_OPERATOR_PERSON_CONFIG", raising=False)
    monkeypatch.setattr(desktop, "_installed_person_fixture", lambda *args: None)
    publisher = SimpleNamespace(poll=lambda: None)
    monkeypatch.setattr(desktop, "_publish_fixture", lambda *args, **kwargs: publisher)
    locator = SimpleNamespace(
        **dict.fromkeys(["wait_for", "check", "fill", "click"], lambda *args, **kwargs: None)
    )
    settings = {
        "enabled": True,
        "confidence_threshold": 0.5,
        "status": "starting",
        "source": "recorded_camera_frames",
        "model": "person-detection-retail-0013",
    }

    class Response:
        value = SimpleNamespace(status=200, json=lambda: settings)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    def locate(selector):
        if selector == "#last-24-hours":

            def after_positive():
                raise original_error

            return SimpleNamespace(click=after_positive)
        return locator

    page = SimpleNamespace(
        locator=locate,
        wait_for_function=lambda *args: None,
        expect_response=lambda *args: Response(),
    )
    events = (
        []
        if gate_fails
        else [
            {
                "camera_id": "private-camera",
                "person_count": 3,
                "confidence": 0.97,
                "occurred_at": datetime.now(UTC).isoformat(),
            }
        ]
    )
    client = SimpleNamespace(json=lambda route: {"events": events})
    snapshots = []

    def probe(**kwargs):
        snapshots.append(kwargs)
        if probe_raises:
            raise ValueError("PRIVATE-DIAGNOSTIC-ERROR")

    def wait(condition, label, *, timeout, process):
        assert timeout == 25
        assert condition() is not gate_fails
        if gate_fails:
            raise original_error
        return True

    monkeypatch.setattr(desktop, "_wait", wait)
    monkeypatch.setattr(desktop, "_installed_analytics_failure_snapshot", probe)
    diagnostic = {}
    with pytest.raises(RuntimeError) as caught:
        desktop._installed_analytics_journey(
            page=page,
            client=client,
            camera_id="private-camera",
            parent=Path("unused"),
            processes=[],
            publishers=[publisher],
            source_uri="private-stream-address",
            ffmpeg=Path("unused"),
            helpers=SimpleNamespace(_stop_owned=lambda *args: None),
            browser_helpers=SimpleNamespace(_open_camera_recordings=lambda *args: None),
            app=SimpleNamespace(pid=9876, poll=lambda: None),
            cohort=_cohort(),
            publisher_credentials=("private", "private"),
            diagnostic=diagnostic,
        )
    assert caught.value is original_error
    assert len(snapshots) == int(gate_fails)
    assert diagnostic["analytics_stage"] == (
        "positive_camera_recording" if gate_fails else "event_recording_navigation"
    )
    assert diagnostic.get("analytics_failure_probe_failed", False) is (probe_raises and gate_fails)
    assert "PRIVATE-" not in json.dumps(diagnostic, allow_nan=False)


@pytest.mark.parametrize("still_owned", [False, True])
def test_failure_snapshot_uses_remembered_birth_identity_without_fresh_pid_lookup(
    desktop, monkeypatch, still_owned
):
    looked_up = _forbid_fresh_lookup(monkeypatch, desktop)
    command_reads, child_walks = [], []

    def command():
        command_reads.append(True)
        return ["private-path", "-m", "k5vision.operator_events_decoder"]

    process = SimpleNamespace(
        pid=9876,
        is_running=lambda: still_owned,
        cmdline=command,
        children=lambda **kwargs: child_walks.append(True),
    )
    cohort = _cohort(process)
    cohort.processes = {(9876, 123.0): process}
    diagnostic = {}
    desktop._installed_analytics_failure_snapshot(
        client=SimpleNamespace(json=lambda route: {}, transport_failure=None),
        camera_id="private-camera",
        publishers=[SimpleNamespace(poll=lambda: None)],
        cohort=cohort,
        diagnostic=diagnostic,
    )
    assert looked_up == [] and child_walks == []
    assert command_reads == ([True] if still_owned else [])
    assert diagnostic["analytics_failure_snapshot"]["decoder_running_count"] == int(still_owned)
    assert "9876" not in json.dumps(diagnostic)
