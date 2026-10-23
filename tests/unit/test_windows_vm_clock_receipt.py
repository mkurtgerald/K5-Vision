"""Require installer-bound proof of real Windows host-clock recovery."""

from __future__ import annotations

import importlib.util
import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

_SOURCE = Path(__file__).resolve().parents[2] / "scripts" / "verify_windows_vm_clock_receipt.py"
_SPEC = importlib.util.spec_from_file_location("windows_vm_clock_receipt_verifier", _SOURCE)
assert _SPEC is not None and _SPEC.loader is not None
_VERIFIER = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_VERIFIER)

_CASE = "test_native_windows_click_install_account_browser_tray_restart_uninstall"
_HEAD = "a" * 40
_INSTALLER = "b" * 64
_TRUE = (
    "native_windows_execution generated_media_only actual_gui_installer actual_gui_account_setup "
    "administrator_login embedded_python_independent_of_path actual_edge_login_enrollment_live "
    "close_hides_to_tray recording_continues_after_browser_and_window_close "
    "second_click_reopens_single_instance graceful_owned_process_cleanup "
    "restart_preserves_account_camera_history uninstall_preserves_profile_and_media "
    "no_console_windows owned_cleanup onvif_credential_profile_enrollment no_manual_stream_uri "
    "onvif_wrong_password_rejected onvif_clock_skew_compensated "
    "onvif_time_health_displayed recorder_time_health_displayed host_clock_step_acceptance"
).split()
_FALSE = (
    "physical_camera_acceptance capacity_acceptance endurance_acceptance windows_service "
    "mocked_gui user_terminal_required"
).split()
_MISSING = object()
_NVR_FLAGS = (
    "four_camera_wall native_default_browser_handoff four_camera_recording_progress "
    "isolated_camera_outage_reconnect pause_resume_preserves_live_history "
    "other_recording_while_paused timeline_seek_playback actual_recording_export "
    "recording_choice_persisted logout_destroys_wall authenticated_camera_read "
    "rtsp_anonymous_read_rejected rtsp_wrong_password_rejected authenticated_camera_restart_live "
    "wall_viewport_fit native_launch_argument_matched browser_origin_connected page_window_visible "
    "direct_rtsp_enrollment direct_rtsp_wrong_password_rejected direct_rtsp_persisted "
    "direct_rtsp_fresh_enrollment direct_rtsp_multi_channel_same_endpoint"
).split()


def _guard_receipt() -> dict:
    def snapshot(utc, tick_ms, elapsed_seconds, residual):
        return {
            "utc": utc,
            "tick_ms": tick_ms,
            "elapsed_seconds": elapsed_seconds,
            "baseline_residual_seconds": residual,
        }

    services = [
        {
            "name": "W32Time",
            "present": True,
            "running": True,
            "start_type": 3,
            "delayed_auto_start": False,
        },
        {
            "name": "vmictimesync",
            "present": False,
            "running": False,
            "start_type": None,
            "delayed_auto_start": None,
        },
    ]
    return {
        "schema_version": 1,
        "reason": "closed",
        "native_clock_api": "SetSystemTime",
        "native_elapsed_api": "GetTickCount64",
        "watchdog_seconds": 90,
        "privilege_verified": True,
        "watchdog_armed": True,
        "baseline": {"utc": "2026-10-18T12:00:00Z", "tick_ms": 1000},
        "services_before": services,
        "services_after": [dict(service) for service in services],
        "shifts": [
            {
                "offset_seconds": 172800,
                "actual_step_seconds": 172800.0,
                "before": snapshot("2026-10-18T12:00:01Z", 2000, 1, 0),
                "after": snapshot("2026-10-20T12:00:01.100Z", 2100, 1.1, 172800),
                "verified": True,
            },
            {
                "offset_seconds": -172800,
                "actual_step_seconds": -172800.0,
                "before": snapshot("2026-10-18T12:00:15Z", 16000, 15, 0),
                "after": snapshot("2026-10-16T12:00:15.100Z", 16100, 15.1, -172800),
                "verified": True,
            },
        ],
        "clock_restored": True,
        "services_restored": True,
        "last_restoration": {
            **snapshot("2026-10-18T12:00:30Z", 31000, 30, 0),
            "restored": True,
            "residual_seconds": 0,
        },
    }


def _phase(offset: int) -> dict:
    return {
        "requested_offset_seconds": offset,
        "actual_step_seconds": float(offset),
        "held_offset_seconds": float(offset),
        "monotonic_elapsed_seconds": 10.0,
        "same_human_session": True,
        "clock_alert_direction": "forward" if offset > 0 else "backward",
        "recorder_utc_verified": True,
        "history_utc_query_verified": True,
        "live_http_status": 200,
        "live_decoded_frames_before": 4,
        "live_decoded_frames_after": 12,
        "recording_bytes_before": 4096,
        "recording_bytes_after": 8192,
        "closed_segment_hashes_preserved": True,
        "utc_restored": True,
        "restore_residual_seconds": 0.125,
    }


def _receipt() -> dict:
    return {
        "schema_version": "1",
        "platform": "Windows",
        **dict.fromkeys(_TRUE, True),
        **dict.fromkeys(_NVR_FLAGS, True),
        **dict.fromkeys(_FALSE, False),
        "browser_channel": "msedge",
        "playwright_version": "1.58.0",
        "browser_version": "142.0.3595.53",
        "live_decoded_frames": 12,
        "recorded_decoded_frames": 2,
        "onvif_clock_offset_seconds": 1200,
        "onvif_time_read_count": 1,
        "duration_seconds": 300.5,
        "camera_count": 4,
        "wall_layouts": [1, 4, 9],
        "wall_viewport": [1440, 900],
        "wall_decoded_frames": [4, 5, 6, 7],
        "camera_recording_bytes_before": [0, 1, 2, 3],
        "camera_recording_bytes_after": [10, 11, 12, 13],
        "outage_other_live_frames_before": [4, 5, 6],
        "outage_other_live_frames_after": [8, 9, 10],
        "tray_camera_recording_bytes_before": [10, 11, 12],
        "tray_camera_recording_bytes_after": [20, 21, 22],
        "paused_recording_bytes": 40,
        "resumed_recording_bytes": 50,
        "timeline_playback_decoded_frames": 4,
        "export_decoded_frames": 4,
        "export_bytes": 4096,
        "authenticated_camera_restart_frames": 4,
        "direct_rtsp_live_decoded_frames": 12,
        "direct_rtsp_history_decoded_frames": 4,
        "direct_rtsp_recording_bytes_before": 0,
        "direct_rtsp_recording_bytes_after": 10,
        "direct_rtsp_fresh_live_decoded_frames": 4,
        "direct_rtsp_fresh_recording_bytes_before": 0,
        "direct_rtsp_fresh_recording_bytes_after": 10,
        "native_browser_handoff_observation": "os_dispatch_origin_and_window",
        "vm_clock": {
            "schema_version": "1",
            "head_sha": _HEAD,
            "installer_sha256": _INSTALLER,
            "method": "windows_SetSystemTime",
            "offset_seconds": [172800, -172800],
            "simulated_time": False,
            "browser_clock_only": False,
            "phases": [_phase(172800), _phase(-172800)],
            "closed_segments": 1,
            "closed_segment_hashes_preserved": True,
            "retained_playback_decoded_frames": 2,
            "loopback_only": True,
            "utc_restored": True,
            "time_services_restored": True,
            "guard": _guard_receipt(),
        },
    }


def _report(tmp_path: Path, receipt: dict, *, outcome: str | None = None) -> Path:
    root = ET.Element("testsuites")
    suite = ET.SubElement(root, "testsuite", name="native-windows-vm", tests="1")
    case = ET.SubElement(suite, "testcase", name=_CASE)
    if outcome is not None:
        ET.SubElement(case, outcome)
    properties = ET.SubElement(case, "properties")
    ET.SubElement(properties, "property", name="desktop_windows_receipt", value=json.dumps(receipt))
    report = tmp_path / "windows-vm-clock.xml"
    ET.ElementTree(root).write(report, encoding="utf-8", xml_declaration=True)
    return report


def _verify(report: Path) -> dict:
    return _VERIFIER.verify(report, expected_head=_HEAD, installer_sha256=_INSTALLER)


def test_vm_clock_receipt_accepts_real_bidirectional_installer_bound_witness(tmp_path):
    receipt = _receipt()

    assert _verify(_report(tmp_path, receipt)) == receipt


@pytest.mark.parametrize("field", _NVR_FLAGS)
def test_vm_clock_receipt_requires_real_installed_nvr_journey_as_well_as_clock_steps(
    tmp_path, field
):
    receipt = _receipt()
    del receipt[field]
    with pytest.raises(ValueError):
        _verify(_report(tmp_path, receipt))


@pytest.mark.parametrize(
    "field",
    [
        "direct_rtsp_enrollment",
        "direct_rtsp_wrong_password_rejected",
        "direct_rtsp_persisted",
        "direct_rtsp_fresh_enrollment",
        "direct_rtsp_multi_channel_same_endpoint",
    ],
)
@pytest.mark.parametrize("invalid", [False, 1, None])
def test_vm_clock_receipt_requires_executed_direct_rtsp_setup(tmp_path, field, invalid):
    receipt = _receipt()
    receipt[field] = invalid
    with pytest.raises(ValueError):
        _verify(_report(tmp_path, receipt))


@pytest.mark.parametrize("invalid", [None, "address_bar_copy", "url_argument_only", True])
def test_vm_clock_receipt_requires_the_declared_os_dispatch_observation(tmp_path, invalid):
    receipt = _receipt()
    receipt["native_browser_handoff_observation"] = invalid
    with pytest.raises(ValueError):
        _verify(_report(tmp_path, receipt))


@pytest.mark.parametrize(
    "field", ["native_launch_argument_matched", "browser_origin_connected", "page_window_visible"]
)
def test_vm_clock_receipt_refuses_unexecuted_browser_handoff_proofs(tmp_path, field):
    receipt = _receipt()
    receipt[field] = 1
    with pytest.raises(ValueError):
        _verify(_report(tmp_path, receipt))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("camera_count", 2),
        ("wall_layouts", [1, 4]),
        ("wall_viewport", [1440, 720]),
        ("wall_decoded_frames", [4, 4]),
        ("camera_recording_bytes_after", [10, 11, 2, 13]),
        ("outage_other_live_frames_after", [7, 9, 10]),
        ("tray_camera_recording_bytes_after", [20, 11, 22]),
        ("resumed_recording_bytes", 40),
        ("timeline_playback_decoded_frames", 0),
        ("export_decoded_frames", 0),
        ("export_bytes", 16 * 1024**2 + 1),
        ("authenticated_camera_restart_frames", 3),
        ("authenticated_camera_restart_frames", True),
        ("direct_rtsp_live_decoded_frames", 3),
        ("direct_rtsp_live_decoded_frames", True),
        ("direct_rtsp_history_decoded_frames", None),
        ("direct_rtsp_history_decoded_frames", 0),
        ("direct_rtsp_recording_bytes_before", -1),
        ("direct_rtsp_recording_bytes_before", False),
        ("direct_rtsp_recording_bytes_after", 0),
        ("direct_rtsp_recording_bytes_after", 0.5),
        ("direct_rtsp_fresh_live_decoded_frames", 3),
        ("direct_rtsp_fresh_recording_bytes_before", False),
        ("direct_rtsp_fresh_recording_bytes_after", 0),
    ],
)
def test_vm_clock_receipt_rejects_old_or_nonadvancing_nvr_media_proof(tmp_path, field, value):
    receipt = _receipt()
    receipt[field] = value
    with pytest.raises(ValueError):
        _verify(_report(tmp_path, receipt))


@pytest.mark.parametrize("outcome", ["skipped", "failure", "error"])
def test_vm_clock_receipt_rejects_unexecuted_or_failed_native_test(tmp_path, outcome):
    with pytest.raises(ValueError):
        _verify(_report(tmp_path, _receipt(), outcome=outcome))


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("host_clock_step_acceptance",), False),
        (("vm_clock",), _MISSING),
        (("vm_clock", "head_sha"), "c" * 40),
        (("vm_clock", "installer_sha256"), "c" * 64),
        (("vm_clock", "simulated_time"), True),
        (("vm_clock", "browser_clock_only"), True),
        (("vm_clock", "offset_seconds"), [1200, -1200]),
        (("vm_clock", "phases"), []),
        (("vm_clock", "phases", 1, "requested_offset_seconds"), 172800),
        (("vm_clock", "phases", 0, "actual_step_seconds"), 0),
        (("vm_clock", "phases", 0, "actual_step_seconds"), True),
        (("vm_clock", "phases", 0, "actual_step_seconds"), float("inf")),
        (("vm_clock", "phases", 0, "held_offset_seconds"), 0),
        (("vm_clock", "phases", 0, "monotonic_elapsed_seconds"), 0),
        (("vm_clock", "phases", 0, "monotonic_elapsed_seconds"), 9.999),
        (("vm_clock", "phases", 0, "monotonic_elapsed_seconds"), 89),
        (("vm_clock", "guard", "shifts", 0, "after", "tick_ms"), 2001),
        (("vm_clock", "guard", "shifts", 0, "after", "utc"), "2026-10-18T12:00:01.100Z"),
        (("vm_clock", "phases", 0, "same_human_session"), False),
        (("vm_clock", "phases", 0, "clock_alert_direction"), "backward"),
        (("vm_clock", "phases", 0, "recorder_utc_verified"), False),
        (("vm_clock", "phases", 0, "history_utc_query_verified"), _MISSING),
        (("vm_clock", "phases", 0, "live_http_status"), 401),
        (("vm_clock", "phases", 0, "live_decoded_frames_after"), 4),
        (("vm_clock", "phases", 0, "recording_bytes_after"), _MISSING),
        (("vm_clock", "phases", 0, "recording_bytes_after"), 4096),
        (("vm_clock", "phases", 1, "utc_restored"), False),
        (("vm_clock", "phases", 1, "restore_residual_seconds"), float("nan")),
        (("vm_clock", "phases", 1, "restore_residual_seconds"), 2.01),
        (("vm_clock", "closed_segments"), True),
        (("vm_clock", "closed_segment_hashes_preserved"), False),
        (("vm_clock", "retained_playback_decoded_frames"), 0),
        (("vm_clock", "time_services_restored"), False),
        (("vm_clock", "guard", "privilege_verified"), False),
        (("vm_clock", "guard", "watchdog_armed"), False),
        (("vm_clock", "guard", "services_after", 0, "running"), False),
        (("vm_clock", "guard", "last_restoration", "restored"), False),
        (("duration_seconds",), 360),
    ],
)
def test_vm_clock_receipt_rejects_incomplete_simulated_or_unbound_evidence(tmp_path, path, value):
    receipt = _receipt()
    target = receipt
    for key in path[:-1]:
        target = target[key]
    if value is _MISSING:
        del target[path[-1]]
    else:
        target[path[-1]] = value

    with pytest.raises(ValueError):
        _verify(_report(tmp_path, receipt))
