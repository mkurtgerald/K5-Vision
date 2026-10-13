"""Fail the native sprint gate unless each declared acceptance actually executed."""

from __future__ import annotations

import argparse
import json
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
        )
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
    "launcher": (
        "preprovisioned_identity wheel_install isolated_wheel_import health "
        "administrator_login worker_ready owned_tree_cleanup"
    ).split(),
    "product": (
        "whole_product_restart_preserved logout_revoked_cached_media "
        "session_invalid_after_restart live_hls_media_received source_reconnected"
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
        "ui_removal_preserved_files_and_denied_media ui_reenrollment_restored_history"
    ).split(),
    "person": (
        "single_flight_busy independent_view_workers third_view_capacity_denied "
        "close_owner_released_capacity idle_views_expired closed_broker_refused "
        "owned_native_threads_stopped"
    ).split(),
    "person-view": (
        "actual_inference ui_sample_positive recording_continues response_frame_correlated "
        "authorized_person_probe logout_cleared logout_person_request_denied "
        "anonymous_person_request_denied owned_cleanup"
    ).split(),
}


def _require(value: bool) -> None:
    if not value:
        raise ValueError("Native sprint acceptance receipt is incomplete or did not execute")


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
        group = "person-view" if property_name == "operator_person_view_runtime_receipt" else lane
        _require(all(receipt.get(key) is True for key in _TRUE[group]))
        if lane == "launcher":
            _require(str(receipt.get("powershell_version", "")).startswith("5.1"))
            cached = property_name == "operator_windows_launcher_receipt"
            _require(receipt.get("verified_cached_workers") is cached)
            if not cached:
                _require(receipt.get("cached") is False)
                _require(receipt.get("downloaded_workers") is True)
                _require(receipt.get("installed_worker_digests") is True)
        elif lane == "product":
            _require(receipt.get("portable_ancestor_adjustment") is False)
            _require(receipt.get("recording_spans", 0) >= 3)
            _require(receipt.get("playback_decoded_frames", 0) > 0)
        elif lane == "install":
            _require(str(receipt.get("powershell_version", "")).startswith("5.1"))
            _require(receipt.get("windows_service") is False)
        elif lane == "browser":
            _require(receipt.get("portable_ancestor_adjustment") is False)
            _require(receipt.get("browser_channel") == "msedge")
            _require(receipt.get("playwright_version") == "1.58.0")
            _require(bool(receipt.get("browser_version")))
            _require(receipt.get("camera_count") == 2)
            _require(receipt.get("browser_script_errors") == 0)
            for field, count in (("live_decoded_frames", 3), ("playback_decoded_frames", 2)):
                frames = receipt.get(field, [])
                _require(len(frames) == count and all(type(n) is int and n >= 4 for n in frames))
            _require(0 < receipt.get("duration_seconds", 0) < 180)
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
