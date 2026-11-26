"""Admit real installed SCM recording/recovery evidence, never boot configuration as boot proof."""

from __future__ import annotations

import argparse
import json
import math
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

_TEST = "test_native_windows_unattended_service_recording_and_recovery"
_PROPERTY = "service_windows_receipt"
_TRUE = {
    "native_windows_execution",
    "generated_media_only",
    "actual_gui_installer",
    "actual_gui_account_setup",
    "actual_edge_ui",
    "installed_worker_bundle_verified",
    "actual_scm_readback",
    "client_quit_recording_continues",
    "backend_failure_scm_recovery",
    "wrapper_failure_scm_recovery",
    "retained_files_preserved",
    "retained_site_identity",
    "retained_camera_sources",
    "retained_users_roles",
    "retained_storage_policy",
    "service_token_mfa_decrypted_after_restart",
    "service_token_oidc_secret_after_restart",
    "mfa_required_after_restart",
    "retained_playback_decoded",
    "retained_export_decoded",
    "service_removed_on_uninstall",
    "uninstall_preserves_profile",
    "owned_cleanup",
    "no_console_windows",
}
_FALSE = {
    "actual_reboot_before_login",
    "actual_windows_logoff",
    "actual_clock_corrections",
    "physical_camera_acceptance",
    "capacity_acceptance",
    "endurance_acceptance",
    "mocked_service",
    "mocked_gui",
    "user_terminal_required",
}
_FIELDS = {
    "schema_version",
    "head_sha",
    "installer_sha256",
    "platform",
    "duration_seconds",
    "browser_channel",
    "browser_version",
    "playwright_version",
    "scm",
    "observations",
    "retained_file_hashes_before",
    "retained_file_hashes_after",
    "site_hash_before",
    "site_hash_after",
    "source_hash_before",
    "source_hash_after",
    "users_hash_before",
    "users_hash_after",
    "storage_hash_before",
    "storage_hash_after",
    "retained_user_count",
    "retained_camera_count",
    "playback_decoded_frames",
    "export_decoded_frames",
    "export_bytes",
    "export_sha256",
    "clock_health_before_restart",
    "clock_health_after_restart",
    "standard_windows_user",
    "startup_shutdown",
    "oidc_restart",
    "process_ownership",
}
_OWNERSHIP_TRUE = {
    "service_token_verified",
    "session_zero_verified",
    "installed_images_verified",
}
_OWNERSHIP_IMAGES = {"wrapper", "host", "python", "mediamtx", "caddy"}
_OWNERSHIP_FIELDS = {
    "admitted_service_images",
    "admitted_service_count",
    "rejected_foreign_count",
    "unreadable_identity_count",
    "unexpected_service_image_count",
    "console_hosts",
}
_CONSOLE_TRUE = {
    "system_file_identity_verified",
    "trusted_code_custody_verified",
    "admitted_parent_identity_verified",
    "pinned_native_exit_verified",
}
_CONSOLE_FIELDS = {"observed_count", "exited_count", "individual_termination_used"}
_PHASES = ("initial", "client_closed", "backend_recovered", "wrapper_recovered")
_OBSERVATION = {
    "service_pid",
    "service_created",
    "backend_pid",
    "backend_created",
    "service_session",
    "backend_session",
    "service_account_verified",
    "backend_account_verified",
    "recording_bytes_before",
    "recording_bytes_after",
}
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_STANDARD_TRUE = {
    "startup_identity_timestamp_order_independent",
    "ordinary_token_user_verified",
    "builtin_users_group_present",
    "admin_pipe_denied",
    "control_pipe_denied",
    "malformed_status_reconnect",
    "eof_status_reconnect",
    "slow_status_reconnect",
    "actual_product_client",
    "generated_account_removed",
    "generated_profile_removed",
    "generated_probe_tree_removed",
    "service_identity_unchanged",
}
_STANDARD_FALSE = {"administrator_group_present", "token_elevated"}
_STANDARD_FIELDS = {
    "slow_status_seconds",
    "product_status_success_count",
    "probe_executable_sha256",
    "recording_bytes_before",
    "recording_bytes_after",
}
_STARTUP_TRUE = {
    "native_start_without_readiness",
    "new_host_verified",
    "scm_stopped",
    "owned_children_exited",
}
_STARTUP_FIELDS = {
    "previous_host_pid",
    "previous_host_created_100ns",
    "host_pid",
    "host_created_100ns",
    "observed_stage",
    "stop_seconds",
}
_OIDC_TRUE = {
    "actual_gui_secret_configuration",
    "secret_write_only",
    "wrong_secret_callback_denied",
    "wrong_secret_issued_session_denied",
    "revoked_session_denied",
    "unlinked_identity_callback_denied",
    "provider_disabled_after_check",
    "idp_owned_cleanup",
}
_OIDC_PHASES = ("initial", "backend_recovered", "wrapper_recovered")
_OIDC_IDENTITY = {"service_pid", "service_created", "backend_pid", "backend_created"}
_OIDC_HASHES = {"protected_secret_sha256", "provider_revision_sha256"}
_OIDC_PHASE_TRUE = {"signed_pkce_secret_exchange", "linked_viewer_role_preserved"}
_OIDC_COUNTERS = {
    "token_requests",
    "authenticated_token_requests",
    "client_auth_rejections",
    "pkce_verifications",
    "signed_tokens",
}


def _oidc_observation(value):
    _require(type(value) is dict and set(value) == _OIDC_COUNTERS | {"failed"})
    _require(value["failed"] is False)
    for key in _OIDC_COUNTERS:
        _integer(value[key], 0, 16)


def _oidc_restart(value, observations):
    _require(
        type(value) is dict and set(value) == _OIDC_TRUE | {"phases", "final_provider_observations"}
    )
    _require(all(value[key] is True for key in _OIDC_TRUE))
    phases = value["phases"]
    _require(type(phases) is dict and set(phases) == set(_OIDC_PHASES))
    previous = {
        "token_requests": 1,
        "authenticated_token_requests": 0,
        "client_auth_rejections": 1,
        "pkce_verifications": 0,
        "signed_tokens": 0,
        "failed": False,
    }
    fingerprints = set()
    for phase in _OIDC_PHASES:
        current = phases[phase]
        _require(
            type(current) is dict
            and set(current)
            == _OIDC_IDENTITY | _OIDC_HASHES | _OIDC_PHASE_TRUE | {"before", "after"}
        )
        _require(all(current[key] is True for key in _OIDC_PHASE_TRUE))
        for prefix in ("service", "backend"):
            _integer(current[prefix + "_pid"], 1, 2**32 - 1)
            _number(current[prefix + "_created"], 1, 2**53)
        _require(all(current[key] == observations[phase][key] for key in _OIDC_IDENTITY))
        for key in _OIDC_HASHES:
            _require(type(current[key]) is str and _HASH.fullmatch(current[key]) is not None)
        fingerprints.add(tuple(current[key] for key in sorted(_OIDC_HASHES)))
        before, after = current["before"], current["after"]
        _oidc_observation(before)
        _oidc_observation(after)
        _require(before == previous)
        for key in _OIDC_COUNTERS - {"client_auth_rejections"}:
            _require(after[key] == before[key] + 1)
        _require(after["client_auth_rejections"] == 1)
        previous = after
    _require(len(fingerprints) == 1)
    final = value["final_provider_observations"]
    _oidc_observation(final)
    _require(final["client_auth_rejections"] == 1)
    # The final unlinked attempt authenticates to the generated IdP, whose
    # signed token the installed recorder must refuse for lack of a link.
    for key in _OIDC_COUNTERS - {"client_auth_rejections"}:
        _require(final[key] == previous[key] + 1)


def _require(value):
    if not value:
        raise ValueError("Installed Windows SCM recording evidence refused")


def _integer(value, minimum, maximum=2**63 - 1):
    _require(type(value) is int and minimum <= value <= maximum)
    return value


def _number(value, minimum, maximum):
    _require(type(value) in (int, float) and math.isfinite(value) and minimum <= value <= maximum)
    return value


def _object(pairs):
    value = {}
    for key, item in pairs:
        _require(key not in value)
        value[key] = item
    return value


def _constant(_value):
    raise ValueError("Nonfinite service evidence refused")


def _witness(raw):
    _require(type(raw) is bytes and 0 < len(raw) <= 1024**2)
    _require(
        b"\0" not in raw and b"<!DOCTYPE" not in raw.upper() and b"<!ENTITY" not in raw.upper()
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
            for k, v in {
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
    _require(all(n.tag in {"testcase", "properties", "system-out", "system-err"} for n in suite))
    _require(all(n.tag in {"properties", "system-out", "system-err"} for n in cases[0]))
    containers = [*suite.findall("properties"), *cases[0].findall("properties")]
    _require(list(root.iter("properties")) == containers)
    _require(all(n.tag == "property" for c in containers for n in c))
    properties = [n for c in containers for n in c]
    _require(len(properties) == 1 and properties[0].get("name") == _PROPERTY)
    _require(set(properties[0].attrib) == {"name", "value"} and len(properties[0]) == 0)
    return json.loads(
        properties[0].get("value", ""), object_pairs_hook=_object, parse_constant=_constant
    )


def verify(report: Path, *, expected_head: str, installer_sha256: str) -> dict:
    _require(
        type(expected_head) is str and re.fullmatch(r"[a-f0-9]{40}", expected_head) is not None
    )
    _require(type(installer_sha256) is str and _HASH.fullmatch(installer_sha256) is not None)
    _require(0 < report.stat().st_size <= 1024**2)
    value = _witness(report.read_bytes())
    _require(type(value) is dict and set(value) == _TRUE | _FALSE | _FIELDS)
    _require(all(value[k] is True for k in _TRUE) and all(value[k] is False for k in _FALSE))
    _require(type(value["schema_version"]) is int and value["schema_version"] == 1)
    _require(value["head_sha"] == expected_head and value["installer_sha256"] == installer_sha256)
    _require(value["platform"] == "Windows" and value["browser_channel"] == "msedge")
    _require(value["playwright_version"] == "1.58.0")
    _require(
        type(value["browser_version"]) is str
        and re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,3}", value["browser_version"])
    )
    _number(value["duration_seconds"], 1, 360)
    ownership = value["process_ownership"]
    _require(type(ownership) is dict and set(ownership) == _OWNERSHIP_TRUE | _OWNERSHIP_FIELDS)
    _require(all(ownership[key] is True for key in _OWNERSHIP_TRUE))
    _require(
        type(ownership["admitted_service_images"]) is list
        and ownership["admitted_service_images"] == sorted(_OWNERSHIP_IMAGES)
    )
    admitted = _integer(ownership["admitted_service_count"], len(_OWNERSHIP_IMAGES), 4096)
    rejected = _integer(ownership["rejected_foreign_count"], 0, 4096)
    _integer(ownership["unreadable_identity_count"], 0, 0)
    _integer(ownership["unexpected_service_image_count"], 0, 0)
    console = ownership["console_hosts"]
    _require(type(console) is dict and set(console) == _CONSOLE_TRUE | _CONSOLE_FIELDS)
    _require(all(console[key] is True for key in _CONSOLE_TRUE))
    _require(console["individual_termination_used"] is False)
    observed = _integer(console["observed_count"], 0, 4096)
    _require(_integer(console["exited_count"], 0, 4096) == observed)
    _require(admitted + rejected + observed <= 4096)
    scm = value["scm"]
    _require(
        type(scm) is dict
        and set(scm)
        == {
            "service_type",
            "start_type",
            "account",
            "reset_seconds",
            "failure_actions",
            "installed_executable_verified",
        }
    )
    _require(type(scm["service_type"]) is int and scm["service_type"] == 0x10)
    _require(type(scm["start_type"]) is int and scm["start_type"] == 2)
    _require(scm["account"] == r"NT SERVICE\K5VisionRecorder")
    _require(type(scm["reset_seconds"]) is int and scm["reset_seconds"] == 3600)
    _require(scm["installed_executable_verified"] is True)
    actions = scm["failure_actions"]
    _require(type(actions) is list and len(actions) == 3)
    for action, delay in zip(actions, (5000, 15000, 60000), strict=True):
        _require(type(action) is dict and set(action) == {"type", "delay_ms"})
        _require(type(action["type"]) is int and action["type"] == 1)
        _require(type(action["delay_ms"]) is int and action["delay_ms"] == delay)
    phases = value["observations"]
    _require(type(phases) is dict and set(phases) == set(_PHASES))
    for phase in _PHASES:
        current = phases[phase]
        _require(type(current) is dict and set(current) == _OBSERVATION)
        for name in ("service", "backend"):
            _integer(current[name + "_pid"], 1, 2**32 - 1)
            _number(current[name + "_created"], 1, 2**53)
            _require(type(current[name + "_session"]) is int and current[name + "_session"] == 0)
            _require(current[name + "_account_verified"] is True)
        _require(current["service_pid"] != current["backend_pid"])
        before = _integer(current["recording_bytes_before"], 0)
        _require(_integer(current["recording_bytes_after"], 1) > before)
    for name in ("service", "backend"):
        identities = {
            phase: (phases[phase][name + "_pid"], phases[phase][name + "_created"])
            for phase in _PHASES
        }
        _require(identities["initial"] == identities["client_closed"])
        _require(identities["backend_recovered"] != identities["client_closed"])
        _require(identities["wrapper_recovered"] != identities["backend_recovered"])
    _oidc_restart(value["oidc_restart"], phases)
    ordinary = value["standard_windows_user"]
    _require(
        type(ordinary) is dict
        and set(ordinary) == _STANDARD_TRUE | _STANDARD_FALSE | _STANDARD_FIELDS
    )
    _require(all(ordinary[key] is True for key in _STANDARD_TRUE))
    _require(all(ordinary[key] is False for key in _STANDARD_FALSE))
    _require(
        type(ordinary["product_status_success_count"]) is int
        and ordinary["product_status_success_count"] == 4
    )
    _number(ordinary["slow_status_seconds"], 4, 8.499999)
    _require(
        type(ordinary["probe_executable_sha256"]) is str
        and _HASH.fullmatch(ordinary["probe_executable_sha256"]) is not None
    )
    _require(
        _integer(ordinary["recording_bytes_after"], 1)
        > _integer(ordinary["recording_bytes_before"], 0)
    )
    startup = value["startup_shutdown"]
    _require(type(startup) is dict and set(startup) == _STARTUP_TRUE | _STARTUP_FIELDS)
    _require(all(startup[key] is True for key in _STARTUP_TRUE))
    for prefix in ("", "previous_"):
        _integer(startup[prefix + "host_pid"], 1, 2**32 - 1)
        _integer(startup[prefix + "host_created_100ns"], 1, 2**63 - 1)
    _require(
        (startup["host_pid"], startup["host_created_100ns"])
        != (startup["previous_host_pid"], startup["previous_host_created_100ns"])
    )
    _require(startup["observed_stage"] in {"token", "code_admission", "payload_verify"})
    _number(startup["stop_seconds"], 0.001, 69.999999)
    before, after = value["retained_file_hashes_before"], value["retained_file_hashes_after"]
    _require(type(before) is list and 1 <= len(before) <= 64 and before == after)
    _require(all(type(item) is str and _HASH.fullmatch(item) is not None for item in before))
    for name in ("site", "source", "users", "storage"):
        before, after = value[name + "_hash_before"], value[name + "_hash_after"]
        _require(type(before) is str and _HASH.fullmatch(before) is not None and before == after)
    _integer(value["retained_user_count"], 2, 100)
    _integer(value["retained_camera_count"], 1, 4)
    _integer(value["playback_decoded_frames"], 4, 100000)
    _integer(value["export_decoded_frames"], 4, 100000)
    _integer(value["export_bytes"], 1, 256 * 1024**2)
    _require(
        type(value["export_sha256"]) is str and _HASH.fullmatch(value["export_sha256"]) is not None
    )
    for name in ("clock_health_before_restart", "clock_health_after_restart"):
        clock = value[name]
        _require(type(clock) is dict and set(clock) == {"status", "sync_verified", "server_utc"})
        _require(clock["status"] == "observing" and clock["sync_verified"] is False)
        timestamp = clock["server_utc"]
        _require(type(timestamp) is str and len(timestamp) <= 32 and timestamp.endswith("Z"))
        _require(
            datetime.fromisoformat(timestamp.replace("Z", "+00:00")).utcoffset().total_seconds()
            == 0
        )
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
                "lane": "service_windows",
                "installer_sha256": args.installer_sha256,
                "installer_qualification_status": "unqualified",
                "receipt": receipt,
            },
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
