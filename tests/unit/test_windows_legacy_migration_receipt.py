"""Synthetic receipt fixtures test admission; they are never native test evidence."""

from __future__ import annotations

import copy
import json
import runpy
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

_VERIFIER = runpy.run_path(
    str(Path(__file__).parents[2] / "scripts/verify_windows_legacy_migration_receipt.py")
)
_BASELINE, _HEAD = "a" * 40, "b" * 40
_BASELINE_SETUP, _SETUP = "c" * 64, "d" * 64


def _receipt():
    retained = {
        name + "_hash_" + phase: "e" * 64
        for name in _VERIFIER["_RETAINED_PAIRS"]
        for phase in ("before", "after")
    }
    retained.update(
        {
            "user_count": 2,
            "camera_count": 1,
            "storage_revision_before": 3,
            "storage_revision_after": 4,
            "closed_hashes_before": ["5" * 64],
            "closed_hashes_after": ["5" * 64],
            "export_hashes_before": ["3" * 64, "4" * 64],
            "export_hashes_after": ["3" * 64, "4" * 64],
        }
    )
    return {
        **dict.fromkeys(_VERIFIER["_TRUE"], True),
        **dict.fromkeys(_VERIFIER["_FALSE"], False),
        "schema_version": 1,
        "baseline_head": _BASELINE,
        "candidate_head": _HEAD,
        "baseline_installer_sha256": _BASELINE_SETUP,
        "candidate_installer_sha256": _SETUP,
        "platform": {"system": "Windows", "release": "11", "architecture": "AMD64"},
        "duration_seconds": 120.5,
        "actual_graphical_installations": 3,
        "retained": retained,
        "security": {
            "source_cipher_hash_before": "f" * 64,
            "source_cipher_hash_after": "f" * 64,
            "service_cipher_hash": "1" * 64,
            "mfa_generation_hash_before": "2" * 64,
            "mfa_generation_hash_after": "2" * 64,
            "migration_factor_replay_refused": True,
            "service_mfa_login_succeeded": True,
            "old_profile_mfa_login_after_reversal": True,
        },
        "media": {
            "playback_decoded_frames": 8,
            "export_decoded_frames": 10,
            "export_bytes": 4096,
            "export_sha256": "3" * 64,
        },
        "recording": {
            "before_bytes": 2000,
            "after_bytes": 3000,
            "service_pid_before": 100,
            "service_pid_after": 100,
            "backend_pid_before": 200,
            "backend_pid_after": 200,
            "service_created_100ns_before": 133000000000000000,
            "service_created_100ns_after": 133000000000000000,
            "backend_created_100ns_before": 133000000000000001,
            "backend_created_100ns_after": 133000000000000001,
            "session": 0,
        },
        "interruption": {
            "cancelled_stage_count": 1,
            "pending_before_retry": True,
            "identity_absent_after_cancel": True,
            "source_unchanged_after_cancel": True,
            "recovered_stage_count": 1,
            "retired_stage_bytes_preserved": True,
        },
        "native_copy": {
            "test_name": "test_native_migration_copy_windows_actual_security_and_io",
            "executed": True,
            "skipped": False,
            "passed": True,
        },
    }


def _tree(value, *, diagnostic=None, case_property=False):
    root = ET.Element("testsuites")
    suite = ET.SubElement(root, "testsuite", tests="1", failures="0", errors="0", skipped="0")
    case = ET.SubElement(suite, "testcase", name=_VERIFIER["_TEST"])
    properties = ET.SubElement(case if case_property else suite, "properties")
    ET.SubElement(properties, "property", name=_VERIFIER["_PROPERTY"], value=json.dumps(value))
    if diagnostic is not None:
        ET.SubElement(
            properties, "property", name=_VERIFIER["_DIAGNOSTIC"], value=json.dumps(diagnostic)
        )
    return root


def _report(tmp_path, value=None, *, tree=None):
    path = tmp_path / "legacy-migration.xml"
    ET.ElementTree(tree if tree is not None else _tree(value)).write(
        path, encoding="utf-8", xml_declaration=True
    )
    return path


def _verify(path, **overrides):
    return _VERIFIER["verify"](
        path,
        **{
            "baseline_head": _BASELINE,
            "head_sha": _HEAD,
            "installer_sha256": _SETUP,
            "baseline_installer_sha256": _BASELINE_SETUP,
            **overrides,
        },
    )


@pytest.mark.parametrize("case_property", [False, True])
def test_accepts_only_exact_installer_bound_generated_media_contract(tmp_path, case_property):
    value = _receipt()
    tree = _tree(value, case_property=case_property)
    assert _verify(_report(tmp_path, tree=tree)) == value


def test_exact_optional_diagnostic_is_not_qualification_evidence(tmp_path):
    value = _receipt()
    tree = _tree(value, diagnostic={"phase": "finished", "details": {"arbitrary": [1, True]}})
    assert _verify(_report(tmp_path, tree=tree)) == value
    tree.find(".//properties/property").set("value", json.dumps({"schema_version": 1}))
    with pytest.raises(ValueError):
        _verify(_report(tmp_path, tree=tree))


@pytest.mark.parametrize("field", sorted(_VERIFIER["_TRUE"] | _VERIFIER["_FALSE"]))
@pytest.mark.parametrize("change", ["opposite", "integer", "missing"])
def test_every_claim_has_required_literal_truth_and_finite_scope(tmp_path, field, change):
    value = _receipt()
    if change == "missing":
        del value[field]
    else:
        value[field] = not value[field] if change == "opposite" else int(value[field])
    with pytest.raises(ValueError):
        _verify(_report(tmp_path, value))


@pytest.mark.parametrize(
    "key,value",
    [
        ("schema_version", True),
        ("duration_seconds", True),
        ("duration_seconds", 0),
        ("duration_seconds", 1800.01),
        ("duration_seconds", float("inf")),
        ("duration_seconds", float("nan")),
        ("actual_graphical_installations", 2),
        ("actual_graphical_installations", 3.0),
        ("baseline_head", "0" * 40),
        ("candidate_head", "0" * 40),
        ("baseline_installer_sha256", "0" * 64),
        ("candidate_installer_sha256", "0" * 64),
        ("extra", True),
    ],
)
def test_rejects_unbound_heads_installers_counts_and_nonfinite_values(tmp_path, key, value):
    receipt = _receipt()
    receipt[key] = value
    with pytest.raises(ValueError):
        _verify(_report(tmp_path, receipt))


@pytest.mark.parametrize(
    "section,key,value",
    [
        ("platform", "system", "Linux"),
        ("platform", "architecture", "x86"),
        ("platform", "release", ""),
        ("platform", "release", "x" * 81),
        ("retained", "site_hash_after", "0" * 64),
        ("retained", "users_hash_after", "0" * 64),
        ("retained", "grants_hash_after", "0" * 64),
        ("retained", "cameras_hash_after", "0" * 64),
        ("retained", "storage_policy_hash_after", "0" * 64),
        ("retained", "user_count", 1),
        ("retained", "user_count", 11),
        ("retained", "camera_count", 0),
        ("retained", "camera_count", 5),
        ("retained", "storage_revision_before", -1),
        ("retained", "storage_revision_before", 2**63 - 1),
        ("retained", "storage_revision_after", 3),
        ("retained", "storage_revision_after", 5),
        ("retained", "closed_hashes_before", []),
        ("retained", "closed_hashes_after", ["0" * 64]),
        ("retained", "export_hashes_before", ["3" * 64]),
        ("retained", "export_hashes_after", ["4" * 64, "3" * 64]),
        ("retained", "closed_hashes_before", ["0" * 64] * 65),
        ("retained", "export_hashes_before", ["0" * 64] * 33),
        ("security", "source_cipher_hash_after", "0" * 64),
        ("security", "service_cipher_hash", "f" * 64),
        ("security", "mfa_generation_hash_after", "0" * 64),
        ("security", "migration_factor_replay_refused", False),
        ("security", "service_mfa_login_succeeded", 1),
        ("security", "old_profile_mfa_login_after_reversal", False),
        ("media", "playback_decoded_frames", 7),
        ("media", "export_decoded_frames", 0),
        ("media", "playback_decoded_frames", 10001),
        ("media", "export_bytes", 1023),
        ("media", "export_bytes", 64 * 1024**2 + 1),
        ("media", "export_sha256", "bad"),
        ("recording", "before_bytes", 0),
        ("recording", "after_bytes", 2000),
        ("recording", "after_bytes", 256 * 1024**2 + 1),
        ("recording", "service_pid_after", 101),
        ("recording", "backend_pid_after", 201),
        ("recording", "service_pid_before", 0),
        ("recording", "backend_pid_before", 2**32),
        ("recording", "service_created_100ns_after", 133000000000000001),
        ("recording", "backend_created_100ns_after", 133000000000000002),
        ("recording", "service_created_100ns_before", None),
        ("recording", "service_created_100ns_after", True),
        ("recording", "backend_created_100ns_before", False),
        ("recording", "backend_created_100ns_after", 133000000000000001.0),
        ("recording", "service_created_100ns_before", 0),
        ("recording", "backend_created_100ns_before", 2**63),
        ("recording", "session", False),
        ("recording", "session", 1),
        ("interruption", "cancelled_stage_count", 0),
        ("interruption", "recovered_stage_count", 17),
        ("interruption", "pending_before_retry", False),
        ("interruption", "identity_absent_after_cancel", 1),
        ("interruption", "source_unchanged_after_cancel", False),
        ("interruption", "retired_stage_bytes_preserved", False),
        ("native_copy", "test_name", "unit-only"),
        ("native_copy", "executed", False),
        ("native_copy", "skipped", True),
        ("native_copy", "passed", 1),
    ],
)
def test_rejects_changed_authorities_no_media_growth_decode_or_recovery(
    tmp_path, section, key, value
):
    receipt = _receipt()
    receipt[section][key] = value
    with pytest.raises(ValueError):
        _verify(_report(tmp_path, receipt))


@pytest.mark.parametrize(
    "section",
    ["platform", "retained", "security", "media", "recording", "interruption", "native_copy"],
)
@pytest.mark.parametrize("mutation", ["extra", "missing", "type"])
def test_nested_contracts_reject_extra_missing_or_wrong_container(tmp_path, section, mutation):
    value = _receipt()
    if mutation == "extra":
        value[section]["unsupported_passed"] = True
    elif mutation == "missing":
        del value[section][next(iter(value[section]))]
    else:
        value[section] = []
    with pytest.raises(ValueError):
        _verify(_report(tmp_path, value))


@pytest.mark.parametrize(
    "mutation",
    [
        "skip",
        "failure",
        "error",
        "counter",
        "root_counter",
        "extra_case",
        "wrong_test",
        "extra_suite",
        "duplicate_receipt",
        "duplicate_diagnostic",
        "unknown_property",
        "missing_receipt",
        "nested_failure",
    ],
)
def test_junit_requires_one_actual_pass_and_one_bound_receipt(tmp_path, mutation):
    tree = _tree(_receipt())
    suite, case, properties = tree[0], tree.find(".//testcase"), tree.find(".//properties")
    if mutation in {"skip", "failure", "error"}:
        ET.SubElement(case, "skipped" if mutation == "skip" else mutation)
    elif mutation == "counter":
        suite.set("failures", "1")
    elif mutation == "root_counter":
        tree.set("failures", "1")
    elif mutation == "extra_case":
        ET.SubElement(suite, "testcase", name="another")
    elif mutation == "wrong_test":
        case.set("name", "test_legacy_unit_only")
    elif mutation == "extra_suite":
        ET.SubElement(tree, "testsuite")
    elif mutation == "duplicate_receipt":
        properties.append(copy.deepcopy(properties[0]))
    elif mutation == "duplicate_diagnostic":
        for _ in range(2):
            ET.SubElement(properties, "property", name=_VERIFIER["_DIAGNOSTIC"], value="{}")
    elif mutation == "unknown_property":
        ET.SubElement(properties, "property", name="different_receipt", value="{}")
    elif mutation == "missing_receipt":
        properties[0].set("name", _VERIFIER["_DIAGNOSTIC"])
    else:
        ET.SubElement(ET.SubElement(case, "system-out"), "failure")
    with pytest.raises(ValueError):
        _verify(_report(tmp_path, tree=tree))


@pytest.mark.parametrize(
    "payload",
    [
        '{"schema_version":1,"schema_version":1}',
        '{"nested":{"same":1,"same":2}}',
        '{"x":NaN}',
        '{"x":Infinity}',
        '{"x":1e999}',
        "[]",
        "null",
    ],
)
@pytest.mark.parametrize("property_name", [_VERIFIER["_PROPERTY"], _VERIFIER["_DIAGNOSTIC"]])
def test_all_json_rejects_duplicate_members_nonfinite_and_non_objects(
    tmp_path, payload, property_name
):
    tree = _tree(_receipt())
    properties = tree.find(".//properties")
    if property_name == _VERIFIER["_PROPERTY"]:
        properties[0].set("value", payload)
    else:
        ET.SubElement(properties, "property", name=property_name, value=payload)
    with pytest.raises(ValueError):
        _verify(_report(tmp_path, tree=tree))


def test_diagnostic_payload_is_bounded_and_cannot_hide_qualification(tmp_path):
    tree = _tree(_receipt(), diagnostic={"data": "x" * 65536})
    with pytest.raises(ValueError):
        _verify(_report(tmp_path, tree=tree))


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"<testsuites>",
        b"<!DOCTYPE testsuites><testsuites/>",
        b"<!ENTITY x 'a'><testsuites/>",
        b"\0",
        b"x" * (1024**2 + 1),
    ],
)
def test_rejects_malformed_or_unbounded_report(tmp_path, raw):
    path = tmp_path / "malformed.xml"
    path.write_bytes(raw)
    with pytest.raises(ValueError):
        _verify(path)


def test_actual_old_nonmedia_desktop_smoke_cannot_qualify_legacy_migration(tmp_path):
    old = {
        "schema_version": 1,
        "baseline_head": _BASELINE,
        "candidate_head": _HEAD,
        "actual_graphical_installations": 3,
        "live_upgrade_refused": True,
        "account_and_profile_retained": True,
        "stopped_manual_reversal": True,
        "candidate_uninstall_preserved_profile": True,
        "manual_reversal_requires_candidate_uninstall": True,
        "automatic_failed_activation_rollback": False,
        "media_compatibility_qualified": False,
        "synthetic_input_only": True,
        "owned_cleanup": True,
    }
    # Even relabeling the historical receipt/test cannot supply missing media,
    # service-token, MFA, retained-authority or interrupted-migration evidence.
    with pytest.raises(ValueError):
        _verify(_report(tmp_path, old))


def test_optional_baseline_installer_pin_still_requires_a_valid_receipt_hash(tmp_path):
    assert _verify(_report(tmp_path, _receipt()), baseline_installer_sha256=None)
    value = _receipt()
    value["baseline_installer_sha256"] = "unbound"
    with pytest.raises(ValueError):
        _verify(_report(tmp_path, value), baseline_installer_sha256=None)


def test_cli_uses_requested_flags_and_refuses_skipped_evidence(tmp_path, capsys):
    path = _report(tmp_path, _receipt())
    args = [
        "--junit",
        str(path),
        "--baseline-head",
        _BASELINE,
        "--head-sha",
        _HEAD,
        "--installer-sha256",
        _SETUP,
        "--baseline-installer-sha256",
        _BASELINE_SETUP,
    ]
    _VERIFIER["main"](args)
    assert json.loads(capsys.readouterr().out) == _receipt()
    tree = _tree(_receipt())
    ET.SubElement(tree.find(".//testcase"), "skipped")
    _report(tmp_path, tree=tree)
    with pytest.raises(SystemExit) as error:
        _VERIFIER["main"](args)
    assert error.value.code == 1
    assert capsys.readouterr().err == "Native Windows legacy migration evidence refused\n"


def test_harness_self_admission_has_same_strict_schema_and_process_birth_binding():
    value = _receipt()
    pins = {
        "baseline_head": _BASELINE,
        "head_sha": _HEAD,
        "installer_sha256": _SETUP,
        "baseline_installer_sha256": _BASELINE_SETUP,
    }
    assert _VERIFIER["validate_receipt"](value, **pins) == value
    value["recording"]["backend_created_100ns_after"] += 1
    with pytest.raises(ValueError):
        _VERIFIER["validate_receipt"](value, **pins)
