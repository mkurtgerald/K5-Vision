"""Admit exact-Setup native motion/zones, real media and elapsed-clock evidence.

The caller separately verifies the Actions run, attempt, job and original ZIP
provenance. This verifier accepts one complete, passing installed test only.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

_TEST = "test_native_windows_motion_recording_and_zones"
_PROPERTY = "motion_windows_receipt"
_TRUE = {
    "native_windows_execution",
    "generated_media_only",
    "actual_gui_installer",
    "installed_worker_bundle_verified",
    "actual_gui_account_setup",
    "embedded_runtime_independent",
    "actual_edge_ui",
    "graphical_mode_save",
    "graphical_zone_authoring",
    "trusted_preview",
    "backend_zone_enforced",
    "full_frame_default",
    "zero_precapture_explicit",
    "browser_closed_detection",
    "restart_policy_persisted",
    "global_pause_wins",
    "other_camera_recording_continues",
    "nonadmin_motion_denied",
    "preview_owner_scoped",
    "source_geometry_invalidates",
    "retained_files_preserved",
    "retained_utc_history",
    "recording_gap_honest",
    "retained_playback_decoded",
    "retained_export_decoded",
    "monotonic_post_capture",
    "actual_clock_corrections",
    "utc_restored",
    "time_services_restored",
    "no_console_windows",
    "uninstall_preserves_profile",
    "owned_cleanup",
}
_FALSE = {
    "physical_camera_acceptance",
    "capacity_acceptance",
    "endurance_acceptance",
    "permanent_privacy_mask",
    "pre_event_buffer",
    "mocked_trigger",
    "mocked_gui",
    "user_terminal_required",
}
_FIELDS = {
    "schema_version",
    "head_sha",
    "installer_sha256",
    "platform",
    "browser_channel",
    "browser_version",
    "playwright_version",
    "duration_seconds",
    "motion_clock",
    "retained",
    "observations",
    "policy_revision_before_restart",
    "policy_revision_after_restart",
    "policy_source_before_restart",
    "policy_source_after_restart",
    "preview_geometry",
    "changed_geometry",
    "post_capture_seconds",
    "pre_capture_seconds",
    "retained_file_count",
    "permission_statuses",
}
_OBSERVATIONS = {
    "full_frame_idle": ("armed", False, 4),
    "full_frame_motion": ("recording", True, 4),
    "excluded_only": ("armed", False, 4),
    "outside_inclusion": ("armed", False, 4),
    "zoned_idle": ("armed", False, 4),
    "included_motion": ("recording", True, 6),
    "post_capture_idle": ("armed", False, 4),
    "browser_closed_recording": ("recording", True, 4),
    "browser_closed_idle": ("armed", False, 4),
    "restart_idle": ("armed", False, 4),
    "master_paused": ("paused", False, 4),
    "stale_geometry": ("zones_need_review", False, 4),
}
_GUARD_FIELDS = {
    "schema_version",
    "reason",
    "native_clock_api",
    "native_elapsed_api",
    "watchdog_seconds",
    "privilege_verified",
    "watchdog_armed",
    "baseline",
    "services_before",
    "services_after",
    "shifts",
    "clock_restored",
    "services_restored",
    "last_restoration",
}
_HASH = re.compile(r"[a-f0-9]{64}\Z")


def _require(value):
    if not value:
        raise ValueError("Installed Windows motion evidence refused")


def _integer(value, minimum, maximum=2**63 - 1):
    _require(type(value) is int and minimum <= value <= maximum)
    return value


def _number(value, minimum, maximum):
    _require(type(value) in (int, float) and math.isfinite(value) and minimum <= value <= maximum)
    return value


def _object(pairs):
    result = {}
    for name, value in pairs:
        _require(name not in result)
        result[name] = value
    return result


def _constant(_value):
    raise ValueError("Installed Windows motion evidence refused")


def _finite_float(value):
    result = float(value)
    _require(math.isfinite(result))
    return result


def _utc(value):
    _require(type(value) is str and len(value) <= 32 and value.endswith("Z"))
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    _require(result.utcoffset() is not None and result.utcoffset().total_seconds() == 0)
    return result


def _witness(raw):
    _require(type(raw) is bytes and 0 < len(raw) <= 1024**2)
    _require(
        b"\x00" not in raw and b"<!DOCTYPE" not in raw.upper() and b"<!ENTITY" not in raw.upper()
    )
    root = ET.fromstring(raw.decode("utf-8-sig"))
    suite = root if root.tag == "testsuite" else None
    if root.tag == "testsuites":
        _require(len(root) == 1 and root[0].tag == "testsuite")
        suite = root[0]
    _require(suite is not None)
    _require(
        all(
            suite.get(k) == v
            for k, v in {"tests": "1", "failures": "0", "errors": "0", "skipped": "0"}.items()
        )
    )
    cases = list(root.iter("testcase"))
    _require(len(cases) == 1 and cases[0].get("name") == _TEST)
    _require(suite.findall("testcase") == cases)
    _require(all(n.tag in {"testcase", "properties", "system-out", "system-err"} for n in suite))
    _require(all(n.tag in {"properties", "system-out", "system-err"} for n in cases[0]))
    containers = [*suite.findall("properties"), *cases[0].findall("properties")]
    _require(list(root.iter("properties")) == containers)
    _require(all(n.tag == "property" for c in containers for n in c))
    properties = [n for c in containers for n in c]
    _require(len(properties) == 1 and properties[0].get("name") == _PROPERTY)
    _require(set(properties[0].attrib) == {"name", "value"} and len(properties[0]) == 0)
    return json.loads(
        properties[0].get("value", ""),
        object_pairs_hook=_object,
        parse_constant=_constant,
        parse_float=_finite_float,
    )


def _clock(value):
    _require(type(value) is dict and set(value) == {"guard", "phases"})
    guard = value["guard"]
    _require(type(guard) is dict and set(guard) == _GUARD_FIELDS)
    _require(type(guard["shifts"]) is list and len(guard["shifts"]) == 2)
    for shift in guard["shifts"]:
        _require(
            type(shift) is dict
            and set(shift)
            == {"offset_seconds", "before", "after", "actual_step_seconds", "verified"}
        )
    source = Path(__file__).with_name("verify_windows_vm_clock_receipt.py")
    spec = importlib.util.spec_from_file_location("motion_native_clock_verifier", source)
    _require(spec is not None and spec.loader is not None)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Retain all original Win32, native-monotonic, service restoration and
    # independently armed watchdog requirements from the qualified clock lane.
    module._guard(guard)
    phases = value["phases"]
    _require(type(phases) is list and len(phases) == 2)
    for phase, offset in zip(phases, (172800, -172800), strict=True):
        _require(
            type(phase) is dict
            and set(phase)
            == {
                "offset_seconds",
                "after_step_state",
                "final_state",
                "post_capture_elapsed_seconds",
                "other_recording_grew",
                "utc_restored",
            }
        )
        _require(type(phase["offset_seconds"]) is int and phase["offset_seconds"] == offset)
        _require(phase["after_step_state"] == "post_capture" and phase["final_state"] == "armed")
        _number(phase["post_capture_elapsed_seconds"], 0.5, 8)
        _require(phase["other_recording_grew"] is True and phase["utc_restored"] is True)


def verify(report: Path, *, expected_head: str, installer_sha256: str) -> dict:
    _require(
        type(expected_head) is str and re.fullmatch(r"[a-f0-9]{40}", expected_head) is not None
    )
    _require(type(installer_sha256) is str and _HASH.fullmatch(installer_sha256) is not None)
    _require(isinstance(report, Path) and 0 < report.stat().st_size <= 1024**2)
    value = _witness(report.read_bytes())
    _require(type(value) is dict and set(value) == _TRUE | _FALSE | _FIELDS)
    _require(type(value["schema_version"]) is int and value["schema_version"] == 1)
    _require(value["head_sha"] == expected_head and value["installer_sha256"] == installer_sha256)
    _require(all(value[k] is True for k in _TRUE) and all(value[k] is False for k in _FALSE))
    _require(value["platform"] == "Windows" and value["browser_channel"] == "msedge")
    _require(value["playwright_version"] == "1.58.0")
    _require(
        type(value["browser_version"]) is str
        and re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,3}", value["browser_version"]) is not None
    )
    _number(value["duration_seconds"], 1, 419.999)
    _require(_integer(value["post_capture_seconds"], 3, 3) == 3)
    _require(_integer(value["pre_capture_seconds"], 0, 0) == 0)
    revision = _integer(value["policy_revision_before_restart"], 2)
    _require(_integer(value["policy_revision_after_restart"], 2) == revision)
    for field in ("policy_source_before_restart", "policy_source_after_restart"):
        _require(type(value[field]) is str and _HASH.fullmatch(value[field]) is not None)
    _require(value["policy_source_before_restart"] == value["policy_source_after_restart"])
    _require(
        type(value["preview_geometry"]) is dict
        and value["preview_geometry"] == {"width": 320, "height": 240, "rotation": 0}
    )
    _require(
        type(value["changed_geometry"]) is dict
        and value["changed_geometry"] == {"width": 640, "height": 360}
    )
    for geometry in (value["preview_geometry"], value["changed_geometry"]):
        _require(all(type(number) is int for number in geometry.values()))
    observations = value["observations"]
    _require(type(observations) is dict and set(observations) == set(_OBSERVATIONS))
    for name, (state, grows, minimum_seconds) in _OBSERVATIONS.items():
        observed = observations[name]
        _require(
            type(observed) is dict
            and set(observed)
            == {
                "state",
                "seconds",
                "target_before",
                "target_after",
                "other_before",
                "other_after",
                "publisher_restarts",
            }
        )
        _require(observed["state"] == [state])
        _number(observed["seconds"], minimum_seconds, 12)
        before, after = (_integer(observed[k], 0) for k in ("target_before", "target_after"))
        _require(after > before if grows else after == before)
        _require(_integer(observed["other_after"], 1) > _integer(observed["other_before"], 0))
        _require(
            _integer(observed["publisher_restarts"], 0, 1) == (1 if name == "stale_geometry" else 0)
        )
    _integer(value["retained_file_count"], 1, 64)
    permissions = value["permission_statuses"]
    _require(
        type(permissions) is dict
        and set(permissions)
        == {
            "operator_policy_get",
            "operator_policy_put",
            "operator_preview_post",
            "preview_owner_get",
            "foreign_preview_get",
        }
        and all(type(status) is int for status in permissions.values())
        and all(
            permissions[name] == 403
            for name in (
                "operator_policy_get",
                "operator_policy_put",
                "operator_preview_post",
            )
        )
        and permissions["preview_owner_get"] == 200
        and permissions["foreign_preview_get"] == 410
    )
    retained = value["retained"]
    _require(
        type(retained) is dict
        and set(retained)
        == {
            "history_spans",
            "playback_frames",
            "export_frames",
            "export_bytes",
            "export_sha256",
            "idle_middle_utc",
            "clip_start_utc",
        }
    )
    _integer(retained["history_spans"], 1, 64)
    _integer(retained["playback_frames"], 4, 100000)
    _integer(retained["export_frames"], 4, 100000)
    _integer(retained["export_bytes"], 1, 16 * 1024**2)
    _require(
        type(retained["export_sha256"]) is str
        and _HASH.fullmatch(retained["export_sha256"]) is not None
    )
    _require(_utc(retained["idle_middle_utc"]) < _utc(retained["clip_start_utc"]))
    _clock(value["motion_clock"])
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
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
                "schema_version": 1,
                "head_sha": args.expected_head,
                "installer_sha256": args.installer_sha256,
                "lane": "motion_windows",
                "receipt": receipt,
            },
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print("Installed Windows motion recording, zones and elapsed-clock evidence admitted")


if __name__ == "__main__":
    main()
