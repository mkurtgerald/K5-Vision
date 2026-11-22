"""Validate installer-bound native migration evidence from generated media only."""

from __future__ import annotations

import argparse
import json
import math
import re
import xml.etree.ElementTree as ET
from pathlib import Path

_TEST = "test_native_windows_legacy_service_migration_and_manual_reversal"
_PROPERTY = "legacy_migration_windows_receipt"
_DIAGNOSTIC = "legacy_migration_windows_diagnostic"
_HEAD = re.compile(r"[a-f0-9]{40}\Z")
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_TRUE = {
    "generated_media_only",
    "actual_graphical_baseline_account",
    "actual_native_migration_wizard",
    "existing_admin_password_required",
    "existing_mfa_required",
    "same_windows_owner_bound",
    "actual_service_token_verified",
    "service_token_dpapi_rewrapped",
    "original_profile_preserved_after_authorization",
    "retained_site_identity",
    "retained_users_and_grants",
    "retained_camera_sources",
    "storage_policy_rebased",
    "retained_closed_footage",
    "retained_completed_export",
    "retained_playback_decoded",
    "retained_export_decoded",
    "client_closed_recording_continues",
    "cancel_refused_activation",
    "authenticated_pending_recovery",
    "candidate_uninstall_preserved_original",
    "manual_reversal_requires_candidate_uninstall",
    "baseline_reversal_mfa_login",
    "owned_cleanup",
    "no_console_windows",
}
_FALSE = {
    "mocked_gui",
    "mocked_service",
    "user_terminal_required",
    "physical_camera_acceptance",
    "actual_reboot_before_login",
    "actual_windows_logoff",
    "actual_clock_corrections",
    "endurance_acceptance",
    "cross_user_import_qualified",
    "automatic_cross_binding_media_resume",
    "automatic_failed_activation_rollback",
}
_FIELDS = {
    "schema_version",
    "baseline_head",
    "candidate_head",
    "baseline_installer_sha256",
    "candidate_installer_sha256",
    "platform",
    "duration_seconds",
    "actual_graphical_installations",
    "retained",
    "security",
    "media",
    "recording",
    "interruption",
    "native_copy",
}
_RETAINED_PAIRS = ("site", "users", "grants", "cameras", "storage_policy")
_RETAINED_FIELDS = {
    *(name + "_hash_" + phase for name in _RETAINED_PAIRS for phase in ("before", "after")),
    "user_count",
    "camera_count",
    "storage_revision_before",
    "storage_revision_after",
    "closed_hashes_before",
    "closed_hashes_after",
    "export_hashes_before",
    "export_hashes_after",
}
_SECURITY_FIELDS = {
    "source_cipher_hash_before",
    "source_cipher_hash_after",
    "service_cipher_hash",
    "mfa_generation_hash_before",
    "mfa_generation_hash_after",
    "migration_factor_replay_refused",
    "service_mfa_login_succeeded",
    "old_profile_mfa_login_after_reversal",
}


def _require(condition):
    if not condition:
        raise ValueError("Native Windows legacy migration evidence refused")


def _integer(value, minimum=0, maximum=2**63 - 1):
    _require(type(value) is int and minimum <= value <= maximum)
    return value


def _number(value, minimum, maximum):
    _require(type(value) in (int, float) and math.isfinite(value) and minimum <= value <= maximum)
    return value


def _hash(value):
    _require(type(value) is str and _HASH.fullmatch(value) is not None)
    return value


def _object(pairs):
    value = {}
    for key, item in pairs:
        _require(key not in value)
        value[key] = item
    return value


def _constant(_value):
    raise ValueError("Nonfinite native migration evidence refused")


def _finite(value):
    if type(value) is dict:
        for item in value.values():
            _finite(item)
    elif type(value) is list:
        for item in value:
            _finite(item)
    elif type(value) is float:
        _require(math.isfinite(value))


def _witness(raw):
    _require(type(raw) is bytes and 0 < len(raw) <= 1024**2)
    _require(
        b"\0" not in raw and b"<!DOCTYPE" not in raw.upper() and b"<!ENTITY" not in raw.upper()
    )
    root = ET.fromstring(raw.decode("utf-8-sig"))
    suite = root if root.tag == "testsuite" else None
    if root.tag == "testsuites":
        _require(len(root) == 1 and root[0].tag == "testsuite")
        for key, expected in {"tests": "1", "failures": "0", "errors": "0", "skipped": "0"}.items():
            _require(root.get(key) is None or root.get(key) == expected)
        suite = root[0]
    _require(suite is not None)
    _require(
        all(
            suite.get(key) == value
            for key, value in {
                "tests": "1",
                "failures": "0",
                "errors": "0",
                "skipped": "0",
            }.items()
        )
    )
    cases = list(root.iter("testcase"))
    _require(len(cases) == 1 and cases[0].get("name") == _TEST)
    _require(suite.findall("testcase") == cases)
    _require(
        all(node.tag in {"testcase", "properties", "system-out", "system-err"} for node in suite)
    )
    _require(all(node.tag in {"properties", "system-out", "system-err"} for node in cases[0]))
    _require(
        all(len(node) == 0 for node in root.iter() if node.tag in {"system-out", "system-err"})
    )
    containers = [*suite.findall("properties"), *cases[0].findall("properties")]
    _require(list(root.iter("properties")) == containers)
    _require(all(node.tag == "property" for container in containers for node in container))
    properties = [node for container in containers for node in container]
    _require(1 <= len(properties) <= 2)
    observed = {}
    for node in properties:
        _require(set(node.attrib) == {"name", "value"} and len(node) == 0)
        name = node.get("name")
        _require(name in {_PROPERTY, _DIAGNOSTIC} and name not in observed)
        payload = node.get("value", "")
        _require(name != _DIAGNOSTIC or len(payload.encode("utf-8")) <= 65536)
        item = json.loads(payload, object_pairs_hook=_object, parse_constant=_constant)
        _require(type(item) is dict)
        _finite(item)
        observed[name] = item
    _require(_PROPERTY in observed)
    value = observed[_PROPERTY]
    _finite(value)
    return value


def _shape(value, fields):
    _require(type(value) is dict and set(value) == fields)


def _equal_hashes(value, name):
    _require(_hash(value[name + "_before"]) == _hash(value[name + "_after"]))


def _hashes(value, minimum, maximum):
    _require(type(value) is list and minimum <= len(value) <= maximum)
    for item in value:
        _hash(item)
    _require(value == sorted(value))
    return value


def _validate(value, *, baseline_head, head_sha, installer_sha256, baseline_installer_sha256):
    _shape(value, _FIELDS | _TRUE | _FALSE)
    _require(all(value[name] is True for name in _TRUE))
    _require(all(value[name] is False for name in _FALSE))
    _require(type(value["schema_version"]) is int and value["schema_version"] == 1)
    _require(value["baseline_head"] == baseline_head and value["candidate_head"] == head_sha)
    _require(_hash(value["candidate_installer_sha256"]) == installer_sha256)
    _hash(value["baseline_installer_sha256"])
    if baseline_installer_sha256 is not None:
        _require(value["baseline_installer_sha256"] == baseline_installer_sha256)
    _shape(value["platform"], {"system", "release", "architecture"})
    platform = value["platform"]
    _require(platform["system"] == "Windows" and platform["architecture"] == "AMD64")
    _require(type(platform["release"]) is str and 1 <= len(platform["release"]) <= 80)
    _number(value["duration_seconds"], 1, 1800)
    _integer(value["actual_graphical_installations"], 3, 3)

    retained = value["retained"]
    _shape(retained, _RETAINED_FIELDS)
    for name in _RETAINED_PAIRS:
        _equal_hashes(retained, name + "_hash")
    _integer(retained["user_count"], 2, 10)
    _integer(retained["camera_count"], 1, 4)
    revision = _integer(retained["storage_revision_before"], 0, 2**63 - 2)
    _require(_integer(retained["storage_revision_after"], 1) == revision + 1)
    for name, minimum, maximum in (("closed", 1, 64), ("export", 2, 32)):
        before = _hashes(retained[name + "_hashes_before"], minimum, maximum)
        _require(before == _hashes(retained[name + "_hashes_after"], minimum, maximum))

    security = value["security"]
    _shape(security, _SECURITY_FIELDS)
    _equal_hashes(security, "source_cipher_hash")
    _equal_hashes(security, "mfa_generation_hash")
    _require(_hash(security["service_cipher_hash"]) != security["source_cipher_hash_before"])
    for name in (
        "migration_factor_replay_refused",
        "service_mfa_login_succeeded",
        "old_profile_mfa_login_after_reversal",
    ):
        _require(security[name] is True)

    media = value["media"]
    _shape(
        media, {"playback_decoded_frames", "export_decoded_frames", "export_bytes", "export_sha256"}
    )
    for name in ("playback_decoded_frames", "export_decoded_frames"):
        _integer(media[name], 8, 10000)
    _integer(media["export_bytes"], 1024, 64 * 1024**2)
    _hash(media["export_sha256"])

    recording = value["recording"]
    _shape(
        recording,
        {
            "before_bytes",
            "after_bytes",
            "service_pid_before",
            "service_pid_after",
            "backend_pid_before",
            "backend_pid_after",
            "service_created_100ns_before",
            "service_created_100ns_after",
            "backend_created_100ns_before",
            "backend_created_100ns_after",
            "session",
        },
    )
    before = _integer(recording["before_bytes"], 1, 256 * 1024**2 - 1)
    _integer(recording["after_bytes"], before + 1, 256 * 1024**2)
    for name in ("service", "backend"):
        pid = _integer(recording[name + "_pid_before"], 1, 2**32 - 1)
        _require(_integer(recording[name + "_pid_after"], 1, 2**32 - 1) == pid)
        birth = _integer(recording[name + "_created_100ns_before"], 1)
        _require(_integer(recording[name + "_created_100ns_after"], 1) == birth)
    _integer(recording["session"], 0, 0)

    interruption = value["interruption"]
    true = {
        "pending_before_retry",
        "identity_absent_after_cancel",
        "source_unchanged_after_cancel",
        "retired_stage_bytes_preserved",
    }
    _shape(interruption, true | {"cancelled_stage_count", "recovered_stage_count"})
    _require(all(interruption[name] is True for name in true))
    for name in ("cancelled_stage_count", "recovered_stage_count"):
        _integer(interruption[name], 1, 16)
    native = value["native_copy"]
    _shape(native, {"test_name", "executed", "skipped", "passed"})
    _require(native["test_name"] == "test_native_migration_copy_windows_actual_security_and_io")
    _require(native["executed"] is True and native["passed"] is True and native["skipped"] is False)
    return value


def validate_receipt(
    value,
    *,
    baseline_head: str,
    head_sha: str,
    installer_sha256: str,
    baseline_installer_sha256: str | None = None,
) -> dict:
    failed = False
    try:
        for head in (baseline_head, head_sha):
            _require(type(head) is str and _HEAD.fullmatch(head) is not None)
        _hash(installer_sha256)
        if baseline_installer_sha256 is not None:
            _hash(baseline_installer_sha256)
        _finite(value)
        return _validate(
            value,
            baseline_head=baseline_head,
            head_sha=head_sha,
            installer_sha256=installer_sha256,
            baseline_installer_sha256=baseline_installer_sha256,
        )
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError, ET.ParseError):
        failed = True
    if failed:
        raise ValueError("Native Windows legacy migration evidence refused")


def verify(
    report: Path,
    *,
    baseline_head: str,
    head_sha: str,
    installer_sha256: str,
    baseline_installer_sha256: str | None = None,
) -> dict:
    failed = False
    try:
        _require(0 < report.stat().st_size <= 1024**2)
        with report.open("rb") as source:
            value = _witness(source.read(1024**2 + 1))
        return validate_receipt(
            value,
            baseline_head=baseline_head,
            head_sha=head_sha,
            installer_sha256=installer_sha256,
            baseline_installer_sha256=baseline_installer_sha256,
        )
    except (OSError, ValueError, TypeError, KeyError, OverflowError, RecursionError, ET.ParseError):
        failed = True
    if failed:
        raise ValueError("Native Windows legacy migration evidence refused")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--junit", required=True, type=Path)
    parser.add_argument("--baseline-head", required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--installer-sha256", required=True)
    parser.add_argument("--baseline-installer-sha256")
    args = parser.parse_args(argv)
    try:
        value = verify(
            args.junit,
            baseline_head=args.baseline_head,
            head_sha=args.head_sha,
            installer_sha256=args.installer_sha256,
            baseline_installer_sha256=args.baseline_installer_sha256,
        )
    except ValueError:
        parser.exit(1, "Native Windows legacy migration evidence refused\n")
    print(json.dumps(value, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
