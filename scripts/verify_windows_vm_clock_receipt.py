"""Require real installed-Windows clock steps and verified guest restoration."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

_TEST = "test_native_windows_click_install_account_browser_tray_restart_uninstall"
_SETTINGS_TRUE = [
    "settings_admin_gui",
    "settings_storage_policy_live",
    "settings_storage_policy_restored",
    "settings_custom_role_assignment",
    "settings_profile_bound_edit",
    "settings_custom_role_permission_denied",
    "settings_role_ui_permissions",
    "settings_role_revoked_sessions",
    "settings_time_status_visible",
    "settings_time_no_clock_changes",
    "settings_audit_download",
    "settings_integrations_gui",
    "settings_analytics_machine_event",
    "settings_neural_v2_roundtrip",
    "settings_provider_event_playback",
    "settings_mfa_enrollment",
    "settings_mfa_challenge",
    "settings_mfa_recovery_single_use",
    "settings_mfa_disabled",
    "settings_oidc_signed_pkce",
    "settings_oidc_existing_role",
    "settings_restored_before_restart",
]
_TRUE = [
    "native_windows_execution",
    "generated_media_only",
    "actual_gui_installer",
    "actual_gui_account_setup",
    "administrator_login",
    "embedded_python_independent_of_path",
    "actual_edge_login_enrollment_live",
    "close_hides_to_tray",
    "recording_continues_after_browser_and_window_close",
    "second_click_reopens_single_instance",
    "graceful_owned_process_cleanup",
    "restart_preserves_account_camera_history",
    "uninstall_preserves_profile_and_media",
    "no_console_windows",
    "owned_cleanup",
    "onvif_credential_profile_enrollment",
    "no_manual_stream_uri",
    "onvif_wrong_password_rejected",
    "onvif_clock_skew_compensated",
    "onvif_time_health_displayed",
    "recorder_time_health_displayed",
    "host_clock_step_acceptance",
    "four_camera_wall",
    "native_default_browser_handoff",
    "four_camera_recording_progress",
    "isolated_camera_outage_reconnect",
    "pause_resume_preserves_live_history",
    "other_recording_while_paused",
    "timeline_seek_playback",
    "actual_recording_export",
    "recording_choice_persisted",
    "logout_destroys_wall",
    "authenticated_camera_read",
    "rtsp_anonymous_read_rejected",
    "rtsp_wrong_password_rejected",
    "authenticated_camera_restart_live",
    "wall_viewport_fit",
    "native_launch_argument_matched",
    "browser_origin_connected",
    "page_window_visible",
    "direct_rtsp_enrollment",
    "direct_rtsp_wrong_password_rejected",
    "direct_rtsp_persisted",
    "direct_rtsp_fresh_enrollment",
    "direct_rtsp_multi_channel_same_endpoint",
    "admin_user_management",
    "role_permissions",
    "camera_access_controls",
    "admin_user_creation",
    "admin_bootstrap_password_change",
    "admin_password_reset",
    "admin_disable_reenable",
    "admin_live_media_revocation",
    "admin_audit_visible",
    "admin_state_persisted",
    "historical_navigation_default_24h",
    "historical_navigation_real_media",
    "playback_rapid_seek_latest_wins",
    "playback_controls_and_zoom",
    "playback_contiguous_chunk_handoff",
    "playback_gap_requires_explicit_navigation",
    "playback_export_selection_independent",
    "export_normal_windows_download",
    "export_download_survives_browser_close",
    "export_download_survives_product_restart",
    "export_download_opened_in_edge",
    "product_analytics_enablement",
    "analytics_bundled_native_inference",
    "analytics_gui_configuration",
    "analytics_person_event_recorded",
    "analytics_event_utc_playback",
    "analytics_public_fixture_only",
    "analytics_browser_closed_detection",
    "analytics_role_camera_permissions",
    "analytics_settings_events_persisted",
]
_TRUE += _SETTINGS_TRUE
_FALSE = [
    "physical_camera_acceptance",
    "capacity_acceptance",
    "endurance_acceptance",
    "windows_service",
    "mocked_gui",
    "user_terminal_required",
    "analytics_mock_inference",
    "analytics_accuracy_acceptance",
]
_UTC = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z\Z")
_OFFSETS = (172800, -172800)


def _require(value):
    if not value:
        raise ValueError("Actual Windows VM clock qualification receipt is incomplete")


def _number(value, minimum, maximum):
    _require(type(value) in (int, float))
    _require(minimum <= value <= maximum and math.isfinite(value))
    return value


def _integer(value, minimum, maximum):
    _require(type(value) is int and minimum <= value <= maximum)
    return value


def _integers(value, count, minimum):
    _require(type(value) is list and len(value) == count)
    for item in value:
        _integer(item, minimum, 2**63 - 1)
    return value


def _admin_journey(receipt):
    for field in (
        "admin_viewer_live_decoded_frames",
        "admin_operator_live_decoded_frames",
        "admin_restart_viewer_live_decoded_frames",
    ):
        value = receipt.get(field)
        _require(type(value) is int and 4 <= value <= 100000)
    _require(type(receipt.get("admin_user_count_created")) is int)
    _require(receipt["admin_user_count_created"] == 2)
    events = receipt.get("admin_audit_event_count")
    _require(type(events) is int and 3 <= events <= 100)
    denials = receipt.get("admin_denied_http_statuses")
    _require(type(denials) is list and 6 <= len(denials) <= 32)
    _require(all(type(value) is int and value in {401, 403, 404} for value in denials))


def _settings_journey(receipt):
    _require(all(receipt.get(field) is True for field in _SETTINGS_TRUE))
    for field, minimum, maximum in (
        ("settings_audit_rows", 5, 5000),
        ("settings_provider_event_count", 1, 100),
        ("settings_provider_playback_frames", 4, 1_000_000),
        ("settings_neural_observations", 1, 20),
        ("settings_mfa_recovery_codes", 10, 10),
        ("settings_oidc_token_requests", 1, 1),
    ):
        _integer(receipt.get(field), minimum, maximum)
    denied = receipt.get("settings_denied_http_statuses")
    _require(type(denied) is list and all(type(code) is int for code in denied))
    _require(denied == [401, 403, 401, 422, 401])
    seconds = _number(receipt.get("settings_journey_seconds"), 0, 180)
    total = _number(receipt.get("duration_seconds"), 0, 360)
    _require(0 < seconds <= 180 and 0 < total < 360)
    _require(seconds <= total + 0.01)


def _nvr_journey(receipt):
    _admin_journey(receipt)
    _settings_journey(receipt)
    _require(_integer(receipt.get("camera_count"), 4, 4) == 4)
    layouts = receipt.get("wall_layouts")
    _require(type(layouts) is list and len(layouts) == 3)
    _require(all(type(count) is int for count in layouts) and layouts == [1, 4, 9])
    viewport = receipt.get("wall_viewport")
    _require(type(viewport) is list and len(viewport) == 2)
    _require(all(type(size) is int for size in viewport) and viewport == [1440, 900])
    _integers(receipt.get("wall_decoded_frames"), 4, 4)
    for field in (
        "direct_rtsp_live_decoded_frames",
        "direct_rtsp_history_decoded_frames",
        "direct_rtsp_fresh_live_decoded_frames",
    ):
        _integer(receipt.get(field), 4, 100000)
    direct_before = _integer(receipt.get("direct_rtsp_recording_bytes_before"), 0, 2**63 - 1)
    direct_after = _integer(receipt.get("direct_rtsp_recording_bytes_after"), 1, 2**63 - 1)
    _require(direct_after > direct_before)
    fresh_before = _integer(receipt.get("direct_rtsp_fresh_recording_bytes_before"), 0, 2**63 - 1)
    fresh_after = _integer(receipt.get("direct_rtsp_fresh_recording_bytes_after"), 1, 2**63 - 1)
    _require(fresh_after > fresh_before)
    for prefix, count, minimum in (
        ("camera_recording_bytes", 4, 0),
        ("outage_other_live_frames", 3, 4),
        ("tray_camera_recording_bytes", 3, 1),
    ):
        before = _integers(receipt.get(prefix + "_before"), count, minimum)
        after = _integers(receipt.get(prefix + "_after"), count, minimum)
        growth = 4 if prefix == "outage_other_live_frames" else 1
        _require(all(end >= start + growth for start, end in zip(before, after, strict=True)))
    for field, minimum, maximum in (
        ("paused_recording_bytes", 1, 2**63 - 1),
        ("resumed_recording_bytes", 1, 2**63 - 1),
        ("timeline_playback_decoded_frames", 4, 100000),
        ("export_decoded_frames", 1, 100000),
        ("export_bytes", 1, 16 * 1024**2),
        ("authenticated_camera_restart_frames", 4, 100000),
    ):
        _integer(receipt.get(field), minimum, maximum)
    _require(receipt["resumed_recording_bytes"] > receipt["paused_recording_bytes"])
    for field, expected in (
        ("historical_recorded_seconds", 10800),
        ("historical_recording_file_count", 18),
        ("historical_recording_gap_seconds", 600),
        ("historical_fixture_fps", 1),
    ):
        _require(type(receipt.get(field)) is int and receipt[field] == expected)
    _number(receipt.get("playback_chunk_seconds"), 60, 300)
    _integer(receipt.get("playback_continuation_decoded_frames"), 4, 100000)
    _integer(receipt.get("export_download_restart_decoded_frames"), 4, 100000)
    _require(receipt.get("analytics_model_revision") == "6697dead54ed1cdd664b0313189c2cb52ee6335e")
    for field, minimum, maximum in (
        ("analytics_event_playback_decoded_frames", 4, 100000),
        ("analytics_positive_person_count", 1, 32),
        ("analytics_processed_frames", 1, 100000),
        ("analytics_background_event_count_before", 1, 100000),
        ("analytics_background_event_count_after", 2, 100000),
    ):
        _integer(receipt.get(field), minimum, maximum)
    _require(
        receipt["analytics_background_event_count_after"]
        > receipt["analytics_background_event_count_before"]
    )
    denials = receipt.get("analytics_denied_http_statuses")
    _require(type(denials) is list and all(type(value) is int for value in denials))
    # Preserve independent viewer person-event denial before operator and anonymous checks.
    _require(denials == [404, 404, 404, 403, 404, 404, 403, 401, 401])


def _utc(value):
    _require(type(value) is str and _UTC.fullmatch(value) is not None)
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _snapshot(value, baseline, *, restored=False):
    fields = {"utc", "tick_ms", "elapsed_seconds", "baseline_residual_seconds"}
    if restored:
        fields |= {"restored", "residual_seconds"}
    _require(type(value) is dict and set(value) == fields)
    tick = _integer(value["tick_ms"], baseline[1], 2**64 - 1)
    elapsed = _number(value["elapsed_seconds"], 0, 360)
    _require(abs(elapsed - (tick - baseline[1]) / 1000) <= 0.002)
    utc = _utc(value["utc"])
    residual = _number(value["baseline_residual_seconds"], -172802, 172802)
    _require(abs(residual - ((utc - baseline[0]).total_seconds() - elapsed)) <= 0.01)
    if restored:
        _require(value["restored"] is True)
        measured = _number(value["residual_seconds"], -2, 2)
        _require(abs(residual) <= 2 and abs(residual - measured) <= 0.01)
    return utc, tick, elapsed, residual


def _services(value):
    _require(type(value) is list and len(value) == 2)
    result = {}
    for service in value:
        _require(
            type(service) is dict
            and set(service) == {"name", "present", "running", "start_type", "delayed_auto_start"}
        )
        name = service["name"]
        _require(name in {"W32Time", "vmictimesync"} and name not in result)
        _require(type(service["present"]) is bool and type(service["running"]) is bool)
        if service["present"]:
            _require(type(service["start_type"]) is int and service["start_type"] in {2, 3, 4})
            _require(type(service["delayed_auto_start"]) is bool)
        else:
            _require(
                service["running"] is False
                and service["start_type"] is None
                and service["delayed_auto_start"] is None
            )
        result[name] = service
    return result


def _guard(value):
    _require(type(value) is dict)
    _require(type(value.get("schema_version")) is int and value["schema_version"] == 1)
    _require(value.get("reason") == "closed")
    _require(value.get("native_clock_api") == "SetSystemTime")
    _require(value.get("native_elapsed_api") == "GetTickCount64")
    seconds = _integer(value.get("watchdog_seconds"), 1, 90)
    for field in (
        "privilege_verified",
        "watchdog_armed",
        "clock_restored",
        "services_restored",
    ):
        _require(value.get(field) is True)
    before_services = _services(value.get("services_before"))
    _require(_services(value.get("services_after")) == before_services)
    raw_baseline = value.get("baseline")
    _require(type(raw_baseline) is dict and set(raw_baseline) == {"utc", "tick_ms"})
    baseline = (
        _utc(raw_baseline["utc"]),
        _integer(raw_baseline["tick_ms"], 0, 2**64 - 1),
    )
    shifts = value.get("shifts")
    _require(type(shifts) is list and len(shifts) == 2)
    previous_tick = baseline[1]
    for shift, offset in zip(shifts, _OFFSETS, strict=True):
        _require(type(shift) is dict and shift.get("verified") is True)
        _require(type(shift.get("offset_seconds")) is int and shift["offset_seconds"] == offset)
        before = _snapshot(shift.get("before"), baseline)
        after = _snapshot(shift.get("after"), baseline)
        _require(previous_tick <= before[1] <= after[1])
        _require(0 <= after[2] <= seconds and abs(before[3]) <= 2)
        _require(abs(after[3] - offset) <= 2)
        actual = (after[0] - before[0]).total_seconds() - (after[1] - before[1]) / 1000
        claimed = _number(shift.get("actual_step_seconds"), offset - 2, offset + 2)
        _require(abs(actual - offset) <= 2 and abs(actual - claimed) <= 0.01)
        previous_tick = after[1]
    restored = _snapshot(value.get("last_restoration"), baseline, restored=True)
    _require(restored[1] >= previous_tick)
    return shifts, restored[2]


def _vm_clock(value, *, expected_head, installer_sha256):
    _require(type(value) is dict and value.get("schema_version") == "1")
    _require(value.get("head_sha") == expected_head)
    _require(value.get("installer_sha256") == installer_sha256)
    _require(value.get("method") == "windows_SetSystemTime")
    _require(value.get("analytics_events_utc_preserved") is True)
    _require(value.get("simulated_time") is False and value.get("browser_clock_only") is False)
    offsets = value.get("offset_seconds")
    _require(type(offsets) is list and len(offsets) == 2)
    _require(
        all(
            type(item) is int and item == target
            for item, target in zip(offsets, _OFFSETS, strict=True)
        )
    )
    for field in (
        "closed_segment_hashes_preserved",
        "loopback_only",
        "utc_restored",
        "time_services_restored",
    ):
        _require(value.get(field) is True)
    _integer(value.get("closed_segments"), 1, 1024)
    _integer(value.get("retained_playback_decoded_frames"), 1, 100000)
    shifts, elapsed = _guard(value.get("guard"))
    phases = value.get("phases")
    _require(type(phases) is list and len(phases) == 2)
    for index, (phase, offset) in enumerate(zip(phases, _OFFSETS, strict=True)):
        _require(type(phase) is dict)
        _require(
            type(phase.get("requested_offset_seconds")) is int
            and phase["requested_offset_seconds"] == offset
        )
        actual = _number(phase.get("actual_step_seconds"), offset - 2, offset + 2)
        _require(abs(actual - shifts[index]["actual_step_seconds"]) <= 0.01)
        _number(phase.get("held_offset_seconds"), offset - 2, offset + 2)
        held_seconds = _number(phase.get("monotonic_elapsed_seconds"), 10, 90)
        end = shifts[index + 1]["before"]["elapsed_seconds"] if index == 0 else elapsed
        _require(held_seconds <= end - shifts[index]["before"]["elapsed_seconds"] + 0.002)
        _require(phase.get("clock_alert_direction") == ("forward" if offset > 0 else "backward"))
        for field in (
            "same_human_session",
            "recorder_utc_verified",
            "history_utc_query_verified",
            "analytics_event_utc_preserved",
            "analytics_event_recording_covered",
            "closed_segment_hashes_preserved",
            "utc_restored",
        ):
            _require(phase.get(field) is True)
        _require(type(phase.get("live_http_status")) is int and phase["live_http_status"] == 200)
        for prefix, maximum in (
            ("live_decoded_frames", 100000),
            ("recording_bytes", 2**63 - 1),
        ):
            before = _integer(phase.get(prefix + "_before"), 1, maximum)
            after = _integer(phase.get(prefix + "_after"), 1, maximum)
            _require(after > before)
        _number(phase.get("restore_residual_seconds"), -2, 2)
    return elapsed


def verify(report: Path, *, expected_head: str, installer_sha256: str) -> dict:
    _require(re.fullmatch(r"[a-f0-9]{40}", expected_head) is not None)
    _require(re.fullmatch(r"[a-f0-9]{64}", installer_sha256) is not None)
    _require(report.stat().st_size <= 1024**2)
    root = ET.parse(report).getroot()
    _require(not root.findall(".//failure") and not root.findall(".//error"))
    cases = root.findall(f".//testcase[@name='{_TEST}']")
    properties = root.findall(".//property[@name='desktop_windows_receipt']")
    _require(len(cases) == len(properties) == 1 and cases[0].find("skipped") is None)
    receipt = json.loads(properties[0].get("value", ""))
    _require(type(receipt) is dict)
    _require(receipt.get("schema_version") == "1" and receipt.get("platform") == "Windows")
    _require(all(receipt.get(field) is True for field in _TRUE))
    _require(all(receipt.get(field) is False for field in _FALSE))
    _require(receipt.get("native_browser_handoff_observation") == "os_dispatch_origin_and_window")
    _nvr_journey(receipt)
    _require(receipt.get("browser_channel") == "msedge")
    _require(receipt.get("playwright_version") == "1.58.0")
    _require(
        type(receipt.get("browser_version")) is str
        and re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,3}", receipt["browser_version"]) is not None
    )
    _integer(receipt.get("live_decoded_frames"), 4, 100000)
    _integer(receipt.get("recorded_decoded_frames"), 1, 100000)
    offset = _number(receipt.get("onvif_clock_offset_seconds"), -86400, 86400)
    _require(600 < abs(offset) < 86400)
    _integer(receipt.get("onvif_time_read_count"), 1, 128)
    duration = _number(receipt.get("duration_seconds"), 0, 360)
    _require(0 < duration < 360)
    elapsed = _vm_clock(
        receipt.get("vm_clock"),
        expected_head=expected_head,
        installer_sha256=installer_sha256,
    )
    _require(elapsed <= duration + 2)
    return receipt


# The full desktop receipt and its legacy clock validator remain independent.
_SCOPED_TEST = "test_native_windows_vm_clock_qualification"
_SCOPED_PROPERTY = "vm_clock_windows_receipt"
_PRIMARY_TESTS = {
    _TEST,
    "test_browser_origin_is_retained_when_connection_closes_before_window_visibility",
    "test_native_browser_handoff_retires_only_observed_descendants",
    "test_native_browser_handoff_rejects_unreaped_descendants",
    "test_owned_cleanup_rejects_historical_pid_reuse_without_waiting_on_its_new_occupant",
    "test_owned_cleanup_still_fails_for_a_live_owned_process",
    "test_owned_cleanup_accepts_a_delayed_real_owned_process_exit",
}
_SCOPED_TRUE = [
    "native_windows_execution",
    "generated_media_only",
    "actual_gui_installer",
    "actual_gui_account_setup",
    "administrator_login",
    "embedded_python_independent_of_path",
    "actual_edge_login_enrollment_live",
    "onvif_credential_profile_enrollment",
    "no_manual_stream_uri",
    "recorder_time_health_displayed",
    "four_camera_wall",
    "four_camera_recording_progress",
    "host_clock_step_acceptance",
    "no_console_windows",
    "owned_cleanup",
    "product_analytics_enablement",
    "analytics_bundled_native_inference",
    "analytics_gui_configuration",
    "analytics_person_event_recorded",
    "analytics_event_utc_playback",
    "analytics_public_fixture_only",
]
_SCOPED_FALSE = [
    "physical_camera_acceptance",
    "capacity_acceptance",
    "endurance_acceptance",
    "windows_service",
    "mocked_gui",
    "user_terminal_required",
    "analytics_mock_inference",
    "analytics_accuracy_acceptance",
]
_SCOPED_FIELDS = set(_SCOPED_TRUE + _SCOPED_FALSE) | {
    "schema_version",
    "scope",
    "platform",
    "head_sha",
    "harness_head_sha",
    "installer_sha256",
    "payload_manifest_sha256",
    "browser_channel",
    "playwright_version",
    "browser_version",
    "duration_seconds",
    "primary_binding",
    "vm_clock",
    "camera_count",
    "wall_decoded_frames",
    "camera_recording_bytes_before",
    "camera_recording_bytes_after",
    "recording_enabled_before_clock",
    "storage_policy",
    "analytics_model_revision",
    "analytics_processed_frames",
    "analytics_positive_person_count",
    "analytics_event_playback_decoded_frames",
}


def _scoped_unique_object(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result)
        result[key] = value
    return result


def _scoped_invalid_constant(_value):
    raise ValueError("Actual Windows VM clock qualification receipt is incomplete")


def _scoped_finite_json(value):
    if type(value) is dict:
        _require(all(type(key) is str for key in value))
        for item in value.values():
            _scoped_finite_json(item)
    elif type(value) is list:
        for item in value:
            _scoped_finite_json(item)
    elif type(value) is float:
        _require(math.isfinite(value))
    else:
        _require(value is None or type(value) in (bool, int, str))


def _scoped_json(raw):
    try:
        result = json.loads(
            raw,
            object_pairs_hook=_scoped_unique_object,
            parse_constant=_scoped_invalid_constant,
        )
        _scoped_finite_json(result)
    except (UnicodeError, RecursionError, OverflowError) as error:
        raise ValueError("Actual Windows VM clock qualification receipt is incomplete") from error
    return result


def _scoped_bytes(path, *, maximum=1024**2):
    _require(isinstance(path, Path) and path.is_file())
    _require(0 < path.stat().st_size <= maximum)
    with path.open("rb") as source:
        raw = source.read(maximum + 1)
    _require(0 < len(raw) <= maximum)
    return raw


def _scoped_digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _scoped_canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _scoped_xml(path, *, tests, receipt_property, diagnostic_property, phase_property=None):
    raw = _scoped_bytes(path)
    root = ET.fromstring(raw)
    _require(root.tag in {"testsuites", "testsuite"})
    _require(not any(root.findall(".//" + tag) for tag in ("failure", "error", "skipped")))
    cases = root.findall(".//testcase")
    names = [case.get("name") for case in cases]
    _require(len(names) == len(tests) and set(names) == tests)
    properties = root.findall(".//property")
    allowed = {receipt_property, diagnostic_property}
    if phase_property is not None:
        allowed.add(phase_property)
    _require(all(item.get("name") in allowed for item in properties))
    receipts = [item for item in properties if item.get("name") == receipt_property]
    diagnostics = [item for item in properties if item.get("name") == diagnostic_property]
    _require(len(receipts) == 1 and len(diagnostics) <= 1)
    receipt = _scoped_json(receipts[0].get("value", ""))
    _require(type(receipt) is dict)
    if phase_property is not None:
        phases = [item for item in properties if item.get("name") == phase_property]
        _require(len(phases) == 1)
        phase = _scoped_json(phases[0].get("value", ""))
        _require(_scoped_canonical(phase) == _scoped_canonical(receipt.get("vm_clock")))
    return raw, receipt


def _scoped_setup_hash(path, expected_bytes):
    _require(path.is_file() and path.stat().st_size == expected_bytes)
    digest = hashlib.sha256()
    read = 0
    with path.open("rb") as source:
        while block := source.read(1024**2):
            read += len(block)
            _require(read <= expected_bytes)
            digest.update(block)
    _require(read == expected_bytes and path.stat().st_size == expected_bytes)
    return digest.hexdigest()


def admit_primary(
    primary_report: Path,
    primary_receipt: Path,
    package: Path,
    *,
    expected_head: str,
    installer_sha256: str,
    expected_run_id: int,
    expected_run_attempt: int,
    expected_primary_artifact_id: int,
    expected_setup_artifact_id: int,
) -> dict:
    """Revalidate actual companion files; producer IDs come from admitted run metadata."""
    _require(type(expected_head) is str and re.fullmatch(r"[a-f0-9]{40}", expected_head))
    _require(type(installer_sha256) is str and re.fullmatch(r"[a-f0-9]{64}", installer_sha256))
    for identifier in (
        expected_run_id,
        expected_run_attempt,
        expected_primary_artifact_id,
        expected_setup_artifact_id,
    ):
        _integer(identifier, 1, 2**63 - 1)
    _require(expected_primary_artifact_id != expected_setup_artifact_id)
    report_raw, parsed = _scoped_xml(
        primary_report,
        tests=_PRIMARY_TESTS,
        receipt_property="desktop_windows_receipt",
        diagnostic_property="desktop_windows_diagnostic",
    )
    normal_source = Path(__file__).with_name("verify_desktop_receipt.py")
    specification = importlib.util.spec_from_file_location("_scoped_normal_desktop", normal_source)
    _require(specification is not None and specification.loader is not None)
    normal = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(normal)
    observed = normal.verify(primary_report)
    _require(_scoped_canonical(observed) == _scoped_canonical(parsed))
    _require("vm_clock" not in observed and "scope" not in observed)

    receipt_raw = _scoped_bytes(primary_receipt)
    wrapper = _scoped_json(receipt_raw)
    _require(
        type(wrapper) is dict and set(wrapper) == {"schema_version", "head_sha", "lane", "receipt"}
    )
    _require(wrapper["schema_version"] == "1" and wrapper["lane"] == "desktop")
    _require(wrapper["head_sha"] == expected_head)
    _require(_scoped_canonical(wrapper["receipt"]) == _scoped_canonical(observed))

    _require(isinstance(package, Path) and package.name == "desktop-package.json")
    package_raw = _scoped_bytes(package)
    document = _scoped_json(package_raw)
    _require(type(document) is dict and type(document.get("schema_version")) is int)
    _require(document["schema_version"] == 1 and document.get("head_sha") == expected_head)
    _require(document.get("setup_sha256") == installer_sha256)
    setup_bytes = _integer(document.get("setup_bytes"), 1, 2**31 - 1)
    _require(
        _scoped_setup_hash(package.with_name("K5VisionSetup.exe"), setup_bytes) == installer_sha256
    )
    _require(_scoped_bytes(package.with_name("desktop-windows-receipt.json")) == receipt_raw)

    manifest_raw = _scoped_bytes(package.with_name("payload-manifest.json"))
    manifest = _scoped_json(manifest_raw)
    _require(type(manifest) is dict and manifest.get("head_sha") == expected_head)
    manifest_sha256 = _scoped_digest(manifest_raw)
    _require(document.get("payload_manifest_sha256") == manifest_sha256)
    return {
        "head_sha": expected_head,
        "run_id": expected_run_id,
        "run_attempt": expected_run_attempt,
        "primary_artifact_id": expected_primary_artifact_id,
        "setup_artifact_id": expected_setup_artifact_id,
        "setup_sha256": installer_sha256,
        "setup_bytes": setup_bytes,
        "report_sha256": _scoped_digest(report_raw),
        "receipt_sha256": _scoped_digest(receipt_raw),
        "package_sha256": _scoped_digest(package_raw),
        "manifest_sha256": manifest_sha256,
    }


def verify_scoped(
    report: Path,
    *,
    expected_head: str,
    installer_sha256: str,
    primary_report: Path,
    primary_receipt: Path,
    package: Path,
    expected_run_id: int,
    expected_run_attempt: int,
    expected_primary_artifact_id: int,
    expected_setup_artifact_id: int,
) -> dict:
    """Admit only a separately observed clock journey plus its actual GUI companion."""
    binding = admit_primary(
        primary_report,
        primary_receipt,
        package,
        expected_head=expected_head,
        installer_sha256=installer_sha256,
        expected_run_id=expected_run_id,
        expected_run_attempt=expected_run_attempt,
        expected_primary_artifact_id=expected_primary_artifact_id,
        expected_setup_artifact_id=expected_setup_artifact_id,
    )
    _, receipt = _scoped_xml(
        report,
        tests={_SCOPED_TEST},
        receipt_property=_SCOPED_PROPERTY,
        diagnostic_property="vm_clock_windows_diagnostic",
        phase_property="vm_clock_windows_phase",
    )
    _require(set(receipt) == _SCOPED_FIELDS)
    _require(type(receipt["schema_version"]) is int and receipt["schema_version"] == 2)
    _require(receipt["scope"] == "vm-clock-dedicated" and receipt["platform"] == "Windows")
    _require(receipt["head_sha"] == receipt["harness_head_sha"] == expected_head)
    _require(receipt["installer_sha256"] == installer_sha256)
    _require(receipt["payload_manifest_sha256"] == binding["manifest_sha256"])
    _require(_scoped_canonical(receipt["primary_binding"]) == _scoped_canonical(binding))
    _require(all(receipt[field] is True for field in _SCOPED_TRUE))
    _require(all(receipt[field] is False for field in _SCOPED_FALSE))
    _require(receipt["browser_channel"] == "msedge" and receipt["playwright_version"] == "1.58.0")
    _require(type(receipt["browser_version"]) is str)
    _require(re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,3}", receipt["browser_version"]) is not None)
    _integer(receipt["camera_count"], 4, 4)
    frames = _integers(receipt["wall_decoded_frames"], 4, 4)
    _require(all(value <= 100000 for value in frames))
    before = _integers(receipt["camera_recording_bytes_before"], 4, 0)
    after = _integers(receipt["camera_recording_bytes_after"], 4, 1)
    _require(all(end > start for start, end in zip(before, after, strict=True)))
    enabled = receipt["recording_enabled_before_clock"]
    _require(type(enabled) is list and all(type(value) is bool for value in enabled))
    _require(enabled == [True, True, True, False])
    policy = {
        "revision": 0,
        "quota_bytes": 20 * 1024**3,
        "minimum_free_bytes": 1024**3,
        "retention_seconds": 86400,
    }
    _require(_scoped_canonical(receipt["storage_policy"]) == _scoped_canonical(policy))
    _require(receipt["analytics_model_revision"] == "6697dead54ed1cdd664b0313189c2cb52ee6335e")
    _integer(receipt["analytics_processed_frames"], 1, 2**63 - 1)
    _integer(receipt["analytics_positive_person_count"], 1, 32)
    _integer(receipt["analytics_event_playback_decoded_frames"], 4, 100000)
    duration = _number(receipt["duration_seconds"], 0, 360)
    _require(0 < duration < 360)
    elapsed = _vm_clock(
        receipt["vm_clock"],
        expected_head=expected_head,
        installer_sha256=installer_sha256,
    )
    _require(elapsed <= duration + 2)
    return receipt


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("report", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--expected-head", required=True)
    parser.add_argument("--installer-sha256", required=True)
    parser.add_argument("--scope", choices=("legacy", "vm-clock-dedicated"), default="legacy")
    parser.add_argument("--primary-report", type=Path)
    parser.add_argument("--primary-receipt", type=Path)
    parser.add_argument("--package", type=Path)
    parser.add_argument("--expected-run-id", type=int)
    parser.add_argument("--expected-run-attempt", type=int)
    parser.add_argument("--expected-primary-artifact-id", type=int)
    parser.add_argument("--expected-setup-artifact-id", type=int)
    args = parser.parse_args()
    companions = {
        "primary_report": args.primary_report,
        "primary_receipt": args.primary_receipt,
        "package": args.package,
        "expected_run_id": args.expected_run_id,
        "expected_run_attempt": args.expected_run_attempt,
        "expected_primary_artifact_id": args.expected_primary_artifact_id,
        "expected_setup_artifact_id": args.expected_setup_artifact_id,
    }
    if args.scope == "legacy":
        _require(all(value is None for value in companions.values()))
        receipt = verify(
            args.report,
            expected_head=args.expected_head,
            installer_sha256=args.installer_sha256,
        )
        schema, lane = "1", "desktop-vm-clock"
    else:
        _require(all(value is not None for value in companions.values()))
        receipt = verify_scoped(
            args.report,
            expected_head=args.expected_head,
            installer_sha256=args.installer_sha256,
            **companions,
        )
        schema, lane = "2", "desktop-vm-clock-scoped"
    args.output.write_text(
        json.dumps(
            {
                "schema_version": schema,
                "head_sha": args.expected_head,
                "installer_sha256": args.installer_sha256,
                "lane": lane,
                "receipt": receipt,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    print("Actual installed Windows VM clock-step and restoration evidence passed")


if __name__ == "__main__":
    main()
