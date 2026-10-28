"""Reject admin release evidence that omits real role and media restrictions."""

from __future__ import annotations

import json
import runpy
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

_VERIFY = runpy.run_path(
    str(Path(__file__).resolve().parents[2] / "scripts/verify_operator_beta_receipt.py")
)["verify"]


def _receipt():
    return {
        "schema_version": "1",
        "platform": "Windows",
        "native_windows_execution": True,
        "generated_media_only": True,
        "physical_camera_acceptance": False,
        "capacity_acceptance": False,
        "endurance_acceptance": False,
        "host_clock_step_acceptance": False,
        "user_terminal_required": False,
        "owned_cleanup": True,
        "browser_channel": "msedge",
        "playwright_version": "1.58.0",
        "browser_version": "142.0.3595.53",
        "browser_script_errors": 0,
        "camera_count": 2,
        "duration_seconds": 25.5,
        "admin_user_management": True,
        "role_permissions": True,
        "camera_access_controls": True,
        "admin_user_creation": True,
        "admin_bootstrap_password_change": True,
        "admin_password_reset": True,
        "admin_disable_reenable": True,
        "admin_live_media_revocation": True,
        "admin_audit_visible": True,
        "admin_state_persisted": True,
        "admin_viewer_live_decoded_frames": 4,
        "admin_operator_live_decoded_frames": 5,
        "admin_restart_viewer_live_decoded_frames": 6,
        "admin_user_count_created": 2,
        "admin_audit_event_count": 16,
        "admin_denied_http_statuses": [403, 404, 403, 401, 401, 404],
    }


def _report(tmp_path, receipt, *, skipped=False):
    suite = ET.Element("testsuite")
    properties = ET.SubElement(suite, "properties")
    ET.SubElement(
        properties,
        "property",
        name="operator_admin_browser_receipt",
        value=json.dumps(receipt),
    )
    case = ET.SubElement(
        suite,
        "testcase",
        name="test_real_browser_admin_users_roles_camera_access_and_restart",
    )
    if skipped:
        ET.SubElement(case, "skipped")
    path = tmp_path / "admin.xml"
    ET.ElementTree(suite).write(path)
    return path


def test_complete_admin_journey_is_admitted(tmp_path):
    receipt = _receipt()
    assert _VERIFY("admin", _report(tmp_path, receipt)) == [receipt]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("admin_user_management", False),
        ("role_permissions", 1),
        ("camera_access_controls", None),
        ("admin_live_media_revocation", False),
        ("admin_password_reset", False),
        ("admin_state_persisted", False),
        ("admin_viewer_live_decoded_frames", 0),
        ("admin_restart_viewer_live_decoded_frames", True),
        ("admin_user_count_created", 1),
        ("admin_denied_http_statuses", [403, 404, 403, 401, 401, 200]),
        ("admin_denied_http_statuses", [403]),
        ("admin_audit_event_count", 0),
        ("native_windows_execution", False),
        ("physical_camera_acceptance", True),
        ("user_terminal_required", True),
        ("duration_seconds", float("nan")),
    ],
)
def test_missing_or_contradictory_admin_evidence_is_rejected(tmp_path, field, value):
    receipt = _receipt()
    receipt[field] = value
    with pytest.raises(ValueError):
        _VERIFY("admin", _report(tmp_path, receipt))


def test_skipped_admin_journey_does_not_qualify(tmp_path):
    with pytest.raises(ValueError):
        _VERIFY("admin", _report(tmp_path, _receipt(), skipped=True))
