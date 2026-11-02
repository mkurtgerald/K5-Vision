from __future__ import annotations

import json
import runpy
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

_VERIFIER = runpy.run_path(
    str(Path(__file__).parents[2] / "scripts/verify_windows_time_administration_receipt.py")
)
_HEAD, _SUPERVISOR, _MANIFEST = "a" * 40, "b" * 64, "c" * 64


def _receipt():
    return {
        **{key: True for key in _VERIFIER["_TRUE"]},
        **{key: False for key in _VERIFIER["_FALSE"]},
        **{key: False for key in _VERIFIER["_BOOL"]},
        "schema_version": 1,
        "head_sha": _HEAD,
        "supervisor_sha256": _SUPERVISOR,
        "payload_manifest_sha256": _MANIFEST,
        "duration_seconds": 45.0,
        "native_resync_succeeded": True,
    }


def _report(tmp_path, receipt, *, failed=False, skipped=False):
    suites = ET.Element("testsuites")
    suite = ET.SubElement(
        suites,
        "testsuite",
        tests="1",
        failures="1" if failed else "0",
        errors="0",
        skipped="1" if skipped else "0",
    )
    properties = ET.SubElement(suite, "properties")
    ET.SubElement(
        properties,
        "property",
        name="k5_native_time_administration_receipt",
        value=json.dumps(receipt),
    )
    case = ET.SubElement(
        suite,
        "testcase",
        name="test_native_windows_time_configuration_readback_and_resync_with_restoration",
    )
    if failed:
        ET.SubElement(case, "failure")
    if skipped:
        ET.SubElement(case, "skipped")
    path = tmp_path / "time.xml"
    ET.ElementTree(suites).write(path, encoding="utf-8", xml_declaration=True)
    return path


def _verify(path):
    return _VERIFIER["verify"](
        path, expected_head=_HEAD, supervisor_sha256=_SUPERVISOR, payload_manifest_sha256=_MANIFEST
    )


def test_strict_time_source_proof_admits_exact_source_binary_and_restoration(tmp_path):
    receipt = _receipt()
    assert _verify(_report(tmp_path, receipt)) == receipt


@pytest.mark.parametrize("outcome", ["upstream_unavailable", "resync_timeout"])
def test_network_unavailability_is_retained_without_becoming_resync_success(tmp_path, outcome):
    receipt = _receipt()
    receipt["native_resync_succeeded"] = False
    receipt[outcome] = True
    assert _verify(_report(tmp_path, receipt))["native_resync_succeeded"] is False


@pytest.mark.parametrize(
    "key", sorted(_VERIFIER["_TRUE"] | _VERIFIER["_FALSE"] | _VERIFIER["_BOOL"])
)
def test_missing_required_time_control_evidence_fails_closed(tmp_path, key):
    receipt = _receipt()
    receipt.pop(key)
    with pytest.raises(ValueError):
        _verify(_report(tmp_path, receipt))


@pytest.mark.parametrize(
    "key", sorted(_VERIFIER["_TRUE"] | _VERIFIER["_FALSE"] | _VERIFIER["_BOOL"])
)
def test_integer_truth_values_cannot_replace_typed_time_proof_booleans(tmp_path, key):
    receipt = _receipt()
    receipt[key] = int(receipt[key])
    with pytest.raises(ValueError):
        _verify(_report(tmp_path, receipt))


@pytest.mark.parametrize("field", ["head_sha", "supervisor_sha256", "payload_manifest_sha256"])
def test_different_source_or_binary_or_payload_is_rejected(tmp_path, field):
    receipt = _receipt()
    receipt[field] = "0" * len(receipt[field])
    with pytest.raises(ValueError):
        _verify(_report(tmp_path, receipt))


@pytest.mark.parametrize(
    "mutation",
    [
        {"schema_version": True},
        {"duration_seconds": True},
        {"duration_seconds": 0},
        {"duration_seconds": 241},
        {"native_resync_succeeded": False},
        {"upstream_unavailable": True},
        {"reference_alignment_verified": True, "reference_measured": False},
        {"unreviewed_field": True},
    ],
)
def test_inconsistent_or_unbounded_evidence_is_rejected(tmp_path, mutation):
    receipt = _receipt()
    receipt.update(mutation)
    with pytest.raises(ValueError):
        _verify(_report(tmp_path, receipt))


@pytest.mark.parametrize("options", [{"failed": True}, {"skipped": True}])
def test_failed_or_skipped_native_tests_cannot_qualify_time_controls(tmp_path, options):
    with pytest.raises(ValueError):
        _verify(_report(tmp_path, _receipt(), **options))


@pytest.mark.parametrize("field,value", [("tests", "0"), ("failures", "1"), ("skipped", "1")])
def test_suite_metadata_cannot_hide_failed_or_skipped_native_work(tmp_path, field, value):
    path = _report(tmp_path, _receipt())
    root = ET.parse(path)
    root.find("testsuite").set(field, value)
    root.write(path, encoding="utf-8", xml_declaration=True)
    with pytest.raises(ValueError):
        _verify(path)
