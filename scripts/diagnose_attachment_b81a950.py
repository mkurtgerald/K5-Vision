"""Attachment-only observations after unchanged installed test preconditions.

This diagnostic intentionally stops before entering the original clock phase.
It never qualifies a release or changes a product HTTP deadline or guest clock.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

SOURCE_HEAD = "b81a950975e50979f57b20fb13cdbfbe73fd1aac"
SAMPLER_SHA256 = "5d79feb301ecdc89b9436b67503a51c112f1250723e33946a5e679b53eb946bf"
IDENTITY_SHA256 = "0899f2be63d065680e4774468dac188adb017492ca751efb7412fd39b30f25a9"
SOURCE_HASHES = {
    ".github/actions/provision-operator-tools/action.yml": "e50c55c29830ed793c0500e52628d145580d23209f49c9369a2c218e722c0c00",
    ".github/workflows/operator-desktop-windows.yml": "c49a6024443d956b62db7cfecfb5acbd0ee4a4118726daa4a794d59390d048ec",
    "scripts/provision_operator_person_acceptance.py": "3dc414b2aa0ba92cae73ab02b81ffdca43386e5e0e1274c02dc0c52110d3c896",
    "scripts/verify_desktop_receipt.py": "1dacff23c919e09c64ff7723f8ec64d80e7660739d327dc9ebb770207f1495ca",
    "scripts/verify_windows_vm_clock_receipt.py": "b391e01b2276bd95e955263e50300507044239f75c1fd4978a5550e0c5a5be11",
    "tests/integration/test_operator_beta_browser.py": "67eea5d12df2cfe6e19603fe459405eac05c93e6db8d59c068a23f69a573f771",
    "tests/integration/test_operator_product_runtime.py": "d7c7eaa38fe36843a937e57d60b696a93bca66599b3883e51d84cd1f37345106",
    "tests/integration/test_windows_desktop_app.py": "e32632d81211987fccfb9b5412fcaa2759fe08ff0ad8db99b1dfba907efbf1ad",
    "tests/integration/test_windows_vm_clock_qualification.py": "cbd66d5cdf2b13a17c4f05e4e2c6fd10a7d50b77add65b8549a2d4d27721dcb8",
    "tests/integration/windows_vm_clock.py": "a43fb41cc3ad80ea84040c22691d20703906f884d6b586b6ccd3c86110ea10a5"
}


def _require(value):
    if not value:
        raise RuntimeError("Instrumented clock diagnostic admission refused")


def _load(path):
    spec = importlib.util.spec_from_file_location("k5_instrumented_clock_original", path)
    _require(spec is not None and spec.loader is not None)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _verify_sources(root):
    for relative, expected in SOURCE_HASHES.items():
        raw = (root / relative).read_bytes()
        normalized = raw.replace(b"\r\n", b"\n")
        _require(b"\r" not in normalized and hashlib.sha256(normalized).hexdigest() == expected)




class AttachmentDiagnosticComplete(RuntimeError):
    """A diagnostic stop before any original clock-phase action."""


class StackSamples:
    def __init__(self, helper, executable, index, identity):
        self.helper, self.executable, self.index = helper, executable, index
        self.identity_helper = identity
        self.process, self.selection = None, "not_prepared"
        self.safe_identity, self.attempts = {}, []
        self.assessment_complete = self.stopped_before_clock = False

    def prepare(self, cohort):
        self.process, self.selection = self.helper.remembered_backend(cohort)
        _require(self.process is not None)
        self.safe_identity = self.identity_helper.verify_owned_target(cohort, self.process)
        _require(self.safe_identity.get("ready_for_attachment") is True)
        for attempt in range(1, 13):
            began = time.monotonic()
            sample = self.helper.sample_once(self.executable, self.process, self.index)
            captured = sample.get("outcome") == "projected" and bool(sample.get("threads"))
            self.attempts.append({
                "attempt": attempt,
                "cap_seconds": 0.8,
                "elapsed_ms": min(90000, round((time.monotonic() - began) * 1000)),
                "sample_outcome": sample.get("outcome"),
                "public_frame_captured": captured,
                "failure_reason": sample.get("failure_reason"),
                "stderr_sha256": sample.get("stderr_sha256"),
                "frames_discarded": True,
            })
            if sample.get("outcome") == "sampler_cleanup_incomplete":
                break
        self.assessment_complete = len(self.attempts) == 12

    def finish(self):
        return {
            "selection": self.selection,
            "safe_target_identity": self.safe_identity,
            "maximum_attachment_attempts": 12,
            "attachment_attempts": self.attempts,
            "assessment_complete": self.assessment_complete,
            "actual_backend_capture_gate_passed": any(x["public_frame_captured"] for x in self.attempts),
            "original_requests_observed": 0,
            "additional_http_requests": 0,
            "clock_phase_entered": False,
            "baseline_frames_discarded": True,
            "cleanup_failed": any(x["sample_outcome"] == "sampler_cleanup_incomplete" for x in self.attempts),
        }


def install_observation(target, stacks):
    original_loader = target._module

    def loaded(name, path):
        module = original_loader(name, path)
        if name != "k5_clock_desktop":
            return module

        def phase(*args, **kwargs):
            __tracebackhide__ = True
            stacks.prepare(kwargs["cohort"])
            stacks.stopped_before_clock = True
            raise AttachmentDiagnosticComplete("Attachment-only diagnostic stopped before clock phase")

        module._vm_clock_phase = phase
        return module

    target._module = loaded


def attachment_result_accepted(exit_code, complete, stopped, samples, properties, xml_tree):
    diagnostic = properties.get("vm_clock_windows_diagnostic")
    if not isinstance(diagnostic, dict) or diagnostic.get("cleanup_failed") is not False:
        return False
    if xml_tree is None:
        return False
    cases = list(xml_tree.iter("testcase"))
    if len(cases) != 1 or cases[0].get("name") != "test_native_windows_vm_clock_qualification":
        return False
    if list(xml_tree.iter("error")) or list(xml_tree.iter("skipped")) or len(list(xml_tree.iter("failure"))) != 1:
        return False
    return (
        exit_code == 1 and complete is True and stopped is True
        and samples.get("cleanup_failed") is False
        and samples.get("actual_backend_capture_gate_passed") is True
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    _require(os.name == "nt" and os.environ.get("K5_VM_CLOCK_ACCEPTANCE") == "1")
    _require(os.environ.get("K5_DESKTOP_HEAD_SHA") == SOURCE_HEAD)
    _require(os.environ.get("GITHUB_RUN_ATTEMPT") == "1")
    _require(os.environ.get("K5_RESTORED_HISTORY_DIAGNOSTIC_PROBE") in {"1", "2"})
    root = Path.cwd()
    _verify_sources(root)
    sys.path.insert(0, str(root / "src"))
    sampler_path = Path(os.environ["K5_INSTRUMENTED_CLOCK_SAMPLER"])
    _require(hashlib.sha256(sampler_path.read_bytes()).hexdigest() == SAMPLER_SHA256)
    sampler = _load(sampler_path)
    executable = sampler.prepare_executable(Path(os.environ["K5_INSTRUMENTED_CLOCK_PYSPY_EXE"]))
    identity_path = Path(os.environ["K5_TARGET_IDENTITY_HELPER"])
    _require(hashlib.sha256(identity_path.read_bytes()).hexdigest() == IDENTITY_SHA256)
    identity = _load(identity_path)
    stacks = StackSamples(sampler, executable, sampler.public_index(root / "src/k5vision"), identity)
    import xml.etree.ElementTree as ET

    import pytest

    target_path = root / "tests/integration/test_windows_vm_clock_qualification.py"
    node = (
        "tests/integration/test_windows_vm_clock_qualification.py::"
        "test_native_windows_vm_clock_qualification"
    )

    class ObservationPlugin:
        def pytest_collection_modifyitems(self, items):
            _require(len(items) == 1 and items[0].nodeid == node)
            target = items[0].module
            _require(Path(target.__file__).resolve(strict=True) == target_path.resolve(strict=True))
            install_observation(target, stacks)

    xml_path = args.output.with_suffix(".xml")
    _require(not xml_path.exists() and not args.output.exists())
    exit_code = 3
    try:
        exit_code = int(
            pytest.main(
                [
                    node,
                    "-o",
                    "addopts=",
                    "--no-cov",
                    "-s",
                    "--tb=short",
                    "-o",
                    "faulthandler_timeout=400",
                    "--junitxml=" + str(xml_path),
                ],
                plugins=[ObservationPlugin()],
            )
        )
    finally:
        samples = stacks.finish()
    properties = {}
    xml_tree = None
    if xml_path.is_file():
        raw = xml_path.read_bytes()
        _require(0 < len(raw) <= 16 * 1024**2)
        _require(b"<!DOCTYPE" not in raw.upper() and b"<!ENTITY" not in raw.upper())
        xml_tree = ET.fromstring(raw)
        for item in xml_tree.iter("property"):
            name, value = item.get("name"), item.get("value")
            _require(
                name
                in {
                    "vm_clock_windows_diagnostic",
                    "vm_clock_windows_phase",
                    "vm_clock_windows_receipt",
                }
            )
            _require(name not in properties and type(value) is str and len(value) <= 1024**2)
            properties[name] = json.loads(value)
    outcome = "attachment_assessed" if attachment_result_accepted(exit_code, stacks.assessment_complete, stacks.stopped_before_clock, samples, properties, xml_tree) else "failed"
    diagnostic = properties.get("vm_clock_windows_diagnostic", {})
    error_kind = diagnostic.get("failure_exception_kind") if type(diagnostic) is dict else None
    report = {
        "schema_version": 1,
        "scope": "owned-backend-attachment-only-diagnostic",
        "candidate_source_head": SOURCE_HEAD,
        "diagnostic_owner": {
            "dispatcher_head_sha": os.environ["K5_RESTORED_HISTORY_DIAGNOSTIC_DISPATCH_HEAD"],
            "run_id": int(os.environ["GITHUB_RUN_ID"]),
            "run_attempt": int(os.environ["GITHUB_RUN_ATTEMPT"]),
            "matrix_probe": int(os.environ["K5_RESTORED_HISTORY_DIAGNOSTIC_PROBE"]),
        },
        "outcome": outcome,
        "original_pytest_exit_code": exit_code,
        "original_test_intentionally_stopped_before_clock": stacks.stopped_before_clock,
        "original_application_cleanup_verified": isinstance(properties.get("vm_clock_windows_diagnostic"), dict) and properties["vm_clock_windows_diagnostic"].get("cleanup_failed") is False,
        "clock_phase_entered": False,
        "original_test_run_through_pytest": True,
        "error_kind": error_kind,
        "properties": properties,
        "sampler_enabled": True,
        "sampler_helper_sha256": SAMPLER_SHA256,
        "pyspy_executable_sha256": sampler.EXE_SHA256,
        "stack_sampling": samples,
        "original_failed_clock_jobs": [131638419488],
        "original_unit_guard_batch_retained": True,
        "publication_artifact_substitution_allowed": False,
        "release_qualification": False,
    }
    args.output.write_text(
        json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8"
    )
    print(
        json.dumps({"scope": report["scope"], "outcome": outcome, "release_qualification": False})
    )
    return 0 if outcome == "attachment_assessed" and samples["actual_backend_capture_gate_passed"] and not samples["cleanup_failed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
