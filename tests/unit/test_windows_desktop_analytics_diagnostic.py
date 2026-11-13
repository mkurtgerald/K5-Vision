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
    monkeypatch.setattr(desktop, "_installed_sampled_person_boxes", lambda *args: {})
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
    client = SimpleNamespace(
        json=lambda route: {"events": events},
        get=lambda route: (200, json.dumps({"events": events}).encode(), {}),
    )
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


def _clock_failure_inputs(*, public_status=None, cameras=None):
    private = "PRIVATE-CLOCK-DIAGNOSTIC-rtsp://camera:secret@10.1.2.3"
    camera_id = "PRIVATE-CLOCK-CAMERA-IDENTITY"
    if public_status is None:
        public_status = {
            "worker_status": "ready",
            "storage": {"reason": "ready", "recording_allowed": True, "recording_bytes": 1234},
            "clock": {"status": "changed", "change_count": 2},
            "private": private,
        }
    if cameras is None:
        cameras = {
            "cameras": [
                {
                    "device_id": camera_id,
                    "name": private,
                    "source_uri": private,
                    "status": "live",
                    "recording": True,
                    "recording_enabled": True,
                    "recording_state": "recording",
                    "recording_status": {
                        "reason": "disk_progress",
                        "observed_bytes": 1234,
                        "last_progress_age_seconds": 0.25,
                        "private": private,
                    },
                }
            ]
        }
    calls = []
    original_transport = object()

    def get(route, *, max_bytes):
        calls.append((route, max_bytes))
        client.transport_failure = private
        payload = public_status if route.endswith("/status") else cameras
        return 200, json.dumps(payload).encode("utf-8"), {"X-Private": private}

    client = SimpleNamespace(get=get, transport_failure=original_transport)
    body = b"Media access denied"

    def body_reader():
        return body

    response = SimpleNamespace(
        status=403,
        headers={"content-type": "text/plain; charset=PRIVATE-CHARSET", "X-Private": private},
        body=body_reader,
        url=private,
    )
    return SimpleNamespace(
        private=private,
        camera_id=camera_id,
        calls=calls,
        client=client,
        original_transport=original_transport,
        response=response,
        body_reader=body_reader,
    )


def test_clock_failure_snapshot_preserves_response_and_bounds_later_observations(
    desktop, monkeypatch
):
    values = _clock_failure_inputs()
    looked_up = _forbid_fresh_lookup(monkeypatch, desktop)
    child_walks = []
    owned = SimpleNamespace(
        pid=97861234,
        is_running=lambda: True,
        name=lambda: "Caddy.exe",
        children=lambda **kwargs: child_walks.append(True),
    )
    cohort = _cohort(owned)
    cohort.snapshot = lambda: child_walks.append(True)
    snapshot = desktop._native_clock_failure_snapshot(
        values.response, client=values.client, camera_id=values.camera_id, cohort=cohort
    )
    assert values.response.status == 403 and values.response.body is values.body_reader
    assert values.response.headers["X-Private"] == values.private
    assert values.response.url == values.private
    assert snapshot["status"] == 403
    assert snapshot["content_type"] == "text_plain"
    assert snapshot["body_class"] == "media_access_denied"
    assert snapshot["body_bytes"] == len(b"Media access denied")
    post = snapshot["post_response"]
    assert post["timing"] == "post_response"
    assert post["status_http_status"] == post["cameras_http_status"] == 200
    assert post["worker_status"] == "ready"
    assert post["storage"]["recording_bytes"] == 1234
    assert post["clock"] == {"status": "changed", "change_count": 2}
    assert post["camera_status_counts"]["live"] == 1
    assert post["target"] == {
        "status": "live",
        "recording": True,
        "recording_enabled": True,
        "recording_state": "recording",
        "reason": "disk_progress",
        "observed_bytes": 1234,
        "last_progress_age_seconds": 0.25,
    }
    assert post["owned_identity_sample_count"] == 1
    assert post["owned_identity_sample_truncated"] is False
    assert post["owned_identity_inspection_failed_count"] == 0
    assert post["owned_process_counts"]["caddy"] == 1
    for name in (
        "owned_process_observed_after_response_ms",
        "status_observed_after_response_ms",
        "cameras_observed_after_response_ms",
    ):
        assert type(post[name]) is int and 0 <= post[name] <= 90000
    assert values.calls == [
        ("/api/v1/operator/status", 32768),
        ("/api/v1/operator/cameras", 32768),
    ]
    assert values.client.transport_failure is values.original_transport
    assert looked_up == [] and child_walks == []
    serialized = json.dumps(snapshot, allow_nan=False)
    for private in (values.private, values.camera_id, "PRIVATE-CHARSET", "97861234"):
        assert private not in serialized
    assert "Media access denied" not in serialized


@pytest.mark.parametrize(
    "body,content_type,expected_class,expected_mime",
    [
        (b"", "", "empty", "missing"),
        (b"Media access denied", "TEXT/PLAIN; charset=utf-8", "media_access_denied", "text_plain"),
        (b"Media access denied PRIVATE-SECRET", "text/plain", "other", "text_plain"),
        (
            b"PRIVATE-BODY-URL-rtsp://user:secret@camera",
            "application/json",
            "other",
            "application_json",
        ),
        (b"PRIVATE-BODY", "text/html", "other", "text_html"),
        (b"PRIVATE-BODY", "PRIVATE-MIME", "other", "other"),
    ],
)
def test_clock_failure_snapshot_only_classifies_original_body_and_mime(
    desktop, body, content_type, expected_class, expected_mime
):
    values = _clock_failure_inputs()
    values.response.body = lambda: body
    values.response.headers = {"content-type": content_type}
    snapshot = desktop._native_clock_failure_snapshot(
        values.response, client=values.client, camera_id=values.camera_id, cohort=_cohort()
    )
    assert snapshot["body_class"] == expected_class
    assert snapshot["content_type"] == expected_mime
    assert snapshot["body_bytes"] == len(body)
    assert "PRIVATE-" not in json.dumps(snapshot, allow_nan=False)


@pytest.mark.parametrize("invalid", [True, -1, 2**80, float("nan"), float("inf")])
def test_clock_failure_snapshot_rejects_malformed_or_nonfinite_public_counters(desktop, invalid):
    values = _clock_failure_inputs(
        public_status={
            "worker_status": "PRIVATE-STATUS",
            "storage": {
                "reason": "PRIVATE-REASON",
                "recording_allowed": 1,
                "recording_bytes": invalid,
            },
            "clock": {"status": "PRIVATE-CLOCK", "change_count": invalid},
        }
    )
    old_get = values.client.get

    def get(route, *, max_bytes):
        if route.endswith("/status"):
            return old_get(route, max_bytes=max_bytes)
        values.calls.append((route, max_bytes))
        return (
            200,
            json.dumps(
                {
                    "cameras": [
                        {
                            "device_id": values.camera_id,
                            "status": "PRIVATE-STATE",
                            "recording": 1,
                            "recording_enabled": "PRIVATE-YES",
                            "recording_state": "PRIVATE-STATE",
                            "recording_status": {
                                "reason": "PRIVATE-REASON",
                                "observed_bytes": invalid,
                                "last_progress_age_seconds": float("nan"),
                            },
                        }
                    ]
                }
            ).encode(),
            {},
        )

    values.client.get = get
    snapshot = desktop._native_clock_failure_snapshot(
        values.response, client=values.client, camera_id=values.camera_id, cohort=_cohort()
    )
    post = snapshot["post_response"]
    assert post["worker_status"] == "unknown"
    assert post["storage"]["reason"] == post["clock"]["status"] == "unknown"
    assert post["storage"]["recording_allowed"] is False
    assert post["storage"]["recording_bytes"] is None
    assert post["clock"]["change_count"] is None
    assert post["target"]["status"] == post["target"]["reason"] == "unknown"
    assert post["target"]["recording"] is post["target"]["recording_enabled"] is False
    assert post["target"]["observed_bytes"] is None
    assert post["target"]["last_progress_age_seconds"] is None
    assert "PRIVATE-" not in json.dumps(snapshot, allow_nan=False)


@pytest.mark.parametrize(
    "detail,code",
    [
        ("Operator identity unavailable", "identity_unavailable"),
        ("Camera state unavailable", "camera_state_unavailable"),
        ("roles_unavailable", "roles_unavailable"),
        ("Media unavailable", "media_unavailable"),
        ("PRIVATE-ERROR-URL", "other"),
    ],
)
def test_clock_failure_snapshot_only_keeps_finite_public_error_codes(desktop, detail, code):
    values = _clock_failure_inputs()

    def get(route, *, max_bytes):
        values.calls.append((route, max_bytes))
        values.client.transport_failure = values.private
        return 503, json.dumps({"detail": {"code": detail, "secret": values.private}}).encode(), {}

    values.client.get = get
    snapshot = desktop._native_clock_failure_snapshot(
        values.response, client=values.client, camera_id=values.camera_id, cohort=_cohort()
    )
    post = snapshot["post_response"]
    assert post["status_http_status"] == post["cameras_http_status"] == 503
    assert post["status_error_code"] == post["cameras_error_code"] == code
    assert post["status_probe_failed"] is post["camera_probe_failed"] is True
    assert len(values.calls) == 2 and all(limit == 32768 for _, limit in values.calls)
    assert values.client.transport_failure is values.original_transport
    assert values.private not in json.dumps(snapshot, allow_nan=False)


def test_clock_failure_snapshot_limits_remembered_locked_process_sample(desktop, monkeypatch):
    values = _clock_failure_inputs()
    looked_up = _forbid_fresh_lookup(monkeypatch, desktop)
    inspections = []

    def name():
        inspections.append(True)
        return "python.exe"

    cohort = _cohort(*[SimpleNamespace(is_running=lambda: True, name=name) for _ in range(65)])

    class LockedInventory(dict):
        def values(self):
            assert cohort.lock.locked()
            return super().values()

    cohort.processes = LockedInventory(cohort.processes)
    snapshot = desktop._native_clock_failure_snapshot(
        values.response, client=values.client, camera_id=values.camera_id, cohort=cohort
    )
    post = snapshot["post_response"]
    assert post["owned_identity_sample_count"] == 64
    assert post["owned_identity_sample_truncated"] is True
    assert post["owned_process_counts"]["python"] == 64
    assert post["owned_identity_inspection_failed_count"] == 0
    assert len(inspections) == 64 and looked_up == []


def test_clock_failure_snapshot_distinguishes_stale_and_uninspectable_identities(
    desktop, monkeypatch
):
    values = _clock_failure_inputs()
    looked_up = _forbid_fresh_lookup(monkeypatch, desktop)
    names, children = [], []

    def unexpected_name():
        names.append(True)
        return values.private

    def denied():
        raise desktop.psutil.AccessDenied(97861234, name=values.private)

    def vanished():
        raise desktop.psutil.NoSuchProcess(97861234, name=values.private)

    cohort = _cohort(
        SimpleNamespace(is_running=lambda: False, name=unexpected_name),
        SimpleNamespace(is_running=denied, name=unexpected_name),
        SimpleNamespace(is_running=lambda: True, name=vanished),
    )
    cohort.snapshot = lambda: children.append(True)
    for process in cohort.processes.values():
        process.children = lambda **kwargs: children.append(True)
    snapshot = desktop._native_clock_failure_snapshot(
        values.response, client=values.client, camera_id=values.camera_id, cohort=cohort
    )
    post = snapshot["post_response"]
    assert post["owned_identity_sample_count"] == 3
    assert post["owned_identity_not_running_count"] == 1
    assert post["owned_identity_inspection_failed_count"] == 2
    assert post["owned_identity_sample_truncated"] is False
    assert all(count == 0 for count in post["owned_process_counts"].values())
    assert names == children == looked_up == []
    serialized = json.dumps(snapshot, allow_nan=False)
    assert values.private not in serialized and "97861234" not in serialized


def test_clock_failure_snapshot_preserves_transport_and_second_probe_after_failure(desktop):
    values = _clock_failure_inputs()

    def get(route, *, max_bytes):
        values.calls.append((route, max_bytes))
        values.client.transport_failure = values.private
        if route.endswith("/status"):
            raise RuntimeError(values.private)
        return 200, b"x" * 32769, {}

    values.client.get = get
    snapshot = desktop._native_clock_failure_snapshot(
        values.response, client=values.client, camera_id=values.camera_id, cohort=None
    )
    post = snapshot["post_response"]
    assert post["status_probe_failed"] is post["camera_probe_failed"] is True
    assert post["cohort_probe_failed"] is True
    assert len(values.calls) == 2 and all(limit == 32768 for _, limit in values.calls)
    assert values.client.transport_failure is values.original_transport
    assert values.private not in json.dumps(snapshot, allow_nan=False)


def _failure_ui_page(value):
    calls = []

    def wait(*args, **kwargs):
        calls.append((args, kwargs))
        assert kwargs["timeout"] == 2000
        return SimpleNamespace(json_value=lambda: value)

    return SimpleNamespace(wait_for_function=wait), calls


def test_native_failure_frames_preserve_only_last_four_exact_public_locations(desktop):
    source_root = Path(desktop.__file__).resolve().parents[2]
    private = "PRIVATE-rtsp://account:password@camera.invalid/private-path"
    error = TimeoutError(private)
    namespace = {"error": error}
    exec(
        compile(
            "def _installed_analytics_journey():\n    _decoded_color()\n",
            str(source_root / "tests/integration/test_windows_desktop_app.py"),
            "exec",
        ),
        namespace,
    )
    exec(
        compile(
            "def _decoded_color():\n    _decoded_video()\n"
            "def _decoded_video():\n    _decoded_video_proof()\n"
            "def _decoded_video_proof():\n    _open_camera_watch()\n"
            "def _open_camera_watch():\n    _open_camera_recordings()\n"
            "def _open_camera_recordings():\n    raise error\n",
            str(source_root / "tests/integration/test_operator_beta_browser.py"),
            "exec",
        ),
        namespace,
    )
    with pytest.raises(TimeoutError) as raised:
        namespace["_installed_analytics_journey"]()
    assert raised.value is error
    frames = desktop._native_failure_frames(raised.value)
    assert [frame["function"] for frame in frames] == [
        "_decoded_video",
        "_decoded_video_proof",
        "_open_camera_watch",
        "_open_camera_recordings",
    ]
    assert all(frame["file"] == "browser" and 1 <= frame["line"] <= 10000 for frame in frames)
    assert private not in json.dumps(frames) and str(source_root) not in json.dumps(frames)


@pytest.mark.parametrize("known_path", [False, True])
def test_native_failure_frames_refuse_unknown_path_or_function(desktop, known_path):
    source_root = Path(desktop.__file__).resolve().parents[2]
    source = source_root / "tests/integration/test_windows_desktop_app.py"
    if not known_path:
        source = source_root / "private/test_windows_desktop_app.py"
    function = "private_account_password" if known_path else "_installed_analytics_journey"
    namespace = {}
    exec(
        compile(
            f"def {function}():\n    raise RuntimeError('private-value')\n", str(source), "exec"
        ),
        namespace,
    )
    with pytest.raises(RuntimeError) as raised:
        namespace[function]()
    assert desktop._native_failure_frames(raised.value) == []


@pytest.mark.parametrize("offset_hours", [0, 2])
def test_native_failure_ui_uses_typed_aware_event_without_publishing_timestamps(
    desktop, offset_hours
):
    from datetime import timezone

    occurred = datetime.now(UTC) + timedelta(seconds=10)
    if offset_hours:
        occurred = occurred.astimezone(timezone(timedelta(hours=offset_hours)))
    private = "PRIVATE-2026-11-07T12:00:00Z-rtsp://account:password@camera.invalid"
    page, calls = _failure_ui_page(
        {
            "history_visible": True,
            "event_button_count": 0,
            "history_item_count": 5,
            "event_within_displayed_range": False,
            "history_end_minus_event_ms": -125.5,
            "history_end_age_seconds": 15.6,
            "playback_ready_state": 0,
            "playback_paused": True,
            "playback_decoded_frames": 0,
            "playback_time_seconds": 0,
            "playback_width": 320,
            "playback_height": 240,
            "live_has_source": private,
            "private": private,
        }
    )
    snapshot = desktop._native_failure_ui(page, occurred=occurred)
    assert len(calls) == 1 and calls[0][1]["arg"] == occurred.timestamp() * 1000
    assert -11 < snapshot["event_age_seconds"] < 0
    assert snapshot["event_within_displayed_range"] is False
    assert snapshot["history_end_minus_event_ms"] == -125.5
    assert snapshot["history_end_age_seconds"] == 15.6
    assert snapshot["event_button_count"] == 0 and snapshot["history_item_count"] == 5
    assert snapshot["live_has_source"] is None
    published = json.dumps(snapshot, allow_nan=False)
    assert private not in published and occurred.isoformat() not in published
    assert str(calls[0][1]["arg"]) not in published


@pytest.mark.parametrize("value", [True, float("nan"), float("inf"), 2**4096, "PRIVATE-VALUE"])
def test_native_failure_ui_rejects_private_or_nonfinite_numeric_payloads(desktop, value):
    page, _ = _failure_ui_page(
        {
            "event_button_count": value,
            "history_item_count": value,
            "history_end_age_seconds": value,
            "history_end_minus_event_ms": value,
            "event_within_displayed_range": value,
            "playback_ready_state": value,
            "playback_time_seconds": value,
            "playback_width": value,
        }
    )
    snapshot = desktop._native_failure_ui(page)
    for key in (
        "event_button_count",
        "history_item_count",
        "history_end_age_seconds",
        "history_end_minus_event_ms",
        "playback_ready_state",
        "playback_time_seconds",
        "playback_width",
    ):
        assert snapshot[key] is None
    # A real boolean is the only admitted type for a containment observation.
    assert snapshot["event_within_displayed_range"] is (True if value is True else None)
    assert "PRIVATE-" not in json.dumps(snapshot, allow_nan=False)


@pytest.mark.parametrize("occurred", [None, True, datetime(2026, 11, 7)])
def test_native_failure_ui_does_not_invent_an_event_epoch(desktop, occurred):
    page, calls = _failure_ui_page({})
    snapshot = desktop._native_failure_ui(page, occurred=occurred)
    assert calls[0][1]["arg"] is None and snapshot["event_age_seconds"] is None


@pytest.mark.parametrize("probe_raises", [False, True])
def test_event_navigation_failure_preserves_original_error_and_typed_event(
    desktop, monkeypatch, probe_raises
):
    original = TimeoutError("PRIVATE-ORIGINAL")
    occurred = datetime.now(UTC)
    monkeypatch.delenv("K5_OPERATOR_PERSON_CONFIG", raising=False)
    monkeypatch.setattr(desktop, "_installed_person_fixture", lambda *args: None)
    monkeypatch.setattr(desktop, "_installed_sampled_person_boxes", lambda *args: {})
    publisher = SimpleNamespace(poll=lambda: None)
    monkeypatch.setattr(desktop, "_publish_fixture", lambda *args, **kwargs: publisher)
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

    locator = SimpleNamespace(
        **dict.fromkeys(["wait_for", "check", "fill", "click"], lambda *a, **k: None)
    )

    def locate(selector):
        if selector == "#last-24-hours":
            return SimpleNamespace(click=lambda: (_ for _ in ()).throw(original))
        return locator

    page = SimpleNamespace(
        locator=locate,
        wait_for_function=lambda *args: None,
        expect_response=lambda *args: Response(),
    )
    client = SimpleNamespace(
        json=lambda route: {
            "events": [
                {
                    "camera_id": "private-camera",
                    "person_count": 3,
                    "confidence": 0.97,
                    "occurred_at": occurred.isoformat(),
                }
            ]
        }
    )

    client.get = lambda route: (200, json.dumps(client.json(route)).encode(), {})

    def wait(condition, label, *, timeout, process):
        assert timeout == 25 and condition() is True

    snapshots = []

    def snapshot(actual_page, *, occurred):
        assert (
            actual_page is page and isinstance(occurred, datetime) and occurred.tzinfo is not None
        )
        snapshots.append(occurred)
        if probe_raises:
            raise RuntimeError("PRIVATE-PROBE")
        return {"event_age_seconds": 1, "event_within_displayed_range": False}

    monkeypatch.setattr(desktop, "_wait", wait)
    monkeypatch.setattr(desktop, "_native_failure_ui", snapshot)
    diagnostic = {}
    with pytest.raises(TimeoutError) as raised:
        desktop._installed_analytics_journey(
            page=page,
            client=client,
            camera_id="private-camera",
            parent=Path("unused"),
            processes=[],
            publishers=[publisher],
            source_uri="private-source",
            ffmpeg=Path("unused"),
            helpers=SimpleNamespace(_stop_owned=lambda *args: None),
            browser_helpers=SimpleNamespace(_open_camera_recordings=lambda *args: None),
            app=SimpleNamespace(poll=lambda: None),
            cohort=_cohort(),
            publisher_credentials=("private", "private"),
            diagnostic=diagnostic,
        )
    assert raised.value is original and snapshots == [occurred]
    assert diagnostic.get("analytics_event_ui_probe_failed", False) is probe_raises
    assert "PRIVATE-" not in json.dumps(diagnostic)


@pytest.mark.parametrize(
    "status,probe_raises", [(200, False), (200, True), (503, False), (503, True)]
)
def test_original_clock_response_and_assertion_survive_failure_observation(
    desktop, monkeypatch, status, probe_raises
):
    import ast

    tree = ast.parse(Path(desktop.__file__).read_text())
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_vm_clock_phase"
    )
    selected = None
    for owner in ast.walk(function):
        body = getattr(owner, "body", None)
        if not isinstance(body, list):
            continue
        for index, node in enumerate(body):
            if isinstance(node, ast.Try) and any(
                isinstance(statement, ast.Assign)
                and any(
                    isinstance(target, ast.Name) and target.id == "live_response"
                    for target in statement.targets
                )
                for statement in node.body
            ):
                selected = body[index : index + 4]
    assert selected is not None and len(selected) == 4
    response = SimpleNamespace(status=status)
    gets, probes = [], []
    original = RuntimeError("PRIVATE-ORIGINAL-ASSERTION")

    def get(*args, **kwargs):
        gets.append((args, kwargs))
        return response

    def probe(actual, **kwargs):
        assert actual is response
        probes.append(actual)
        if probe_raises:
            raise RuntimeError("PRIVATE-PROBE")
        return {"status": status}

    def require(condition, message):
        if not condition:
            raise original

    diagnostic = {}
    namespace = {
        "context": SimpleNamespace(request=SimpleNamespace(get=get)),
        "live_url": "PRIVATE-LIVE-URL",
        "diagnostic": diagnostic,
        "client": object(),
        "camera_id": "PRIVATE-CAMERA",
        "cohort": object(),
        "_native_clock_failure_snapshot": probe,
        "_require": require,
    }
    compiled = compile(
        ast.Module(body=selected, type_ignores=[]), "<original-clock-statements>", "exec"
    )
    if status == 200:
        exec(compiled, namespace)
    else:
        with pytest.raises(RuntimeError) as raised:
            exec(compiled, namespace)
        assert raised.value is original
    assert gets == [(("PRIVATE-LIVE-URL",), {"timeout": 5000})]
    assert namespace["live_response"] is response and namespace["live_status"] == status
    assert len(probes) == int(status != 200)
    assert diagnostic.get("vm_clock_failure_probe_failed", False) is (
        probe_raises and status != 200
    )
    assert "PRIVATE-" not in json.dumps(diagnostic)
