"""Reject desktop/browser receipts without executed clock and time-health proof."""

from __future__ import annotations

import importlib.util
import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

_SOURCE = Path(__file__).resolve().parents[2] / "scripts" / "verify_desktop_receipt.py"
_SPEC = importlib.util.spec_from_file_location("desktop_receipt_verifier", _SOURCE)
assert _SPEC is not None and _SPEC.loader is not None
_VERIFIER = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_VERIFIER)
_BETA_SPEC = importlib.util.spec_from_file_location(
    "operator_beta_receipt_verifier", _SOURCE.with_name("verify_operator_beta_receipt.py")
)
assert _BETA_SPEC is not None and _BETA_SPEC.loader is not None
_BETA_VERIFIER = importlib.util.module_from_spec(_BETA_SPEC)
_BETA_SPEC.loader.exec_module(_BETA_VERIFIER)

_CASE = "test_native_windows_click_install_account_browser_tray_restart_uninstall"
_REQUIRED_TRUE = (
    "native_windows_execution generated_media_only actual_gui_installer actual_gui_account_setup "
    "administrator_login embedded_python_independent_of_path actual_edge_login_enrollment_live "
    "close_hides_to_tray recording_continues_after_browser_and_window_close "
    "second_click_reopens_single_instance graceful_owned_process_cleanup "
    "restart_preserves_account_camera_history uninstall_preserves_profile_and_media "
    "no_console_windows owned_cleanup onvif_credential_profile_enrollment no_manual_stream_uri "
    "onvif_clock_skew_compensated onvif_wrong_password_rejected "
    "onvif_time_health_displayed recorder_time_health_displayed"
).split()
_REQUIRED_FALSE = (
    "physical_camera_acceptance capacity_acceptance endurance_acceptance windows_service "
    "mocked_gui user_terminal_required host_clock_step_acceptance"
).split()
_CLOCK_FIELDS = (
    "onvif_clock_skew_compensated",
    "onvif_wrong_password_rejected",
    "onvif_clock_offset_seconds",
    "onvif_time_read_count",
)
_HEALTH_FIELDS = ("onvif_time_health_displayed", "recorder_time_health_displayed")
_NVR_FLAGS = (
    "four_camera_wall native_default_browser_handoff four_camera_recording_progress "
    "isolated_camera_outage_reconnect pause_resume_preserves_live_history "
    "other_recording_while_paused timeline_seek_playback actual_recording_export "
    "recording_choice_persisted logout_destroys_wall authenticated_camera_read "
    "rtsp_anonymous_read_rejected rtsp_wrong_password_rejected authenticated_camera_restart_live "
    "wall_viewport_fit native_launch_argument_matched browser_origin_connected page_window_visible "
    "direct_rtsp_enrollment direct_rtsp_wrong_password_rejected direct_rtsp_persisted "
    "direct_rtsp_fresh_enrollment direct_rtsp_multi_channel_same_endpoint "
    "admin_user_management role_permissions camera_access_controls admin_user_creation "
    "admin_bootstrap_password_change admin_password_reset admin_disable_reenable "
    "admin_live_media_revocation admin_audit_visible admin_state_persisted "
    "historical_navigation_default_24h historical_navigation_real_media "
    "playback_rapid_seek_latest_wins playback_controls_and_zoom "
    "playback_contiguous_chunk_handoff playback_gap_requires_explicit_navigation "
    "playback_export_selection_independent export_normal_windows_download "
    "export_download_survives_browser_close export_download_survives_product_restart "
    "export_download_opened_in_edge"
).split()
_ANALYTICS_TRUE = (
    "product_analytics_enablement analytics_bundled_native_inference analytics_gui_configuration "
    "analytics_person_event_recorded analytics_event_utc_playback analytics_public_fixture_only "
    "analytics_browser_closed_detection analytics_role_camera_permissions "
    "analytics_settings_events_persisted"
).split()
_ANALYTICS_FALSE = ("analytics_mock_inference", "analytics_accuracy_acceptance")
_PLAYBACK_EXPORT_TRUE = (
    "historical_navigation_default_24h historical_navigation_real_media "
    "playback_rapid_seek_latest_wins playback_controls_and_zoom "
    "playback_contiguous_chunk_handoff playback_gap_requires_explicit_navigation "
    "playback_export_selection_independent export_normal_windows_download "
    "export_download_survives_browser_close export_download_survives_product_restart "
    "export_download_opened_in_edge"
).split()
_MISSING = object()


def _receipt() -> dict:
    return {
        "schema_version": "1",
        "platform": "Windows",
        **dict.fromkeys(_REQUIRED_TRUE, True),
        **dict.fromkeys(_NVR_FLAGS, True),
        **dict.fromkeys(_ANALYTICS_TRUE, True),
        **dict.fromkeys(_ANALYTICS_FALSE, False),
        **dict.fromkeys(_REQUIRED_FALSE, False),
        "browser_channel": "msedge",
        "playwright_version": "1.58.0",
        "browser_version": "142.0.3595.53",
        "live_decoded_frames": 4,
        "recorded_decoded_frames": 1,
        "duration_seconds": 90.5,
        "onvif_clock_offset_seconds": 1200,
        "onvif_time_read_count": 1,
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
        "historical_recorded_seconds": 10800,
        "historical_recording_file_count": 18,
        "historical_recording_gap_seconds": 600,
        "historical_fixture_fps": 1,
        "playback_chunk_seconds": 120,
        "playback_continuation_decoded_frames": 4,
        "export_download_restart_decoded_frames": 4,
        "analytics_model_revision": "6697dead54ed1cdd664b0313189c2cb52ee6335e",
        "analytics_event_playback_decoded_frames": 4,
        "analytics_positive_person_count": 1,
        "analytics_processed_frames": 1,
        "analytics_background_event_count_before": 1,
        "analytics_background_event_count_after": 2,
        "analytics_denied_http_statuses": [404, 404, 403, 404, 404, 403, 401, 401],
        "authenticated_camera_restart_frames": 4,
        "direct_rtsp_live_decoded_frames": 4,
        "direct_rtsp_history_decoded_frames": 4,
        "direct_rtsp_recording_bytes_before": 0,
        "direct_rtsp_recording_bytes_after": 10,
        "direct_rtsp_fresh_live_decoded_frames": 4,
        "direct_rtsp_fresh_recording_bytes_before": 0,
        "direct_rtsp_fresh_recording_bytes_after": 10,
        "native_browser_handoff_observation": "os_dispatch_origin_and_window",
        "admin_viewer_live_decoded_frames": 4,
        "admin_operator_live_decoded_frames": 5,
        "admin_restart_viewer_live_decoded_frames": 4,
        "admin_user_count_created": 2,
        "admin_audit_event_count": 6,
        "admin_denied_http_statuses": [403, 404, 403, 401, 401, 404],
    }


def _report(tmp_path: Path, receipt: dict, *, browser=False) -> Path:
    root = ET.Element("testsuites")
    suite = ET.SubElement(root, "testsuite", name="native-desktop", tests="1")
    name = "test_real_browser_two_camera_live_history_playback_and_logout" if browser else _CASE
    property_name = "operator_beta_browser_receipt" if browser else "desktop_windows_receipt"
    case = ET.SubElement(suite, "testcase", name=name)
    properties = ET.SubElement(case, "properties")
    ET.SubElement(properties, "property", name=property_name, value=json.dumps(receipt))
    report = tmp_path / "desktop.xml"
    ET.ElementTree(root).write(report, encoding="utf-8", xml_declaration=True)
    return report


@pytest.mark.parametrize("offset,time_reads", [(1200.5, 1), (-1200.5, 128)])
def test_desktop_receipt_accepts_measured_clock_skew(tmp_path, offset, time_reads):
    receipt = _receipt()
    receipt.update(onvif_clock_offset_seconds=offset, onvif_time_read_count=time_reads)

    assert _VERIFIER.verify(_report(tmp_path, receipt)) == receipt


@pytest.mark.parametrize("seconds", [239.9, 240, 300, 359.999])
def test_desktop_receipt_accepts_full_playback_export_and_analytics_scope_within_budget(
    tmp_path, seconds
):
    receipt = _receipt()
    receipt["duration_seconds"] = seconds
    assert _VERIFIER.verify(_report(tmp_path, receipt)) == receipt


@pytest.mark.parametrize(
    "seconds", [0, -1, 360, 361, True, "300", float("nan"), float("inf"), float("-inf")]
)
def test_desktop_receipt_rejects_invalid_or_over_budget_full_product_journey(tmp_path, seconds):
    receipt = _receipt()
    receipt["duration_seconds"] = seconds
    with pytest.raises(ValueError):
        _VERIFIER.verify(_report(tmp_path, receipt))


def test_desktop_receipt_rejects_legacy_evidence_without_clock_proof(tmp_path):
    receipt = _receipt()
    for field in _CLOCK_FIELDS:
        del receipt[field]

    with pytest.raises(ValueError):
        _VERIFIER.verify(_report(tmp_path, receipt))


@pytest.mark.parametrize("field", (*_CLOCK_FIELDS, *_HEALTH_FIELDS))
def test_desktop_receipt_requires_every_clock_proof_field(tmp_path, field):
    receipt = _receipt()
    del receipt[field]

    with pytest.raises(ValueError):
        _VERIFIER.verify(_report(tmp_path, receipt))


@pytest.mark.parametrize("field", _CLOCK_FIELDS[:2])
@pytest.mark.parametrize("invalid", [False, 1])
def test_desktop_receipt_requires_clock_and_bad_password_witnesses(tmp_path, field, invalid):
    receipt = _receipt()
    receipt[field] = invalid

    with pytest.raises(ValueError):
        _VERIFIER.verify(_report(tmp_path, receipt))


@pytest.mark.parametrize(
    "offset",
    [
        0,
        599.9,
        600,
        -600,
        86400,
        -86400,
        True,
        "1200",
        float("nan"),
        float("inf"),
        float("-inf"),
    ],
)
def test_desktop_receipt_rejects_unproven_or_invalid_clock_offset(tmp_path, offset):
    receipt = _receipt()
    receipt["onvif_clock_offset_seconds"] = offset

    with pytest.raises(ValueError):
        _VERIFIER.verify(_report(tmp_path, receipt))


@pytest.mark.parametrize("time_reads", [0, 129, True, 1.0, "1"])
def test_desktop_receipt_rejects_invalid_device_time_read_count(tmp_path, time_reads):
    receipt = _receipt()
    receipt["onvif_time_read_count"] = time_reads

    with pytest.raises(ValueError):
        _VERIFIER.verify(_report(tmp_path, receipt))


@pytest.mark.parametrize("field", _HEALTH_FIELDS)
@pytest.mark.parametrize("invalid", [False, 1, None, "true"])
def test_desktop_receipt_requires_executed_time_health_displays(tmp_path, field, invalid):
    receipt = _receipt()
    receipt[field] = invalid
    with pytest.raises(ValueError):
        _VERIFIER.verify(_report(tmp_path, receipt))


@pytest.mark.parametrize("claim", [True, 0, None])
def test_desktop_receipt_refuses_unqualified_host_clock_step_claim(tmp_path, claim):
    receipt = _receipt()
    receipt["host_clock_step_acceptance"] = claim
    with pytest.raises(ValueError):
        _VERIFIER.verify(_report(tmp_path, receipt))


@pytest.mark.parametrize("field", _NVR_FLAGS)
def test_desktop_receipt_rejects_missing_executed_nvr_journey(tmp_path, field):
    receipt = _receipt()
    del receipt[field]
    with pytest.raises(ValueError):
        _VERIFIER.verify(_report(tmp_path, receipt))


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
def test_desktop_receipt_requires_executed_direct_rtsp_setup(tmp_path, field, invalid):
    receipt = _receipt()
    receipt[field] = invalid
    with pytest.raises(ValueError):
        _VERIFIER.verify(_report(tmp_path, receipt))


@pytest.mark.parametrize("invalid", [None, "address_bar_copy", "url_argument_only", True])
def test_desktop_receipt_requires_the_declared_os_dispatch_observation(tmp_path, invalid):
    receipt = _receipt()
    receipt["native_browser_handoff_observation"] = invalid
    with pytest.raises(ValueError):
        _VERIFIER.verify(_report(tmp_path, receipt))


@pytest.mark.parametrize(
    "field", ["native_launch_argument_matched", "browser_origin_connected", "page_window_visible"]
)
def test_desktop_receipt_refuses_unexecuted_browser_handoff_proofs(tmp_path, field):
    receipt = _receipt()
    receipt[field] = 1
    with pytest.raises(ValueError):
        _VERIFIER.verify(_report(tmp_path, receipt))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("camera_count", 2),
        ("camera_count", 4.0),
        ("wall_layouts", [1, 4]),
        ("wall_viewport", [1440, 720]),
        ("wall_decoded_frames", [4, 4]),
        ("wall_decoded_frames", [4, 4, 4, True]),
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
def test_desktop_receipt_requires_four_live_recording_and_export_proofs(tmp_path, field, value):
    receipt = _receipt()
    receipt[field] = value
    with pytest.raises(ValueError):
        _VERIFIER.verify(_report(tmp_path, receipt))


def _browser_receipt():
    required_true = (
        "ui_login ui_two_camera_enrollment simultaneous_recording camera_switch_pixel_isolation "
        "anonymous_media_denied one_camera_outage_isolated recording_pixel_isolation "
        "ui_history_and_gaps script_visible_credentials_absent logout_revoked_media_and_session "
        "owned_cleanup ui_service_storage_status ui_removal_cancel_preserved_camera "
        "ui_removal_preserved_files_and_denied_media ui_reenrollment_restored_history "
        "onvif_credential_profile_enrollment no_manual_stream_uri onvif_clock_skew_compensated "
        "onvif_time_health_displayed recorder_time_health_displayed "
        "server_utc_rolling_history browser_clock_skew_session_preserved"
    ).split()
    nvr_true = (
        "generated_media_only ui_four_camera_enrollment four_camera_wall wall_outage_recovery "
        "recording_pause_live_continues recording_pause_others_continue "
        "recording_preference_persisted recording_resume_growth ui_recording_controls "
        "timeline_seek_playback actual_download_saved_and_decoded logout_destroyed_wall_tiles "
        "logout_background_recording_continues authenticated_camera_read "
        "rtsp_anonymous_read_rejected rtsp_wrong_password_rejected wall_viewport_fit "
        "direct_rtsp_enrollment direct_rtsp_fresh_enrollment "
        "direct_rtsp_multi_channel_same_endpoint direct_rtsp_wrong_password_rejected "
        "direct_rtsp_reconnect_preserved_identity direct_rtsp_history_and_export "
        "historical_navigation_default_24h historical_navigation_real_media "
        "playback_rapid_seek_latest_wins playback_controls_and_zoom "
        "playback_contiguous_chunk_handoff playback_gap_requires_explicit_navigation "
        "playback_export_selection_independent"
    ).split()
    return {
        **dict.fromkeys(required_true, True),
        **dict.fromkeys(nvr_true, True),
        **dict.fromkeys(
            (
                "real_camera_acceptance",
                "analytics_acceptance",
                "long_duration_acceptance",
                "hardware_capacity_acceptance",
            ),
            False,
        ),
        "schema_version": "1",
        "platform": "Windows",
        "native_windows_execution": True,
        "portable_ancestor_adjustment": False,
        "browser_channel": "msedge",
        "playwright_version": "1.58.0",
        "browser_version": "142.0.3595.53",
        "camera_count": 4,
        "wall_layouts": [1, 4, 9],
        "wall_viewport": [1440, 900],
        "onvif_clock_offsets_seconds": [1199.5, -1200.5, 1200.5, -1199.5],
        "onvif_time_read_counts": [2, 1, 1, 1],
        "browser_script_errors": 0,
        "live_decoded_frames": [4, 4, 4],
        "playback_decoded_frames": [4, 4, 4, 4],
        "duration_seconds": 30.5,
        "historical_recorded_seconds": 10800,
        "historical_recording_file_count": 18,
        "historical_recording_gap_seconds": 600,
        "historical_fixture_fps": 1,
        "playback_chunk_seconds": 120,
        "playback_continuation_decoded_frames": 4,
        "browser_date_skew_seconds": 172800,
        "host_clock_step_acceptance": False,
        "wall_decoded_frames": [4, 5, 6, 7],
        "wall_reconnected_decoded_frames": [8, 9, 10, 11],
        "wall_outage_other_decoded_frames_before": [4, 5, 6],
        "wall_outage_other_decoded_frames": [8, 9, 10],
        "wall_outage_other_recording_bytes_before": [10, 11, 12],
        "wall_outage_other_recording_bytes_after": [20, 21, 22],
        "per_camera_recording_bytes_before": [0, 1, 2, 3],
        "per_camera_recording_bytes_after": [10, 11, 12, 13],
        "export_decoded_frames": [1, 2, 3, 4],
        "pause_live_decoded_frames_before": 4,
        "pause_live_decoded_frames_after": 8,
        "pause_recording_bytes_before": 30,
        "pause_recording_bytes_after": 30,
        "pause_other_recording_bytes_before": [10, 11, 12],
        "pause_other_recording_bytes_after": [20, 21, 22],
        "paused_recording_bytes_after_restart": 30,
        "resumed_recording_bytes": 40,
        "logout_wall_decoded_frames": [4, 5, 6, 7],
        "direct_rtsp_live_decoded_frames": 4,
        "direct_rtsp_playback_decoded_frames": 4,
        "direct_rtsp_export_decoded_frames": 1,
        "direct_rtsp_recording_bytes_before": 0,
        "direct_rtsp_recording_bytes_after": 10,
        "direct_rtsp_fresh_live_decoded_frames": 4,
        "direct_rtsp_fresh_recording_bytes_before": 0,
        "direct_rtsp_fresh_recording_bytes_after": 10,
    }


def test_browser_receipt_accepts_executed_time_health_and_client_clock_skew(tmp_path):
    receipt = _browser_receipt()
    assert _BETA_VERIFIER.verify("browser", _report(tmp_path, receipt, browser=True)) == [receipt]


@pytest.mark.parametrize(
    "field", (*_HEALTH_FIELDS, "server_utc_rolling_history", "browser_clock_skew_session_preserved")
)
@pytest.mark.parametrize("invalid", [False, 1, None])
def test_browser_receipt_requires_actual_time_health_and_skew_witnesses(tmp_path, field, invalid):
    receipt = _browser_receipt()
    receipt[field] = invalid
    with pytest.raises(ValueError):
        _BETA_VERIFIER.verify("browser", _report(tmp_path, receipt, browser=True))


@pytest.mark.parametrize("invalid", [0, 172799, True, "172800", float("nan")])
def test_browser_receipt_requires_the_declared_two_day_client_date_offset(tmp_path, invalid):
    receipt = _browser_receipt()
    receipt["browser_date_skew_seconds"] = invalid
    with pytest.raises(ValueError):
        _BETA_VERIFIER.verify("browser", _report(tmp_path, receipt, browser=True))


def test_browser_receipt_refuses_claiming_host_clock_step_from_client_date_override(tmp_path):
    receipt = _browser_receipt()
    receipt["host_clock_step_acceptance"] = True
    with pytest.raises(ValueError):
        _BETA_VERIFIER.verify("browser", _report(tmp_path, receipt, browser=True))


@pytest.mark.parametrize(
    "field",
    [
        "ui_four_camera_enrollment",
        "four_camera_wall",
        "wall_outage_recovery",
        "recording_pause_live_continues",
        "recording_preference_persisted",
        "timeline_seek_playback",
        "actual_download_saved_and_decoded",
        "logout_destroyed_wall_tiles",
        "logout_background_recording_continues",
        "authenticated_camera_read",
        "rtsp_anonymous_read_rejected",
        "rtsp_wrong_password_rejected",
        "wall_viewport_fit",
    ],
)
def test_browser_receipt_rejects_missing_actual_nvr_proof(tmp_path, field):
    receipt = _browser_receipt()
    del receipt[field]
    with pytest.raises(ValueError):
        _BETA_VERIFIER.verify("browser", _report(tmp_path, receipt, browser=True))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("camera_count", 2),
        ("wall_layouts", [1, 4]),
        ("wall_viewport", [1440, 720]),
        ("wall_decoded_frames", [4, 4]),
        ("wall_outage_other_decoded_frames", [7, 9, 10]),
        ("wall_outage_other_recording_bytes_after", [10, 21, 22]),
        ("per_camera_recording_bytes_after", [10, 11, 2, 13]),
        ("pause_live_decoded_frames_after", 4),
        ("pause_recording_bytes_after", 31),
        ("pause_other_recording_bytes_after", [20, 11, 22]),
        ("paused_recording_bytes_after_restart", 31),
        ("resumed_recording_bytes", 30),
        ("export_decoded_frames", [4, 4]),
        ("export_decoded_frames", [4, 4, 4, False]),
        ("playback_decoded_frames", [4, 4]),
        ("logout_wall_decoded_frames", [4, 4, 4]),
        ("real_camera_acceptance", True),
        ("hardware_capacity_acceptance", True),
        ("long_duration_acceptance", True),
    ],
)
def test_browser_receipt_requires_advancing_camera_specific_nvr_evidence(tmp_path, field, value):
    receipt = _browser_receipt()
    receipt[field] = value
    with pytest.raises(ValueError):
        _BETA_VERIFIER.verify("browser", _report(tmp_path, receipt, browser=True))


@pytest.mark.parametrize(
    "field",
    [
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
    ],
)
@pytest.mark.parametrize("invalid", [False, 1, None])
def test_native_receipt_rejects_unexecuted_user_administration(tmp_path, field, invalid):
    receipt = _receipt()
    receipt[field] = invalid
    with pytest.raises(ValueError):
        _VERIFIER.verify(_report(tmp_path, receipt))


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("admin_viewer_live_decoded_frames", 3),
        ("admin_operator_live_decoded_frames", True),
        ("admin_restart_viewer_live_decoded_frames", 0),
        ("admin_user_count_created", 1),
        ("admin_user_count_created", True),
        ("admin_audit_event_count", 2),
        ("admin_audit_event_count", 101),
        ("admin_denied_http_statuses", [403] * 5),
        ("admin_denied_http_statuses", [403] * 33),
        ("admin_denied_http_statuses", [403, 404, 403, 401, 401, 200]),
        ("admin_denied_http_statuses", [403, 404, 403, 401, 401, True]),
    ],
)
def test_native_receipt_requires_bounded_live_and_authorization_observations(
    tmp_path, field, invalid
):
    receipt = _receipt()
    receipt[field] = invalid
    with pytest.raises(ValueError):
        _VERIFIER.verify(_report(tmp_path, receipt))


@pytest.mark.parametrize("field", [*_PLAYBACK_EXPORT_TRUE, *_ANALYTICS_TRUE])
@pytest.mark.parametrize("invalid", [False, 1, None, _MISSING])
def test_native_receipt_requires_executed_sprint_product_features(tmp_path, field, invalid):
    receipt = _receipt()
    if invalid is _MISSING:
        del receipt[field]
    else:
        receipt[field] = invalid
    with pytest.raises(ValueError):
        _VERIFIER.verify(_report(tmp_path, receipt))


@pytest.mark.parametrize("field", _ANALYTICS_FALSE)
@pytest.mark.parametrize("invalid", [True, 0, None, _MISSING])
def test_native_receipt_requires_real_inference_without_accuracy_claims(tmp_path, field, invalid):
    receipt = _receipt()
    if invalid is _MISSING:
        del receipt[field]
    else:
        receipt[field] = invalid
    with pytest.raises(ValueError):
        _VERIFIER.verify(_report(tmp_path, receipt))


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("historical_recorded_seconds", 7200),
        ("historical_recording_file_count", 17),
        ("historical_recording_file_count", 18.0),
        ("historical_recording_gap_seconds", 0),
        ("historical_fixture_fps", True),
        ("playback_chunk_seconds", 59.9),
        ("playback_chunk_seconds", 301),
        ("playback_chunk_seconds", float("nan")),
        ("playback_chunk_seconds", True),
        ("playback_continuation_decoded_frames", 3),
        ("export_download_restart_decoded_frames", True),
        ("export_download_restart_decoded_frames", 3),
        ("analytics_model_revision", "0" * 40),
        ("analytics_model_revision", _MISSING),
        ("analytics_event_playback_decoded_frames", 3),
        ("analytics_event_playback_decoded_frames", True),
        ("analytics_positive_person_count", 0),
        ("analytics_positive_person_count", 33),
        ("analytics_positive_person_count", 1.0),
        ("analytics_processed_frames", 0),
        ("analytics_processed_frames", _MISSING),
        ("analytics_processed_frames", 100001),
        ("analytics_background_event_count_before", 0),
        ("analytics_background_event_count_before", 2),
        ("analytics_background_event_count_after", 1),
        ("analytics_background_event_count_after", _MISSING),
        ("analytics_background_event_count_after", 100001),
        ("analytics_denied_http_statuses", [404.0, 404, 403, 404, 404, 403, 401, 401]),
        ("analytics_denied_http_statuses", [True, 404, 403, 404, 404, 403, 401, 401]),
        ("analytics_denied_http_statuses", ["404", 404, 403, 404, 404, 403, 401, 401]),
        ("analytics_denied_http_statuses", [404, 404, 403, 404, 404, 403, 401, 200]),
        ("analytics_denied_http_statuses", [404] * 8),
        ("analytics_denied_http_statuses", _MISSING),
    ],
)
def test_native_receipt_rejects_unmeasured_or_tampered_sprint_evidence(tmp_path, field, invalid):
    receipt = _receipt()
    if invalid is _MISSING:
        del receipt[field]
    else:
        receipt[field] = invalid
    with pytest.raises(ValueError):
        _VERIFIER.verify(_report(tmp_path, receipt))


@pytest.mark.parametrize("field", _PLAYBACK_EXPORT_TRUE[:7])
@pytest.mark.parametrize("invalid", [False, 1, None, _MISSING])
def test_browser_receipt_requires_actual_history_navigation(tmp_path, field, invalid):
    receipt = _browser_receipt()
    if invalid is _MISSING:
        del receipt[field]
    else:
        receipt[field] = invalid
    with pytest.raises(ValueError):
        _BETA_VERIFIER.verify("browser", _report(tmp_path, receipt, browser=True))


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("historical_recorded_seconds", 60),
        ("historical_recording_file_count", 1),
        ("historical_recording_file_count", True),
        ("historical_recording_gap_seconds", 0),
        ("historical_fixture_fps", 30),
        ("playback_chunk_seconds", 1),
        ("playback_chunk_seconds", 301),
        ("playback_chunk_seconds", float("nan")),
        ("playback_continuation_decoded_frames", 3),
        ("playback_continuation_decoded_frames", True),
        ("playback_continuation_decoded_frames", _MISSING),
    ],
)
def test_browser_receipt_refuses_short_fixture_as_hours_of_navigation(tmp_path, field, invalid):
    receipt = _browser_receipt()
    if invalid is _MISSING:
        del receipt[field]
    else:
        receipt[field] = invalid
    with pytest.raises(ValueError):
        _BETA_VERIFIER.verify("browser", _report(tmp_path, receipt, browser=True))
