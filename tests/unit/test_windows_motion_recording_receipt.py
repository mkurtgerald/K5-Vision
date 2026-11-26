"""Do not publish motion/zones without its actual native, media and clock proof."""

from __future__ import annotations

import copy
import json
import runpy
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parents[2] / "scripts/verify_windows_motion_recording_receipt.py"
_V = runpy.run_path(str(_SCRIPT))
_CLOCK_FIXTURE = runpy.run_path(str(Path(__file__).with_name("test_windows_vm_clock_receipt.py")))
_HEAD, _SETUP = "a" * 40, "b" * 64


def _receipt():
    observations = {}
    for name, (state, grows, seconds) in _V["_OBSERVATIONS"].items():
        observations[name] = {
            "state": [state],
            "seconds": float(seconds),
            "target_before": 100,
            "target_after": 200 if grows else 100,
            "other_before": 100,
            "other_after": 300,
            "publisher_restarts": 1 if name == "stale_geometry" else 0,
        }
    return {
        **dict.fromkeys(_V["_TRUE"], True),
        **dict.fromkeys(_V["_FALSE"], False),
        "schema_version": 1,
        "head_sha": _HEAD,
        "installer_sha256": _SETUP,
        "platform": "Windows",
        "browser_channel": "msedge",
        "browser_version": "156.0.1.1",
        "playwright_version": "1.58.0",
        "duration_seconds": 240.5,
        "policy_revision_before_restart": 2,
        "policy_revision_after_restart": 2,
        "policy_source_before_restart": "c" * 64,
        "policy_source_after_restart": "c" * 64,
        "preview_geometry": {"width": 320, "height": 240, "rotation": 0},
        "changed_geometry": {"width": 640, "height": 360},
        "post_capture_seconds": 3,
        "pre_capture_seconds": 0,
        "retained_file_count": 3,
        "permission_statuses": {
            "operator_policy_get": 403,
            "operator_policy_put": 403,
            "operator_preview_post": 403,
            "preview_owner_get": 200,
            "foreign_preview_get": 410,
        },
        "observations": observations,
        "retained": {
            "history_spans": 2,
            "playback_frames": 8,
            "export_frames": 20,
            "export_bytes": 8192,
            "export_sha256": "d" * 64,
            "idle_middle_utc": "2026-11-16T12:00:00Z",
            "clip_start_utc": "2026-11-16T12:00:08Z",
        },
        "motion_clock": {
            "guard": _CLOCK_FIXTURE["_guard_receipt"](),
            "phases": [
                {
                    "offset_seconds": offset,
                    "after_step_state": "post_capture",
                    "final_state": "armed",
                    "post_capture_elapsed_seconds": 2.5,
                    "other_recording_grew": True,
                    "utc_restored": True,
                }
                for offset in (172800, -172800)
            ],
        },
    }


def _report(tmp_path, value):
    root = ET.Element("testsuites")
    suite = ET.SubElement(root, "testsuite", tests="1", failures="0", errors="0", skipped="0")
    properties = ET.SubElement(suite, "properties")
    ET.SubElement(properties, "property", name=_V["_PROPERTY"], value=json.dumps(value))
    ET.SubElement(suite, "testcase", name=_V["_TEST"])
    path = tmp_path / "motion.xml"
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)
    return path


def _verify(path):
    return _V["verify"](path, expected_head=_HEAD, installer_sha256=_SETUP)


def test_admits_the_complete_installer_bound_motion_and_clock_journey(tmp_path):
    value = _receipt()
    assert _verify(_report(tmp_path, value)) == value


@pytest.mark.parametrize("field", sorted(_V["_TRUE"] | _V["_FALSE"]))
def test_refuses_a_missing_or_reversed_executed_scope_fact(tmp_path, field):
    value = _receipt()
    value[field] = not value[field]
    with pytest.raises(ValueError):
        _verify(_report(tmp_path, value))


@pytest.mark.parametrize(
    "field,value",
    [
        ("head_sha", "e" * 40),
        ("installer_sha256", "e" * 64),
        ("post_capture_seconds", 120),
        ("pre_capture_seconds", 3),
        ("schema_version", True),
        ("owned_cleanup", 1),
        ("duration_seconds", float("nan")),
        ("duration_seconds", float("inf")),
        ("duration_seconds", 420),
        ("policy_revision_after_restart", 3),
        ("policy_source_after_restart", "e" * 64),
        ("foreign", "PRIVATE_SOURCE"),
        ("policy_source_before_restart", "rtsp://PRIVATE_SOURCE"),
    ],
)
def test_refuses_wrong_source_false_buffer_and_lost_restart_authority(tmp_path, field, value):
    receipt = _receipt()
    receipt[field] = value
    with pytest.raises(ValueError):
        _verify(_report(tmp_path, receipt))


@pytest.mark.parametrize("name", sorted(_V["_OBSERVATIONS"]))
def test_refuses_recording_bytes_that_disagree_with_the_actual_gate(tmp_path, name):
    value = _receipt()
    observed = value["observations"][name]
    _, grows, _ = _V["_OBSERVATIONS"][name]
    observed["target_after"] = observed["target_before"] if grows else observed["target_before"] + 1
    with pytest.raises(ValueError):
        _verify(_report(tmp_path, value))


@pytest.mark.parametrize(
    "field,value",
    [
        ("other_after", 100),
        ("seconds", 0.1),
        ("publisher_restarts", 1),
        ("state", ["recording"]),
        ("target_before", True),
        ("foreign", "PRIVATE_PATH"),
    ],
)
def test_masked_only_movement_requires_real_quiet_stable_stream_and_camera_isolation(
    tmp_path, field, value
):
    receipt = _receipt()
    receipt["observations"]["excluded_only"][field] = value
    with pytest.raises(ValueError):
        _verify(_report(tmp_path, receipt))


@pytest.mark.parametrize(
    "fault",
    [
        "wrong_geometry",
        "wrong_rotation",
        "boolean_geometry",
        "same_geometry",
        "no_changed_source",
        "lost_retained",
        "fake_gap",
    ],
)
def test_refuses_geometry_and_real_retained_media_failures(tmp_path, fault):
    value = _receipt()
    if fault == "wrong_geometry":
        value["preview_geometry"]["width"] = 640
    elif fault == "wrong_rotation":
        value["preview_geometry"]["rotation"] = 90
    elif fault == "boolean_geometry":
        value["preview_geometry"]["rotation"] = False
    elif fault == "same_geometry":
        value["changed_geometry"] = {"width": 320, "height": 240}
    elif fault == "no_changed_source":
        value["observations"]["stale_geometry"]["publisher_restarts"] = 0
    elif fault == "lost_retained":
        value["retained"]["export_frames"] = 0
    else:
        value["retained"]["idle_middle_utc"] = value["retained"]["clip_start_utc"]
    with pytest.raises(ValueError):
        _verify(_report(tmp_path, value))


@pytest.mark.parametrize(
    "name",
    [
        "operator_policy_get",
        "operator_policy_put",
        "operator_preview_post",
        "foreign_preview_get",
    ],
)
def test_new_motion_controls_and_preview_remain_permission_and_owner_bound(tmp_path, name):
    value = _receipt()
    value["permission_statuses"][name] = 200
    with pytest.raises(ValueError):
        _verify(_report(tmp_path, value))


@pytest.mark.parametrize(
    "field,value",
    [
        ("native_clock_api", "simulated"),
        ("native_elapsed_api", "Date.now"),
        ("watchdog_armed", False),
        ("watchdog_seconds", 91),
        ("clock_restored", False),
        ("services_restored", False),
        ("foreign", "PRIVATE_PATH"),
    ],
)
def test_motion_clock_proof_requires_actual_guarded_restored_windows_clock(tmp_path, field, value):
    receipt = _receipt()
    receipt["motion_clock"]["guard"][field] = value
    with pytest.raises(ValueError):
        _verify(_report(tmp_path, receipt))


@pytest.mark.parametrize(
    "field,value",
    [
        ("offset_seconds", 0),
        ("after_step_state", "armed"),
        ("final_state", "post_capture"),
        ("post_capture_elapsed_seconds", 0),
        ("post_capture_elapsed_seconds", 9),
        ("other_recording_grew", False),
        ("utc_restored", False),
        ("foreign", "PRIVATE_SOURCE"),
    ],
)
def test_wall_steps_cannot_expire_early_or_extend_motion_hold_forever(tmp_path, field, value):
    receipt = _receipt()
    receipt["motion_clock"]["phases"][1][field] = value
    with pytest.raises(ValueError):
        _verify(_report(tmp_path, receipt))


@pytest.mark.parametrize(
    "fault",
    [
        "wrong_step",
        "inconsistent_native_ticks",
        "services_changed",
        "unverified_step",
        "foreign_step",
    ],
)
def test_reuses_all_native_clock_and_service_integrity_requirements(tmp_path, fault):
    value = _receipt()
    guard = value["motion_clock"]["guard"]
    if fault == "wrong_step":
        guard["shifts"][0]["actual_step_seconds"] = 0
    elif fault == "inconsistent_native_ticks":
        guard["shifts"][1]["after"]["tick_ms"] += 5000
    elif fault == "services_changed":
        guard["services_after"][0]["running"] = False
    elif fault == "unverified_step":
        guard["shifts"][0]["verified"] = False
    else:
        guard["shifts"][0]["PRIVATE_SOURCE"] = "PRIVATE_SOURCE"
    with pytest.raises(ValueError):
        _verify(_report(tmp_path, value))


@pytest.mark.parametrize(
    "fault",
    [
        "failed",
        "error",
        "skipped",
        "wrong_test",
        "duplicate_receipt",
        "foreign_property",
        "hidden_case",
        "nested_suite",
        "doctype",
        "nonfinite_number",
        "duplicate_json_key",
    ],
)
def test_only_one_exact_passing_raw_motion_witness_can_be_admitted(tmp_path, fault):
    path = _report(tmp_path, _receipt())
    root = ET.parse(path).getroot()
    suite, case = root.find("testsuite"), root.find(".//testcase")
    if fault in {"failed", "error", "skipped"}:
        tag = "failure" if fault == "failed" else fault
        ET.SubElement(case, tag)
    elif fault == "wrong_test":
        case.set("name", "unexecuted_test")
    elif fault == "duplicate_receipt":
        prop = suite.find("properties/property")
        suite.find("properties").append(copy.deepcopy(prop))
    elif fault == "foreign_property":
        ET.SubElement(suite.find("properties"), "property", name="foreign", value="PRIVATE_PATH")
    elif fault == "hidden_case":
        ET.SubElement(ET.SubElement(case, "system-out"), "testcase", name="hidden")
    elif fault == "nested_suite":
        ET.SubElement(case, "testsuite")
    elif fault in {"nonfinite_number", "duplicate_json_key"}:
        prop = suite.find("properties/property")
        raw = prop.get("value")
        prop.set(
            "value",
            raw.replace(
                '"schema_version": 1',
                '"schema_version": 1e309'
                if fault == "nonfinite_number"
                else '"schema_version": 1,"schema_version": 1',
            ),
        )
    if fault == "doctype":
        path.write_bytes(b'<!DOCTYPE testsuites [<!ENTITY x "PRIVATE">]>' + ET.tostring(root))
    else:
        ET.ElementTree(root).write(path, encoding="utf-8")
    with pytest.raises((ValueError, ET.ParseError)):
        _verify(path)
