"""Require actual clock-skew and bad-password evidence in desktop receipts."""

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

_CASE = "test_native_windows_click_install_account_browser_tray_restart_uninstall"
_REQUIRED_TRUE = (
    "native_windows_execution generated_media_only actual_gui_installer actual_gui_account_setup "
    "administrator_login embedded_python_independent_of_path actual_edge_login_enrollment_live "
    "close_hides_to_tray recording_continues_after_browser_and_window_close "
    "second_click_reopens_single_instance graceful_owned_process_cleanup "
    "restart_preserves_account_camera_history uninstall_preserves_profile_and_media "
    "no_console_windows owned_cleanup onvif_credential_profile_enrollment no_manual_stream_uri "
    "onvif_clock_skew_compensated onvif_wrong_password_rejected"
).split()
_REQUIRED_FALSE = (
    "physical_camera_acceptance capacity_acceptance endurance_acceptance windows_service "
    "mocked_gui user_terminal_required"
).split()
_CLOCK_FIELDS = (
    "onvif_clock_skew_compensated",
    "onvif_wrong_password_rejected",
    "onvif_clock_offset_seconds",
    "onvif_time_read_count",
)


def _receipt() -> dict:
    return {
        "schema_version": "1",
        "platform": "Windows",
        **dict.fromkeys(_REQUIRED_TRUE, True),
        **dict.fromkeys(_REQUIRED_FALSE, False),
        "browser_channel": "msedge",
        "playwright_version": "1.58.0",
        "browser_version": "142.0.3595.53",
        "live_decoded_frames": 4,
        "recorded_decoded_frames": 1,
        "duration_seconds": 90.5,
        "onvif_clock_offset_seconds": 1200,
        "onvif_time_read_count": 1,
    }


def _report(tmp_path: Path, receipt: dict) -> Path:
    root = ET.Element("testsuites")
    suite = ET.SubElement(root, "testsuite", name="native-desktop", tests="1")
    case = ET.SubElement(suite, "testcase", name=_CASE)
    properties = ET.SubElement(case, "properties")
    ET.SubElement(properties, "property", name="desktop_windows_receipt", value=json.dumps(receipt))
    report = tmp_path / "desktop.xml"
    ET.ElementTree(root).write(report, encoding="utf-8", xml_declaration=True)
    return report


@pytest.mark.parametrize("offset,time_reads", [(1200.5, 1), (-1200.5, 128)])
def test_desktop_receipt_accepts_measured_clock_skew(tmp_path, offset, time_reads):
    receipt = _receipt()
    receipt.update(onvif_clock_offset_seconds=offset, onvif_time_read_count=time_reads)

    assert _VERIFIER.verify(_report(tmp_path, receipt)) == receipt


def test_desktop_receipt_rejects_legacy_evidence_without_clock_proof(tmp_path):
    receipt = _receipt()
    for field in _CLOCK_FIELDS:
        del receipt[field]

    with pytest.raises(ValueError):
        _VERIFIER.verify(_report(tmp_path, receipt))


@pytest.mark.parametrize("field", _CLOCK_FIELDS)
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
