"""Diagnostic-only replay of exact 699 bytes, with a separate cleanup-frame helper.

The original clock journey and restoration guard execute unchanged. Its
properties have a diagnostic-only namespace and cannot qualify the product.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
from pathlib import Path

import pytest

PRODUCT_HEAD = "6993360f0682e41589005b0b97aa391398baa163"
SETUP_SHA256 = "c64a9c50c328acd520491d97572fd2ea247fb9faacaf147164466a40769acbcb"
HELPER_SHA256 = "fed6eb0105ef9989586d51a67dbfb75b2f45903a2df8012346f65ac15f1beacb"
SOURCE_PINS = {
    "tests/integration/test_windows_vm_clock_qualification.py": (
        "3049b70f0692c5872d2be9c83238c6f42f4da7d42277668c292147908911195e"
    ),
    "tests/integration/windows_vm_clock.py": (
        "a43fb41cc3ad80ea84040c22691d20703906f884d6b586b6ccd3c86110ea10a5"
    ),
    "tests/integration/windows_process_identity.py": (
        "780605038abd93dcd0f166512d2720db731c83fe87d569cfc22ee9decc5064ff"
    ),
    "tests/integration/windows_service_acceptance.py": (
        "2a1240ada1d63bb0454fa11e9f30e0591d3b1c9fd5f394857ecfc5174603764f"
    ),
    "tests/integration/test_operator_product_runtime.py": (
        "7e662e77bb3c072119d0ae545d2193c9f909bf0229478659c5698272aca20050"
    ),
    "scripts/verify_windows_vm_clock_receipt.py": (
        "38d658b372c7eba6b5f1eea9acad223ce5a4a542acd62d22bf6bfafcd5bcf241"
    ),
    "scripts/verify_desktop_receipt.py": (
        "68eb434fd5281d3b7bc7d9b33566a4858930903c7c71730dd2f3a16cf8ab8bb1"
    ),
}
_ORIGINAL_PROPERTIES = {
    "vm_clock_windows_phase",
    "vm_clock_windows_diagnostic",
    "vm_clock_windows_receipt",
}
pytestmark = pytest.mark.skipif(
    os.name != "nt" or os.environ.get("K5_CLOCK_CLEANUP_DIAGNOSTIC_ONLY") != "1",
    reason="Exact 699 diagnostic requires its separate disposable Windows VM",
)


def _run_original(original, record_testsuite_property):
    def diagnostic_property(name, value):
        assert name in _ORIGINAL_PROPERTIES, "Unknown original clock property"
        record_testsuite_property("diagnostic_only_clock_cleanup_original_" + name, value)

    # No catch, retry, additional wait, or replacement of the original result.
    return original.test_native_windows_vm_clock_qualification(diagnostic_property)


def test_diagnostic_only_exact_699_clock_cleanup(record_testsuite_property):
    root = Path(__file__).resolve().parents[2]
    harness_head = os.environ.get("K5_CLOCK_CLEANUP_DIAGNOSTIC_HARNESS_SHA", "")
    assert re.fullmatch(r"[a-f0-9]{40}", harness_head) and harness_head != PRODUCT_HEAD
    assert os.environ.get("K5_DESKTOP_HEAD_SHA") == PRODUCT_HEAD
    assert os.environ.get("K5_DESKTOP_SETUP_SHA256") == SETUP_SHA256
    assert os.environ.get("K5_VM_CLOCK_ACCEPTANCE") == "1"
    assert os.environ.get("GITHUB_ACTIONS") == "true"
    assert os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted"
    assert os.environ.get("RUNNER_OS") == "Windows"
    for relative, expected in SOURCE_PINS.items():
        canonical = (root / relative).read_bytes().replace(b"\r\n", b"\n")
        assert hashlib.sha256(canonical).hexdigest() == expected, "Original 699 source differs"
    helper = root / "tests/integration/test_windows_desktop_app.py"
    assert hashlib.sha256(helper.read_bytes()).hexdigest() == HELPER_SHA256
    setup = Path(os.environ["K5_DESKTOP_SETUP"])
    assert setup.stat().st_size == 286589280
    with setup.open("rb") as stream:
        assert hashlib.file_digest(stream, "sha256").hexdigest() == SETUP_SHA256
    record_testsuite_property(
        "diagnostic_only_clock_cleanup_binding",
        json.dumps(
            {
                "schema_version": 1,
                "diagnostic_only": True,
                "normal_qualification": False,
                "product_head": PRODUCT_HEAD,
                "harness_head": harness_head,
                "installer_sha256": SETUP_SHA256,
                "installer_bytes": 286589280,
                "source_run_id": 45425429979,
                "source_run_attempt": 1,
                "producer_job_id": 138555995448,
                "setup_artifact_id": 14937595093,
                "primary_artifact_id": 14936174289,
                "helper_sha256": HELPER_SHA256,
                "original_source_canonical_lf_sha256": SOURCE_PINS,
                "original_deadlines_and_criteria": True,
                "product_source_changed": False,
            },
            sort_keys=True,
        ),
    )
    source = root / "tests/integration/test_windows_vm_clock_qualification.py"
    spec = importlib.util.spec_from_file_location("exact_699_clock_cleanup_original", source)
    assert spec is not None and spec.loader is not None
    original = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(original)
    assert all(os.environ.get(name) for name in original._INPUTS)
    _run_original(original, record_testsuite_property)
