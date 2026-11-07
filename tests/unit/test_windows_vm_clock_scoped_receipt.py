"""Reject mixed or resealed companion evidence at the distinct clock boundary."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "scoped_legacy_fixtures",
    Path(__file__).with_name("test_windows_vm_clock_receipt.py"),
)
LEGACY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(LEGACY)
VERIFIER = LEGACY._VERIFIER


def _bytes(value):
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def _xml(path, receipt, *, primary=False):
    tree = ET.Element("testsuites")
    suite = ET.SubElement(tree, "testsuite")
    for name in sorted(VERIFIER._PRIMARY_TESTS if primary else {VERIFIER._SCOPED_TEST}):
        case = ET.SubElement(suite, "testcase", name=name)
        if name == (VERIFIER._TEST if primary else VERIFIER._SCOPED_TEST):
            props = ET.SubElement(case, "properties")
            ET.SubElement(
                props,
                "property",
                name="desktop_windows_receipt" if primary else VERIFIER._SCOPED_PROPERTY,
                value=json.dumps(receipt),
            )
            if not primary:
                ET.SubElement(
                    props,
                    "property",
                    name="vm_clock_windows_phase",
                    value=json.dumps(receipt.get("vm_clock")),
                )
    ET.ElementTree(tree).write(path, encoding="utf-8", xml_declaration=True)


def valid_scoped_evidence(root):
    """Synthetic validator inputs only; no native execution claim is made by this test."""
    root.mkdir(parents=True, exist_ok=True)
    setup_dir = root / "setup"
    setup_dir.mkdir()
    setup = b"native-setup-validator-fixture"
    setup_hash = hashlib.sha256(setup).hexdigest()
    (setup_dir / "K5VisionSetup.exe").write_bytes(setup)
    manifest = _bytes({"schema_version": 1, "head_sha": LEGACY._HEAD, "files": []})
    (setup_dir / "payload-manifest.json").write_bytes(manifest)
    package = setup_dir / "desktop-package.json"
    package.write_bytes(
        _bytes(
            {
                "schema_version": 1,
                "head_sha": LEGACY._HEAD,
                "setup_bytes": len(setup),
                "setup_sha256": setup_hash,
                "payload_manifest_sha256": hashlib.sha256(manifest).hexdigest(),
            }
        )
    )
    primary = LEGACY._receipt()
    del primary["vm_clock"]
    primary["host_clock_step_acceptance"] = False
    primary_report, primary_receipt = root / "primary.xml", root / "primary.json"
    _xml(primary_report, primary, primary=True)
    wrapper = {
        "schema_version": "1",
        "lane": "desktop",
        "head_sha": LEGACY._HEAD,
        "receipt": primary,
    }
    primary_receipt.write_bytes(_bytes(wrapper))
    (setup_dir / "desktop-windows-receipt.json").write_bytes(primary_receipt.read_bytes())
    kwargs = dict(
        expected_head=LEGACY._HEAD,
        installer_sha256=setup_hash,
        primary_report=primary_report,
        primary_receipt=primary_receipt,
        package=package,
        expected_run_id=1001,
        expected_run_attempt=1,
        expected_primary_artifact_id=2001,
        expected_setup_artifact_id=2002,
    )
    binding = VERIFIER.admit_primary(**kwargs)
    clock = copy.deepcopy(LEGACY._receipt()["vm_clock"])
    clock["installer_sha256"] = setup_hash
    receipt = {
        **dict.fromkeys(VERIFIER._SCOPED_TRUE, True),
        **dict.fromkeys(VERIFIER._SCOPED_FALSE, False),
        "schema_version": 2,
        "scope": "vm-clock-dedicated",
        "platform": "Windows",
        "head_sha": LEGACY._HEAD,
        "harness_head_sha": LEGACY._HEAD,
        "installer_sha256": setup_hash,
        "payload_manifest_sha256": binding["manifest_sha256"],
        "browser_channel": "msedge",
        "playwright_version": "1.58.0",
        "browser_version": "142.0.1.2",
        "duration_seconds": 180.0,
        "primary_binding": binding,
        "vm_clock": clock,
        "camera_count": 4,
        "wall_decoded_frames": [4, 5, 6, 7],
        "camera_recording_bytes_before": [0, 1, 2, 3],
        "camera_recording_bytes_after": [10, 11, 12, 13],
        "recording_enabled_before_clock": [True, True, True, False],
        "storage_policy": {
            "revision": 0,
            "quota_bytes": 20 * 1024**3,
            "minimum_free_bytes": 1024**3,
            "retention_seconds": 86400,
        },
        "analytics_model_revision": "6697dead54ed1cdd664b0313189c2cb52ee6335e",
        "analytics_processed_frames": 1,
        "analytics_positive_person_count": 1,
        "analytics_event_playback_decoded_frames": 4,
    }
    report = root / "clock.xml"
    _xml(report, receipt)
    return SimpleNamespace(
        root=root,
        report=report,
        receipt=receipt,
        kwargs=kwargs,
        primary=primary,
        wrapper=wrapper,
    )


def _verify(bundle):
    _xml(bundle.report, bundle.receipt)
    return VERIFIER.verify_scoped(bundle.report, **bundle.kwargs)


def test_scoped_accepts_independent_clock_and_actual_revalidated_companion(tmp_path):
    bundle = valid_scoped_evidence(tmp_path)
    assert _verify(bundle) == bundle.receipt
    assert not (set(VERIFIER._TRUE) - VERIFIER._SCOPED_FIELDS) & set(bundle.receipt)
    with pytest.raises(ValueError):
        VERIFIER.verify(
            bundle.report,
            expected_head=LEGACY._HEAD,
            installer_sha256=bundle.kwargs["installer_sha256"],
        )


@pytest.mark.parametrize(
    "field",
    [
        "report_sha256",
        "receipt_sha256",
        "package_sha256",
        "manifest_sha256",
        "setup_sha256",
        "head_sha",
        "run_id",
        "run_attempt",
        "primary_artifact_id",
        "setup_artifact_id",
    ],
)
def test_scoped_rejects_any_foreign_or_resealed_binding_claim(tmp_path, field):
    bundle = valid_scoped_evidence(tmp_path)
    old = bundle.receipt["primary_binding"][field]
    bundle.receipt["primary_binding"][field] = old + 1 if type(old) is int else "c" * len(old)
    with pytest.raises(ValueError):
        _verify(bundle)


@pytest.mark.parametrize(
    "file",
    [
        "primary_report",
        "primary_receipt",
        "package",
        "Setup",
        "manifest",
        "packaged_receipt",
    ],
)
def test_scoped_requires_every_actual_companion_file(tmp_path, file):
    bundle = valid_scoped_evidence(tmp_path)
    paths = {
        **bundle.kwargs,
        "Setup": bundle.kwargs["package"].with_name("K5VisionSetup.exe"),
        "manifest": bundle.kwargs["package"].with_name("payload-manifest.json"),
        "packaged_receipt": bundle.kwargs["package"].with_name("desktop-windows-receipt.json"),
    }
    paths[file].unlink()
    with pytest.raises(ValueError):
        _verify(bundle)


@pytest.mark.parametrize("target", ["Setup", "manifest", "packaged_receipt"])
def test_scoped_checks_actual_bytes_not_only_claimed_package_metadata(tmp_path, target):
    bundle = valid_scoped_evidence(tmp_path)
    names = {
        "Setup": "K5VisionSetup.exe",
        "manifest": "payload-manifest.json",
        "packaged_receipt": "desktop-windows-receipt.json",
    }
    path = bundle.kwargs["package"].with_name(names[target])
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError):
        _verify(bundle)


def test_consistently_resealed_failed_normal_journey_cannot_replace_companion(tmp_path):
    bundle = valid_scoped_evidence(tmp_path)
    bundle.primary["actual_recording_export"] = False
    _xml(bundle.kwargs["primary_report"], bundle.primary, primary=True)
    raw = _bytes(bundle.wrapper)
    bundle.kwargs["primary_receipt"].write_bytes(raw)
    bundle.kwargs["package"].with_name("desktop-windows-receipt.json").write_bytes(raw)
    bundle.receipt["primary_binding"]["report_sha256"] = hashlib.sha256(
        bundle.kwargs["primary_report"].read_bytes()
    ).hexdigest()
    bundle.receipt["primary_binding"]["receipt_sha256"] = hashlib.sha256(raw).hexdigest()
    with pytest.raises(ValueError):
        _verify(bundle)


@pytest.mark.parametrize("primary", [True, False])
@pytest.mark.parametrize(
    "fault", ["duplicate", "foreign", "skipped", "failure", "error", "mixed_property"]
)
def test_scoped_rejects_mixed_duplicate_skipped_or_failed_reports(tmp_path, primary, fault):
    bundle = valid_scoped_evidence(tmp_path)
    path = bundle.kwargs["primary_report"] if primary else bundle.report
    tree = ET.parse(path)
    suite = tree.getroot().find("testsuite")
    case = suite.find("testcase")
    if fault == "duplicate":
        suite.append(copy.deepcopy(case))
    elif fault == "foreign":
        case.set("name", "foreign_case")
    elif fault == "mixed_property":
        ET.SubElement(
            case,
            "property",
            name="vm_clock_windows_receipt" if primary else "desktop_windows_receipt",
            value="{}",
        )
    else:
        ET.SubElement(case, fault)
    tree.write(path)
    with pytest.raises(ValueError):
        VERIFIER.verify_scoped(bundle.report, **bundle.kwargs)


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", True),
        ("duration_seconds", 360),
        ("duration_seconds", float("nan")),
        ("camera_count", True),
        ("harness_head_sha", "c" * 40),
        ("recording_enabled_before_clock", [1, 1, 1, 0]),
        ("camera_recording_bytes_after", [0, 11, 12, 13]),
        ("wall_decoded_frames", [4, 4, 4, 3]),
        (
            "storage_policy",
            {
                "revision": 0,
                "quota_bytes": 20 * 1024**3,
                "minimum_free_bytes": 1024**3,
                "retention_seconds": 0,
            },
        ),
        ("owned_cleanup", False),
        ("analytics_mock_inference", True),
        ("native_default_browser_handoff", True),
    ],
)
def test_scoped_rejects_unobserved_bootstrap_borrowed_flags_and_budget_changes(
    tmp_path, field, value
):
    bundle = valid_scoped_evidence(tmp_path)
    bundle.receipt[field] = value
    with pytest.raises(ValueError):
        _verify(bundle)


_CLOCK_REJECTION_TEST = (
    LEGACY.test_vm_clock_receipt_rejects_incomplete_simulated_or_unbound_evidence
)
CLOCK_MUTATIONS = next(
    mark.args[1] for mark in _CLOCK_REJECTION_TEST.pytestmark if mark.name == "parametrize"
)


@pytest.mark.parametrize("path,value", CLOCK_MUTATIONS)
def test_scoped_preserves_all_existing_clock_rejection_mutations(tmp_path, path, value):
    bundle = valid_scoped_evidence(tmp_path)
    target = bundle.receipt
    for key in path[:-1]:
        target = target[key]
    if value is LEGACY._MISSING:
        del target[path[-1]]
    else:
        target[path[-1]] = value
    with pytest.raises(ValueError):
        _verify(bundle)


@pytest.mark.parametrize("file", ["primary_receipt", "package", "manifest", "clock"])
def test_scoped_rejects_duplicate_json_keys_even_with_same_values(tmp_path, file):
    bundle = valid_scoped_evidence(tmp_path)
    if file == "clock":
        tree = ET.parse(bundle.report)
        prop = tree.getroot().find(".//property")
        raw = prop.get("value")
        prop.set("value", '{"scope":"vm-clock-dedicated",' + raw[1:])
        tree.write(bundle.report)
    else:
        path = (
            bundle.kwargs[file]
            if file != "manifest"
            else bundle.kwargs["package"].with_name("payload-manifest.json")
        )
        raw = path.read_text()
        key = "head_sha"
        path.write_text('{"' + key + '":"' + LEGACY._HEAD + '",' + raw[1:])
    with pytest.raises(ValueError):
        VERIFIER.verify_scoped(bundle.report, **bundle.kwargs)


def test_scoped_cli_dispatch_is_explicit_and_legacy_does_not_accept_companion_flags(
    tmp_path,
):
    bundle = valid_scoped_evidence(tmp_path)
    output = tmp_path / "accepted.json"
    command = [
        sys.executable,
        str(ROOT / "scripts/verify_windows_vm_clock_receipt.py"),
        str(bundle.report),
        str(output),
    ]
    for key, value in bundle.kwargs.items():
        command.extend(["--" + key.replace("_", "-"), str(value)])
    assert subprocess.run(command, capture_output=True, check=False).returncode != 0
    assert not output.exists()
    result = subprocess.run(
        command + ["--scope", "vm-clock-dedicated"], capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr
    envelope = json.loads(output.read_bytes())
    assert envelope["schema_version"] == "2" and envelope["lane"] == "desktop-vm-clock-scoped"
    assert envelope["receipt"] == bundle.receipt


@pytest.mark.parametrize(
    "fault", ["missing", "duplicate", "mismatched", "nonfinite", "duplicate_json"]
)
def test_scoped_requires_one_exact_retained_phase_equal_to_its_clock_receipt(tmp_path, fault):
    bundle = valid_scoped_evidence(tmp_path)
    tree = ET.parse(bundle.report)
    properties = tree.getroot().find(".//properties")
    phase = next(item for item in properties if item.get("name") == "vm_clock_windows_phase")
    if fault == "missing":
        properties.remove(phase)
    elif fault == "duplicate":
        properties.append(copy.deepcopy(phase))
    elif fault == "mismatched":
        phase.set("value", "{}")
    elif fault == "nonfinite":
        phase.set("value", '{"private":NaN}')
    else:
        raw = phase.get("value")
        phase.set("value", '{"schema_version":"1",' + raw[1:])
    tree.write(bundle.report)
    with pytest.raises(ValueError):
        VERIFIER.verify_scoped(bundle.report, **bundle.kwargs)
