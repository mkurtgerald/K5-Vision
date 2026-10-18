"""Require real installed-Windows clock steps and verified guest restoration."""

from __future__ import annotations

import argparse
import json
import math
import re
import xml.etree.ElementTree as ET
from datetime import datetime
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
    "onvif_time_health_displayed recorder_time_health_displayed host_clock_step_acceptance"
).split()
_FALSE = (
    "physical_camera_acceptance capacity_acceptance endurance_acceptance windows_service "
    "mocked_gui user_terminal_required"
).split()
_UTC = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z\Z")
_OFFSETS = (172800, -172800)


def _require(value):
    if not value:
        raise ValueError("Actual Windows VM clock qualification receipt is incomplete")


def _number(value, minimum, maximum):
    _require(type(value) in (int, float))
    _require(math.isfinite(value) and minimum <= value <= maximum)
    return value


def _integer(value, minimum, maximum):
    _require(type(value) is int and minimum <= value <= maximum)
    return value


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
    for field in ("privilege_verified", "watchdog_armed", "clock_restored", "services_restored"):
        _require(value.get(field) is True)
    before_services = _services(value.get("services_before"))
    _require(_services(value.get("services_after")) == before_services)
    raw_baseline = value.get("baseline")
    _require(type(raw_baseline) is dict and set(raw_baseline) == {"utc", "tick_ms"})
    baseline = (_utc(raw_baseline["utc"]), _integer(raw_baseline["tick_ms"], 0, 2**64 - 1))
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
            "closed_segment_hashes_preserved",
            "utc_restored",
        ):
            _require(phase.get(field) is True)
        _require(type(phase.get("live_http_status")) is int and phase["live_http_status"] == 200)
        for prefix, maximum in (("live_decoded_frames", 100000), ("recording_bytes", 2**63 - 1)):
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
        receipt.get("vm_clock"), expected_head=expected_head, installer_sha256=installer_sha256
    )
    _require(elapsed <= duration + 2)
    return receipt


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("report", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--expected-head", required=True)
    parser.add_argument("--installer-sha256", required=True)
    args = parser.parse_args()
    receipt = verify(
        args.report, expected_head=args.expected_head, installer_sha256=args.installer_sha256
    )
    args.output.write_text(
        json.dumps(
            {
                "schema_version": "1",
                "head_sha": args.expected_head,
                "installer_sha256": args.installer_sha256,
                "lane": "desktop-vm-clock",
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
