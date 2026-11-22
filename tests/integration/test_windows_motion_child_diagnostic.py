"""Exact 0bc Motion replay with read-only, non-admitting child-image attribution."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path

import pytest

PRODUCT_HEAD = "0bc10f5aacaa45903b33d9f2dfb23c9d8d197868"
SETUP_SHA256 = "e39800706dad04edaa52a3f87578ec046553f82c2cf438976eb2a8730342d901"
OBSERVER_SHA256 = "5bf5140835d7b466b5e719ceba248bfe8a20e82c9237ff5240f7e108cf1541a1"
SOURCE_PINS = {
    "test_windows_motion_recording.py": (
        "746eea30ce15d88d01edab4c0bb1f836485ef1eab5a97c55f255aed816777276"
    ),
    "test_windows_desktop_app.py": (
        "066a9cabf28b578cae675dc31a8dfeb04c7957a02e20a304d2cfb5b5aedcaaac"
    ),
    "windows_process_identity.py": (
        "49c8f6e726de75b3c0c17ff2bd8033eb262eb6789a415db72252bc2e77644f0d"
    ),
    "test_operator_product_runtime.py": (
        "7e662e77bb3c072119d0ae545d2193c9f909bf0229478659c5698272aca20050"
    ),
}
PREFIX = "diagnostic_only_motion_child_"
pytestmark = pytest.mark.skipif(
    os.name != "nt" or os.environ.get("K5_NATIVE_MOTION_CHILD_DIAGNOSTIC_ONLY") != "1",
    reason="Exact Motion child attribution requires its separate disposable Windows VM",
)


def _record_original_property(record, name, value):
    assert name in {
        "diagnostic_only_motion_scope",
        "diagnostic_only_motion_windows_diagnostic",
        "diagnostic_only_motion_windows_receipt",
    }
    record(PREFIX + "original_" + name.removeprefix("diagnostic_only_"), value)


def _finite_error(error):
    if error is None:
        return None
    kind = type(error).__name__
    return (
        kind
        if kind
        in {
            "Failed",
            "AssertionError",
            "RuntimeError",
            "TimeoutError",
            "OSError",
            "ValueError",
            "TypeError",
            "Error",
        }
        else "other"
    )


def test_diagnostic_only_exact_0bc_motion_child(record_testsuite_property, monkeypatch):
    import test_windows_motion_recording as original
    from windows_service_image_observer import install

    harness_head = os.environ.get("K5_DIAGNOSTIC_HARNESS_HEAD_SHA", "")
    assert re.fullmatch(r"[a-f0-9]{40}", harness_head) and harness_head != PRODUCT_HEAD
    assert os.environ.get("K5_DESKTOP_HEAD_SHA") == PRODUCT_HEAD
    assert os.environ.get("K5_DESKTOP_SETUP_SHA256") == SETUP_SHA256
    assert os.environ.get("K5_MOTION_DIAGNOSTIC_ONLY") == "1"
    assert os.environ.get("K5_MOTION_DIAGNOSTIC_HARNESS_SHA") == harness_head
    assert os.environ.get("K5_NATIVE_MOTION_ACCEPTANCE") == "1"
    assert os.environ.get("K5_VM_CLOCK_ACCEPTANCE") == "1"
    assert all(os.environ.get(name) for name in original._INPUTS)
    directory = Path(__file__).parent
    for name, sha in SOURCE_PINS.items():
        assert (
            hashlib.sha256((directory / name).read_bytes().replace(b"\r\n", b"\n")).hexdigest()
            == sha
        )
    assert (
        hashlib.sha256((directory / "windows_service_image_observer.py").read_bytes()).hexdigest()
        == OBSERVER_SHA256
    )
    wrapper_sha = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    assert wrapper_sha == os.environ["K5_MOTION_CHILD_WRAPPER_SHA256"]
    setup = Path(os.environ["K5_DESKTOP_SETUP"])
    assert setup.stat().st_size == 286580017
    with setup.open("rb") as stream:
        assert hashlib.file_digest(stream, "sha256").hexdigest() == SETUP_SHA256
    record_testsuite_property(
        PREFIX + "binding",
        json.dumps(
            {
                "schema_version": 1,
                "diagnostic_only": True,
                "product_qualification": False,
                "installer_head": PRODUCT_HEAD,
                "harness_head": harness_head,
                "installer_sha256": SETUP_SHA256,
                "installer_bytes": 286580017,
                "candidate_artifact_id": 14742725690,
                "candidate_run_id": 44992781948,
                "candidate_run_attempt": 1,
                "candidate_producer_job_id": 137117284062,
                "observer_sha256": OBSERVER_SHA256,
                "wrapper_sha256": wrapper_sha,
                "original_source_canonical_lf_sha256": SOURCE_PINS,
                "original_deadlines_and_criteria": True,
                "normal_admission_changed": False,
                "product_source_changed": False,
            },
            sort_keys=True,
        ),
    )
    observers, stop_calls = [], []
    original_helpers = original._helpers

    def helpers(name):
        module = original_helpers(name)
        if name != "test_windows_desktop_app":
            return module
        cohort_class, original_stop = module._Cohort, module._stop_app

        def observed_cohort(api):
            cohort = cohort_class(api)
            observers.append(install(cohort))
            return cohort

        def observed_stop(*args, **kwargs):
            index = min(len(stop_calls) + 1, 4)
            stop_calls.append(index)
            for observer in observers:
                observer.safe_checkpoint("before_explicit_service_stop_" + str(index))
            try:
                return original_stop(*args, **kwargs)
            finally:
                for observer in observers:
                    observer.safe_checkpoint("after_explicit_service_stop_" + str(index))

        module._Cohort, module._stop_app = observed_cohort, observed_stop
        return module

    monkeypatch.setattr(original, "_helpers", helpers)
    began, primary_error = time.monotonic(), None
    try:
        original.test_native_windows_motion_recording_and_zones(
            lambda name, value: _record_original_property(record_testsuite_property, name, value)
        )
    except BaseException as error:
        primary_error = error
        raise
    finally:
        observations = []
        for observer in observers:
            try:
                observations.append(observer.finalize())
            except Exception as error:
                observations.append(
                    {
                        "diagnostic_only": True,
                        "normal_admission_changed": False,
                        "observer_error": _finite_error(error),
                    }
                )
        record_testsuite_property(
            PREFIX + "observations",
            json.dumps(
                {
                    "diagnostic_only": True,
                    "product_qualification": False,
                    "observer_count": len(observers),
                    "explicit_service_stop_calls": len(stop_calls),
                    "original_journey_passed": primary_error is None,
                    "original_error_type": _finite_error(primary_error),
                    "elapsed_seconds": round(time.monotonic() - began, 3),
                    "cohorts": observations,
                },
                sort_keys=True,
                allow_nan=False,
            ),
        )
    assert len(observers) == 1, "Diagnostic replay did not observe exactly its original cohort"
