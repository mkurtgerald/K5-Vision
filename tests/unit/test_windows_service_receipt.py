"""Reject incomplete/configuration-only claims of native unattended recording."""

from __future__ import annotations

import copy
import json
import runpy
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

_VERIFIER = runpy.run_path(
    str(Path(__file__).parents[2] / "scripts/verify_windows_service_receipt.py")
)
_HEAD, _SETUP = "a" * 40, "b" * 64


def _receipt():
    observations = {}
    for index, phase in enumerate(_VERIFIER["_PHASES"]):
        generation = max(index - 1, 0)
        observations[phase] = {
            "service_pid": 100 + generation,
            "service_created": 1700000000.0 + generation,
            "backend_pid": 200 + generation,
            "backend_created": 1700000000.5 + generation,
            "service_session": 0,
            "backend_session": 0,
            "service_account_verified": True,
            "backend_account_verified": True,
            "recording_bytes_before": index * 1000,
            "recording_bytes_after": index * 1000 + 500,
        }
    return {
        **dict.fromkeys(_VERIFIER["_TRUE"], True),
        **dict.fromkeys(_VERIFIER["_FALSE"], False),
        "schema_version": 1,
        "head_sha": _HEAD,
        "installer_sha256": _SETUP,
        "platform": "Windows",
        "duration_seconds": 120.5,
        "browser_channel": "msedge",
        "browser_version": "142.0.1.1",
        "playwright_version": "1.58.0",
        "process_ownership": {
            **dict.fromkeys(_VERIFIER["_OWNERSHIP_TRUE"], True),
            "admitted_service_images": sorted(_VERIFIER["_OWNERSHIP_IMAGES"]),
            "admitted_service_count": 20,
            "rejected_foreign_count": 0,
            "unreadable_identity_count": 0,
            "unexpected_service_image_count": 0,
            "console_hosts": {
                **dict.fromkeys(_VERIFIER["_CONSOLE_TRUE"], True),
                "observed_count": 4,
                "exited_count": 4,
                "individual_termination_used": False,
            },
        },
        "scm": {
            "service_type": 16,
            "start_type": 2,
            "account": r"NT SERVICE\K5VisionRecorder",
            "reset_seconds": 3600,
            "failure_actions": [{"type": 1, "delay_ms": delay} for delay in (5000, 15000, 60000)],
            "installed_executable_verified": True,
        },
        "observations": observations,
        "standard_windows_user": {
            **dict.fromkeys(_VERIFIER["_STANDARD_TRUE"], True),
            **dict.fromkeys(_VERIFIER["_STANDARD_FALSE"], False),
            "slow_status_seconds": 5.01,
            "product_status_success_count": 4,
            "probe_executable_sha256": "f" * 64,
            "recording_bytes_before": 100,
            "recording_bytes_after": 200,
        },
        "startup_shutdown": {
            **dict.fromkeys(_VERIFIER["_STARTUP_TRUE"], True),
            "previous_host_pid": 300,
            "previous_host_created_100ns": 133000000000000000,
            "host_pid": 400,
            "host_created_100ns": 133000000100000000,
            "observed_stage": "payload_verify",
            "stop_seconds": 12.5,
        },
        "retained_file_hashes_before": ["c" * 64],
        "retained_file_hashes_after": ["c" * 64],
        **{
            name + suffix: "d" * 64
            for name in ("site", "source", "users", "storage")
            for suffix in ("_hash_before", "_hash_after")
        },
        "retained_user_count": 2,
        "retained_camera_count": 1,
        "playback_decoded_frames": 5,
        "export_decoded_frames": 10,
        "export_bytes": 4096,
        "export_sha256": "e" * 64,
        "clock_health_before_restart": {
            "status": "observing",
            "sync_verified": False,
            "server_utc": "2026-11-21T12:00:00Z",
        },
        "clock_health_after_restart": {
            "status": "observing",
            "sync_verified": False,
            "server_utc": "2026-11-21T12:01:00Z",
        },
    }


def _report(tmp_path, receipt):
    root = ET.Element("testsuites")
    suite = ET.SubElement(root, "testsuite", tests="1", failures="0", errors="0", skipped="0")
    ET.SubElement(suite, "testcase", name=_VERIFIER["_TEST"])
    properties = ET.SubElement(suite, "properties")
    ET.SubElement(properties, "property", name=_VERIFIER["_PROPERTY"], value=json.dumps(receipt))
    path = tmp_path / "service-windows.xml"
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)
    return path


def _verify(report):
    return _VERIFIER["verify"](report, expected_head=_HEAD, installer_sha256=_SETUP)


def test_service_receipt_accepts_only_installer_bound_real_process_and_recording_observations(
    tmp_path,
):
    value = _receipt()
    assert _verify(_report(tmp_path, value)) == value


@pytest.mark.parametrize(
    "change",
    [
        {"service_token_verified": False},
        {"service_token_verified": 1},
        {"session_zero_verified": False},
        {"installed_images_verified": False},
        {"admitted_service_images": ["host", "python", "wrapper"]},
        {"admitted_service_images": ["caddy", "host", "mediamtx", "python", "other"]},
        {"admitted_service_count": 4},
        {"admitted_service_count": 4097},
        {"admitted_service_count": 4096, "rejected_foreign_count": 1},
        {"rejected_foreign_count": -1},
        {"rejected_foreign_count": True},
        {"unreadable_identity_count": 1},
        {"unreadable_identity_count": False},
        {"unexpected_service_image_count": 1},
        {"unexpected_service_image_count": False},
        {"private_path": "must-not-qualify"},
    ],
)
def test_service_receipt_requires_actual_token_session_and_installed_image_ownership(
    tmp_path, change
):
    value = _receipt()
    value["process_ownership"].update(change)
    with pytest.raises(ValueError):
        _verify(_report(tmp_path, value))


def test_service_receipt_cannot_reuse_an_older_cleanup_only_receipt(tmp_path):
    value = _receipt()
    value.pop("process_ownership")
    with pytest.raises(ValueError):
        _verify(_report(tmp_path, value))


@pytest.mark.parametrize(
    "change",
    [
        {"system_file_identity_verified": False},
        {"system_file_identity_verified": 1},
        {"trusted_code_custody_verified": False},
        {"admitted_parent_identity_verified": False},
        {"pinned_native_exit_verified": False},
        {"individual_termination_used": True},
        {"individual_termination_used": 0},
        {"observed_count": -1},
        {"observed_count": True},
        {"observed_count": 4097},
        {"exited_count": 3},
        {"exited_count": False},
        {"unexpected_field": "refused"},
    ],
)
def test_service_receipt_requires_authorized_console_hosts_to_exit_without_individual_kill(
    tmp_path, change
):
    value = _receipt()
    value["process_ownership"]["console_hosts"].update(change)
    with pytest.raises(ValueError):
        _verify(_report(tmp_path, value))


def test_service_receipt_allows_no_console_host_without_claiming_one_was_observed(tmp_path):
    value = _receipt()
    value["process_ownership"]["console_hosts"].update(observed_count=0, exited_count=0)
    assert _verify(_report(tmp_path, value)) == value


def test_service_receipt_requires_optional_console_host_lifetime_evidence_field(tmp_path):
    value = _receipt()
    value["process_ownership"].pop("console_hosts")
    with pytest.raises(ValueError):
        _verify(_report(tmp_path, value))


@pytest.mark.parametrize(
    "field", sorted(_VERIFIER["_STANDARD_TRUE"] | _VERIFIER["_STANDARD_FALSE"])
)
def test_service_receipt_requires_real_ordinary_user_boundary_and_cleanup(tmp_path, field):
    value = _receipt()
    value["standard_windows_user"][field] = int(value["standard_windows_user"][field])
    with pytest.raises(ValueError):
        _verify(_report(tmp_path, value))


@pytest.mark.parametrize(
    "change",
    [
        {"administrator_group_present": True},
        {"token_elevated": True},
        {"product_status_success_count": 3},
        {"slow_status_seconds": 0.1},
        {"slow_status_seconds": 9},
        {"recording_bytes_after": 100},
        {"generated_account_removed": False},
        {"probe_executable_sha256": "invalid"},
    ],
    ids=[
        "admin-group",
        "elevated",
        "missing-reconnect",
        "no-timeout",
        "unbounded",
        "no-recording",
        "account-left",
        "unbound-probe",
    ],
)
def test_service_receipt_rejects_false_ordinary_user_or_reconnect_proof(tmp_path, change):
    value = _receipt()
    value["standard_windows_user"].update(change)
    with pytest.raises(ValueError):
        _verify(_report(tmp_path, value))


@pytest.mark.parametrize(
    "change",
    [
        {"native_start_without_readiness": False},
        {"new_host_verified": 1},
        {"observed_stage": "running"},
        {"observed_stage": "account_required"},
        {"host_pid": 300, "host_created_100ns": 133000000000000000},
        {"host_created_100ns": True},
        {"scm_stopped": False},
        {"owned_children_exited": False},
        {"stop_seconds": 70},
    ],
    ids=[
        "waited-ready",
        "unverified-host",
        "already-running",
        "already-ready",
        "old-host",
        "invalid-birth",
        "scm-live",
        "orphan",
        "deadline",
    ],
)
def test_service_receipt_requires_real_new_host_pre_ready_shutdown_with_original_budget(
    tmp_path, change
):
    value = _receipt()
    value["startup_shutdown"].update(change)
    with pytest.raises(ValueError):
        _verify(_report(tmp_path, value))


@pytest.mark.parametrize(
    "field", sorted(_VERIFIER["_TRUE"] | _VERIFIER["_FALSE"] | _VERIFIER["_FIELDS"])
)
def test_service_receipt_requires_every_claim_and_observation(tmp_path, field):
    value = _receipt()
    value.pop(field)
    with pytest.raises(ValueError):
        _verify(_report(tmp_path, value))


@pytest.mark.parametrize("field", sorted(_VERIFIER["_TRUE"] | _VERIFIER["_FALSE"]))
def test_service_receipt_refuses_integer_boolean_claims(tmp_path, field):
    value = _receipt()
    value[field] = int(value[field])
    with pytest.raises(ValueError):
        _verify(_report(tmp_path, value))


@pytest.mark.parametrize(
    "field", ["actual_reboot_before_login", "actual_windows_logoff", "actual_clock_corrections"]
)
def test_hosted_service_lane_cannot_turn_configuration_into_reboot_logoff_or_clock_proof(
    tmp_path, field
):
    value = _receipt()
    value[field] = True
    with pytest.raises(ValueError):
        _verify(_report(tmp_path, value))


@pytest.mark.parametrize("phase", _VERIFIER["_PHASES"])
@pytest.mark.parametrize(
    "field",
    [
        "service_session",
        "backend_session",
        "service_account_verified",
        "backend_account_verified",
        "recording_bytes_after",
    ],
)
def test_service_receipt_rejects_interactive_wrong_account_or_stalled_recording(
    tmp_path, phase, field
):
    value = _receipt()
    current = value["observations"][phase]
    current[field] = (
        1
        if field.endswith("session")
        else False
        if field.endswith("verified")
        else current["recording_bytes_before"]
    )
    with pytest.raises(ValueError):
        _verify(_report(tmp_path, value))


@pytest.mark.parametrize("phase", ["backend_recovered", "wrapper_recovered"])
def test_service_receipt_requires_actual_scm_and_worker_process_replacement(tmp_path, phase):
    value = _receipt()
    prior = "client_closed" if phase == "backend_recovered" else "backend_recovered"
    for name in ("service", "backend"):
        changed = copy.deepcopy(value)
        for suffix in ("_pid", "_created"):
            changed["observations"][phase][name + suffix] = value["observations"][prior][
                name + suffix
            ]
        with pytest.raises(ValueError):
            _verify(_report(tmp_path, changed))


@pytest.mark.parametrize(
    "change",
    [
        {"start_type": 3},
        {"service_type": 0x110},
        {"account": "LocalSystem"},
        {"failure_actions": [{"type": 1, "delay_ms": 5000}]},
        {"failure_actions": [{"type": 0, "delay_ms": d} for d in (5000, 15000, 60000)]},
        {"installed_executable_verified": 1},
    ],
    ids=[
        "manual-start",
        "interactive",
        "system-account",
        "missing-retries",
        "no-restart",
        "unbound-exe",
    ],
)
def test_service_receipt_requires_native_auto_start_and_restart_policy(tmp_path, change):
    value = _receipt()
    value["scm"].update(change)
    with pytest.raises(ValueError):
        _verify(_report(tmp_path, value))


@pytest.mark.parametrize(
    "field",
    [
        "site_hash_after",
        "source_hash_after",
        "users_hash_after",
        "storage_hash_after",
        "retained_file_hashes_after",
    ],
)
def test_service_receipt_refuses_identity_source_authority_policy_or_closed_media_loss(
    tmp_path, field
):
    value = _receipt()
    value[field] = ["f" * 64] if field.startswith("retained") else "f" * 64
    with pytest.raises(ValueError):
        _verify(_report(tmp_path, value))


def test_service_receipt_refuses_skips_duplicate_json_and_private_extra_material(tmp_path):
    value = _receipt()
    value["private_path"] = "C:/private"
    with pytest.raises(ValueError):
        _verify(_report(tmp_path, value))
    report = _report(tmp_path, _receipt())
    root = ET.parse(report)
    root.find("testsuite").set("skipped", "1")
    root.write(report)
    with pytest.raises(ValueError):
        _verify(report)
    report = _report(tmp_path, _receipt())
    root = ET.parse(report)
    prop = root.find(".//property")
    prop.set("value", '{"schema_version":1,' + prop.get("value")[1:])
    root.write(report)
    with pytest.raises(ValueError):
        _verify(report)
