"""Fail the native sprint gate unless each declared acceptance actually executed."""

from __future__ import annotations

import argparse
import json
import math
import os
import xml.etree.ElementTree as ET
from pathlib import Path

_COMMON = {"schema_version": "1", "platform": "Windows", "native_windows_execution": True}
_TESTS = {
    "launcher": [
        (
            "test_native_windows_powershell_launcher_installs_wheel_and_authenticates",
            "operator_windows_launcher_receipt",
        ),
        (
            "test_native_windows_powershell_launcher_downloads_workers_and_authenticates",
            "operator_windows_fresh_launcher_receipt",
        ),
    ],
    "product": [
        (
            "test_assembled_operator_product_login_live_recording_logout_and_restart",
            "operator_product_runtime_receipt",
        ),
        (
            "test_native_mediamtx_records_reconnects_and_restores_playback",
            "native_donor_runtime_receipt",
        ),
    ],
    "install": [
        ("test_native_windows_installed_operator_lifecycle", "operator_windows_install_receipt")
    ],
    "browser": [
        (
            "test_real_browser_two_camera_live_history_playback_and_logout",
            "operator_beta_browser_receipt",
        )
    ],
    "admin": [
        (
            "test_real_browser_admin_users_roles_camera_access_and_restart",
            "operator_admin_browser_receipt",
        ),
    ],
    "person": [
        (
            "test_native_person_broker_positive_blank_capacity_and_cleanup",
            "operator_beta_person_receipt",
        ),
        (
            "test_assembled_person_view_browser_native_boxes_recording_and_logout",
            "operator_person_view_runtime_receipt",
        ),
    ],
}
_TRUE = {
    "admin": (
        "admin_user_management role_permissions camera_access_controls admin_user_creation "
        "admin_bootstrap_password_change admin_password_reset admin_disable_reenable "
        "admin_live_media_revocation admin_audit_visible admin_state_persisted "
        "generated_media_only owned_cleanup"
    ).split(),
    "launcher": (
        "preprovisioned_identity wheel_install isolated_wheel_import health "
        "administrator_login worker_ready owned_tree_cleanup"
    ).split(),
    "product": (
        "whole_product_restart_preserved logout_revoked_cached_media "
        "session_invalid_after_restart live_hls_media_received source_reconnected"
    ).split(),
    "donor": (
        "past_dated_segment_preserved retained_file_hashes_preserved "
        "restart_inventory_preserved source_reconnected"
    ).split(),
    "install": (
        "per_user_install start_desktop_logon_shortcuts repeat_install foreign_shortcut_refused "
        "recording_limit_persisted installed_wrapper_health administrator_login "
        "live_upgrade_uninstall_refused owned_tree_cleanup stopped_upgrade "
        "prior_release_preserved changed_release_refused unknown_content_refused "
        "uninstall_preserves_profile same_version_wheel_upgrade bootstrap_identity_refused "
        "foreign_writable_tree_refused cached_runtime_no_index readonly_foreign_shortcut_accepted "
        "foreign_write_shortcut_refused"
    ).split(),
    "browser": (
        "ui_login ui_two_camera_enrollment simultaneous_recording camera_switch_pixel_isolation "
        "anonymous_media_denied one_camera_outage_isolated recording_pixel_isolation "
        "ui_history_and_gaps script_visible_credentials_absent logout_revoked_media_and_session "
        "owned_cleanup ui_service_storage_status ui_removal_cancel_preserved_camera "
        "ui_removal_preserved_files_and_denied_media ui_reenrollment_restored_history "
        "onvif_credential_profile_enrollment no_manual_stream_uri onvif_clock_skew_compensated "
        "onvif_time_health_displayed recorder_time_health_displayed "
        "server_utc_rolling_history browser_clock_skew_session_preserved "
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
    ).split(),
    "person": (
        "single_flight_busy independent_view_workers third_view_capacity_denied "
        "close_owner_released_capacity idle_views_expired closed_broker_refused "
        "owned_native_threads_stopped"
    ).split(),
    "person-view": (
        "actual_inference ui_sample_positive recording_continues response_frame_correlated "
        "authorized_person_probe logout_cleared logout_person_request_denied "
        "anonymous_person_request_denied owned_cleanup "
        "onvif_credential_profile_enrollment no_manual_stream_uri"
    ).split(),
}


def _require(value: bool) -> None:
    if not value:
        raise ValueError("Native sprint acceptance receipt is incomplete or did not execute")


def _integers(value, count, minimum):
    _require(type(value) is list and len(value) == count)
    _require(all(type(item) is int and minimum <= item <= 2**63 - 1 for item in value))
    return value


def _browser_nvr_journey(receipt):
    for field, expected in (
        ("historical_recorded_seconds", 10800),
        ("historical_recording_file_count", 18),
        ("historical_recording_gap_seconds", 600),
        ("historical_fixture_fps", 1),
    ):
        _require(type(receipt.get(field)) is int and receipt[field] == expected)
    chunk = receipt.get("playback_chunk_seconds")
    _require(type(chunk) in (int, float) and math.isfinite(chunk) and 60 <= chunk <= 300)
    frames = receipt.get("playback_continuation_decoded_frames")
    _require(type(frames) is int and 4 <= frames <= 100000)
    _require(type(receipt.get("camera_count")) is int and receipt["camera_count"] == 4)
    layouts = receipt.get("wall_layouts")
    _require(type(layouts) is list and len(layouts) == 3)
    _require(all(type(count) is int for count in layouts) and layouts == [1, 4, 9])
    viewport = receipt.get("wall_viewport")
    _require(type(viewport) is list and len(viewport) == 2)
    _require(all(type(size) is int for size in viewport) and viewport == [1440, 900])
    for field, minimum in (
        ("direct_rtsp_live_decoded_frames", 4),
        ("direct_rtsp_playback_decoded_frames", 4),
        ("direct_rtsp_export_decoded_frames", 1),
        ("direct_rtsp_recording_bytes_before", 0),
        ("direct_rtsp_recording_bytes_after", 1),
    ):
        value = receipt.get(field)
        _require(type(value) is int and minimum <= value <= 2**63 - 1)
    _require(
        receipt["direct_rtsp_recording_bytes_after"] > receipt["direct_rtsp_recording_bytes_before"]
    )
    for field in (
        "real_camera_acceptance",
        "analytics_acceptance",
        "long_duration_acceptance",
        "hardware_capacity_acceptance",
    ):
        _require(receipt.get(field) is False)
    for field, count, minimum in (
        ("wall_decoded_frames", 4, 4),
        ("wall_reconnected_decoded_frames", 4, 4),
        ("wall_outage_other_decoded_frames", 3, 4),
        ("export_decoded_frames", 4, 1),
        ("logout_wall_decoded_frames", 4, 4),
    ):
        _integers(receipt.get(field), count, minimum)
    before = _integers(receipt.get("per_camera_recording_bytes_before"), 4, 0)
    after = _integers(receipt.get("per_camera_recording_bytes_after"), 4, 0)
    _require(all(end > start for start, end in zip(before, after, strict=True)))
    outage_before = _integers(receipt.get("wall_outage_other_decoded_frames_before"), 3, 4)
    outage_after = _integers(receipt.get("wall_outage_other_decoded_frames"), 3, 4)
    _require(all(end >= start + 4 for start, end in zip(outage_before, outage_after, strict=True)))
    for prefix in ("wall_outage_other_recording_bytes", "pause_other_recording_bytes"):
        before = _integers(receipt.get(prefix + "_before"), 3, 0)
        after = _integers(receipt.get(prefix + "_after"), 3, 0)
        _require(all(end > start for start, end in zip(before, after, strict=True)))
    for field in (
        "pause_live_decoded_frames_before",
        "pause_live_decoded_frames_after",
        "pause_recording_bytes_before",
        "pause_recording_bytes_after",
        "paused_recording_bytes_after_restart",
        "resumed_recording_bytes",
    ):
        value = receipt.get(field)
        _require(type(value) is int and 0 < value <= 2**63 - 1)
    _require(
        receipt["pause_live_decoded_frames_after"] > receipt["pause_live_decoded_frames_before"]
    )
    _require(
        receipt["pause_recording_bytes_before"]
        == receipt["pause_recording_bytes_after"]
        == receipt["paused_recording_bytes_after_restart"]
        < receipt["resumed_recording_bytes"]
    )


def verify(lane: str, path: Path) -> list[dict]:
    root = ET.parse(path).getroot()
    _require(not root.findall(".//failure") and not root.findall(".//error"))
    values = []
    for test, property_name in _TESTS[lane]:
        cases = root.findall(f".//testcase[@name='{test}']")
        properties = root.findall(f".//property[@name='{property_name}']")
        _require(len(cases) == len(properties) == 1 and cases[0].find("skipped") is None)
        receipt = json.loads(properties[0].get("value", ""))
        _require(type(receipt) is dict)
        common = dict(_COMMON)
        if lane == "product":
            common.pop("schema_version")
        for key, expected in common.items():
            _require(type(receipt.get(key)) is type(expected) and receipt[key] == expected)
        group = (
            "person-view"
            if property_name == "operator_person_view_runtime_receipt"
            else "donor"
            if property_name == "native_donor_runtime_receipt"
            else lane
        )
        _require(all(receipt.get(key) is True for key in _TRUE[group]))
        if lane == "launcher":
            _require(str(receipt.get("powershell_version", "")).startswith("5.1"))
            cached = property_name == "operator_windows_launcher_receipt"
            _require(receipt.get("verified_cached_workers") is cached)
            if not cached:
                _require(receipt.get("cached") is False)
                _require(receipt.get("downloaded_workers") is True)
                _require(receipt.get("installed_worker_digests") is True)
        elif group == "donor":
            _require(receipt.get("version") == "v1.21.2")
            _require(receipt.get("media") == "locally_generated_h264")
            _require(receipt.get("production_camera_acceptance") is False)
            _require(receipt.get("analytics_acceptance") is False)
            _require(receipt.get("browser_acceptance") is False)
            _require(receipt.get("anonymous_api_status") == 401)
            _require(receipt.get("anonymous_playback_status") == 401)
            _require(type(receipt.get("recording_segments")) is int)
            _require(receipt["recording_segments"] >= 3)
            frames = receipt.get("playback_decoded_frames_after_restart")
            _require(type(frames) is list and len(frames) >= 2)
            _require(all(type(count) is int and count > 0 for count in frames))
        elif lane == "product":
            _require(receipt.get("portable_ancestor_adjustment") is False)
            _require(receipt.get("recording_spans", 0) >= 3)
            _require(receipt.get("playback_decoded_frames", 0) > 0)
        elif lane == "install":
            _require(str(receipt.get("powershell_version", "")).startswith("5.1"))
            _require(receipt.get("windows_service") is False)
        elif lane == "admin":
            for field in (
                "physical_camera_acceptance",
                "capacity_acceptance",
                "endurance_acceptance",
                "host_clock_step_acceptance",
                "user_terminal_required",
            ):
                _require(receipt.get(field) is False)
            _require(receipt.get("browser_channel") == "msedge")
            _require(receipt.get("playwright_version") == "1.58.0")
            _require(bool(receipt.get("browser_version")))
            _require(type(receipt.get("camera_count")) is int and receipt["camera_count"] == 2)
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
            _require(type(events) is int and 3 <= events <= 100000)
            denied = receipt.get("admin_denied_http_statuses")
            _require(type(denied) is list and 6 <= len(denied) <= 32)
            _require(all(type(status) is int and status in {401, 403, 404} for status in denied))
            _require(receipt.get("browser_script_errors") == 0)
            duration = receipt.get("duration_seconds")
            _require(type(duration) in (int, float) and math.isfinite(duration))
            _require(0 < duration < 180)
        elif lane == "browser":
            _require(receipt.get("host_clock_step_acceptance") is False)
            _require(type(receipt.get("browser_date_skew_seconds")) is int)
            _require(receipt["browser_date_skew_seconds"] == 172800)
            _require(receipt.get("portable_ancestor_adjustment") is False)
            _require(receipt.get("browser_channel") == "msedge")
            _require(receipt.get("playwright_version") == "1.58.0")
            _require(bool(receipt.get("browser_version")))
            _browser_nvr_journey(receipt)
            offsets = receipt.get("onvif_clock_offsets_seconds")
            reads = receipt.get("onvif_time_read_counts")
            _require(type(offsets) is list and len(offsets) == 4)
            _require(
                all(
                    type(offset) in (int, float)
                    and math.isfinite(offset)
                    and 600 < abs(offset) < 86400
                    for offset in offsets
                )
            )
            _require(offsets[0] > 0 > offsets[1])
            _require(type(reads) is list and len(reads) == 4)
            _require(all(type(count) is int and 0 < count <= 128 for count in reads))
            _require(receipt.get("browser_script_errors") == 0)
            for field, count in (("live_decoded_frames", 3), ("playback_decoded_frames", 4)):
                _integers(receipt.get(field), count, 4)
            _require(0 < receipt.get("duration_seconds", 0) < 240)
        elif group == "person-view":
            _require(receipt.get("portable_ancestor_adjustment") is False)
            _require(receipt.get("browser_channel") == "msedge")
            _require(receipt.get("playwright_version") == "1.58.0")
            _require(bool(receipt.get("browser_version")))
            _require(receipt.get("browser_script_errors") == 0)
            _require(receipt.get("model_revision") == "6697dead54ed1cdd664b0313189c2cb52ee6335e")
            _require(receipt.get("mock_inference") is False)
            _require(receipt.get("logout_request_frame_bytes") == 4)
            _require(1 <= receipt.get("person_boxes", 0) <= 32)
            _require(receipt.get("displayed_frame_matching_fraction", 0) >= 0.90)
            _require(receipt.get("box_overlay_pixels", 0) >= 100)
            _require(receipt.get("box_added_overlay_pixels", 0) >= 100)
            _require(receipt.get("live_decoded_frames", 0) >= 4)
            _require(receipt.get("recorded_playback_decoded_frames", 0) > 0)
            _require(0 < receipt.get("duration_seconds", 0) < 90)
        elif lane == "person":
            _require(receipt.get("model_revision") == "6697dead54ed1cdd664b0313189c2cb52ee6335e")
            _require(receipt.get("mock_inference") is False)
            _require(receipt.get("positive_person_boxes", 0) > 0)
            _require(receipt.get("blank_person_boxes") == 0)
            _require(
                receipt.get("runtime_versions")
                == {"openvino": "2026.3.1", "numpy": "2.2.6", "openvino-telemetry": "2025.2.0"}
            )
            _require(0 < receipt.get("duration_seconds", 0) < 120)
        values.append(receipt)
    return values


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("lane", choices=sorted(_TESTS))
    parser.add_argument("report", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    values = verify(args.lane, args.report)
    args.output.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "lane": args.lane,
                "head_sha": os.environ["K5_BETA_HEAD_SHA"],
                "receipts": values,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"Actual native Windows {args.lane} acceptance and receipt passed")


if __name__ == "__main__":
    main()
