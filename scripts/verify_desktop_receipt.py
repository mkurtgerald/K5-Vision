"""Reject skipped, failed or incomplete native desktop qualification evidence."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path

_TEST = "test_native_windows_click_install_account_browser_tray_restart_uninstall"
_TRUE = (
    "native_windows_execution generated_media_only actual_gui_installer actual_gui_account_setup "
    "administrator_login embedded_python_independent_of_path actual_edge_login_enrollment_live "
    "close_hides_to_tray recording_continues_after_browser_and_window_close "
    "second_click_reopens_single_instance graceful_owned_process_cleanup "
    "restart_preserves_account_camera_history uninstall_preserves_profile_and_media "
    "no_console_windows owned_cleanup onvif_credential_profile_enrollment no_manual_stream_uri "
    "onvif_wrong_password_rejected onvif_clock_skew_compensated "
    "onvif_time_health_displayed recorder_time_health_displayed "
    "four_camera_wall native_default_browser_handoff four_camera_recording_progress "
    "isolated_camera_outage_reconnect pause_resume_preserves_live_history "
    "other_recording_while_paused timeline_seek_playback actual_recording_export "
    "recording_choice_persisted logout_destroys_wall authenticated_camera_read "
    "rtsp_anonymous_read_rejected rtsp_wrong_password_rejected authenticated_camera_restart_live "
    "wall_viewport_fit native_launch_argument_matched browser_origin_connected page_window_visible "
    "direct_rtsp_enrollment direct_rtsp_wrong_password_rejected direct_rtsp_persisted "
    "direct_rtsp_fresh_enrollment direct_rtsp_multi_channel_same_endpoint"
).split()
_FALSE = (
    "physical_camera_acceptance capacity_acceptance endurance_acceptance windows_service "
    "mocked_gui user_terminal_required host_clock_step_acceptance"
).split()


def _require(value):
    if not value:
        raise ValueError("Actual native desktop qualification receipt is incomplete")


def _integers(value, count, minimum):
    _require(type(value) is list and len(value) == count)
    _require(all(type(item) is int and minimum <= item <= 2**63 - 1 for item in value))
    return value


def _nvr_journey(receipt):
    _require(type(receipt.get("camera_count")) is int and receipt["camera_count"] == 4)
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
        value = receipt.get(field)
        _require(type(value) is int and 4 <= value <= 100000)
    direct_before = receipt.get("direct_rtsp_recording_bytes_before")
    direct_after = receipt.get("direct_rtsp_recording_bytes_after")
    _require(type(direct_before) is int and 0 <= direct_before <= 2**63 - 1)
    _require(type(direct_after) is int and direct_before < direct_after <= 2**63 - 1)
    fresh_before = receipt.get("direct_rtsp_fresh_recording_bytes_before")
    fresh_after = receipt.get("direct_rtsp_fresh_recording_bytes_after")
    _require(type(fresh_before) is int and 0 <= fresh_before <= 2**63 - 1)
    _require(type(fresh_after) is int and fresh_before < fresh_after <= 2**63 - 1)
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
        value = receipt.get(field)
        _require(type(value) is int and minimum <= value <= maximum)
    _require(receipt["resumed_recording_bytes"] > receipt["paused_recording_bytes"])


def verify(report: Path) -> dict:
    _require(report.stat().st_size <= 1024**2)
    root = ET.parse(report).getroot()
    _require(not root.findall(".//failure") and not root.findall(".//error"))
    cases = root.findall(f".//testcase[@name='{_TEST}']")
    properties = root.findall(".//property[@name='desktop_windows_receipt']")
    _require(len(cases) == len(properties) == 1)
    _require(cases[0].find("skipped") is None)
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
        isinstance(receipt.get("browser_version"), str)
        and re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,3}", receipt["browser_version"]) is not None
    )
    for field, minimum in (("live_decoded_frames", 4), ("recorded_decoded_frames", 1)):
        _require(type(receipt.get(field)) is int and receipt[field] >= minimum)
    offset = receipt.get("onvif_clock_offset_seconds")
    _require(type(offset) in (int, float) and math.isfinite(offset))
    _require(600 < abs(offset) < 86400)
    reads = receipt.get("onvif_time_read_count")
    _require(type(reads) is int and 1 <= reads <= 128)
    _require(type(receipt.get("duration_seconds")) in (int, float))
    _require(0 < receipt["duration_seconds"] < 240)
    return receipt


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("report", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    receipt = verify(args.report)
    head = os.environ["K5_DESKTOP_HEAD_SHA"]
    _require(re.fullmatch(r"[a-f0-9]{40}", head) is not None)
    args.output.write_text(
        json.dumps(
            {"schema_version": "1", "head_sha": head, "lane": "desktop", "receipt": receipt},
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    print("Actual Windows click-through installation, account, browser and tray evidence passed")


if __name__ == "__main__":
    main()
