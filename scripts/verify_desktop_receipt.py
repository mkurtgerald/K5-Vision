"""Reject skipped, failed or incomplete native desktop qualification evidence."""

from __future__ import annotations

import argparse
import json
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
    "no_console_windows owned_cleanup onvif_credential_profile_enrollment no_manual_stream_uri"
).split()
_FALSE = (
    "physical_camera_acceptance capacity_acceptance endurance_acceptance windows_service "
    "mocked_gui user_terminal_required"
).split()


def _require(value):
    if not value:
        raise ValueError("Actual native desktop qualification receipt is incomplete")


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
    _require(receipt.get("browser_channel") == "msedge")
    _require(receipt.get("playwright_version") == "1.58.0")
    _require(
        isinstance(receipt.get("browser_version"), str)
        and re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,3}", receipt["browser_version"]) is not None
    )
    for field, minimum in (("live_decoded_frames", 4), ("recorded_decoded_frames", 1)):
        _require(type(receipt.get(field)) is int and receipt[field] >= minimum)
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
