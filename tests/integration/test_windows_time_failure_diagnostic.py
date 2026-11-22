"""Diagnostic-only exact-b6 replay; never emits a normal qualification receipt."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

import pytest

PRODUCT_HEAD = "b6e725ac6e63dbfda5e4f1b7c20d47f311075c3c"
SETUP_SHA256 = "a638d8455d125330bfe2433c22a73a47172775522fa9b73415e38332e7ac9f4b"
SOURCE_PINS = {
    "tests/integration/test_windows_time_administration.py": (
        "06180f43158ed175170b9a108223fdd8956731b97252ff57806d6bb3c25f8071"
    ),
    "tests/integration/windows_time_configuration.py": (
        "5674175dca9a9e1de8b9870f3546097529a9fc44cc913fb902f471ddaef83d15"
    ),
    "src/k5vision/operator_time_admin.py": (
        "babaa226e6f0ae1a7fa6089108724bbea56212f51f336d0a8b660d2521389ead"
    ),
    "tests/integration/test_operator_product_runtime.py": (
        "d7c7eaa38fe36843a937e57d60b696a93bca66599b3883e51d84cd1f37345106"
    ),
}
pytestmark = pytest.mark.skipif(
    os.name != "nt" or os.environ.get("K5_NATIVE_TIME_FAILURE_DIAGNOSTIC") != "1",
    reason="Exact installed b6 diagnostic requires its separate disposable Windows VM",
)


def test_diagnostic_only_exact_b6_time_failure_retention(record_testsuite_property, monkeypatch):
    import test_windows_time_administration as original
    import windows_time_configuration as configuration
    from windows_time_failure_observer import TimeObserver, finite_error

    harness_head = os.environ.get("K5_DIAGNOSTIC_HARNESS_HEAD_SHA", "")
    assert re.fullmatch(r"[a-f0-9]{40}", harness_head) and harness_head != PRODUCT_HEAD
    assert os.environ.get("K5_DESKTOP_HEAD_SHA") == PRODUCT_HEAD
    assert os.environ.get("K5_DESKTOP_SETUP_SHA256") == SETUP_SHA256
    assert original._ENABLED
    root = Path(__file__).resolve().parents[2]
    for relative, expected in SOURCE_PINS.items():
        raw = (root / relative).read_bytes().replace(b"\r\n", b"\n")
        assert hashlib.sha256(raw).hexdigest() == expected, "Original b6 Time source differs"
    setup = Path(os.environ["K5_DESKTOP_SETUP"])
    assert setup.stat().st_size == 286524130
    with setup.open("rb") as stream:
        assert hashlib.file_digest(stream, "sha256").hexdigest() == SETUP_SHA256
    binding = {
        "product_head": PRODUCT_HEAD,
        "harness_head": harness_head,
        "installer_sha256": SETUP_SHA256,
        "installer_bytes": 286524130,
        "candidate_artifact_id": 14736951903,
        "candidate_run_id": 44979183807,
        "candidate_run_attempt": 1,
        "candidate_producer_job_id": 137075697725,
        "original_source_canonical_lf_sha256": SOURCE_PINS,
        "diagnostic_only": True,
        "normal_qualification": False,
        "product_source_changed": False,
        "original_deadlines_and_criteria": True,
    }
    record_testsuite_property(
        "diagnostic_only_b6_time_binding", json.dumps(binding, sort_keys=True)
    )

    observer = TimeObserver(original)
    original_request, original_approve = original._time_request, original._approve_time

    def observed_request(client, operation):
        return observer.request(original_request, client, operation)

    def observed_approve(client, operation, **kwargs):
        failure = None
        observer.begin(operation, kwargs["profile"], kwargs["service"])
        try:
            return original_approve(client, operation, **kwargs)
        except BaseException as error:
            failure = error
            raise
        finally:
            observer.end(kwargs["service"], failure)

    monkeypatch.setattr(original, "_time_request", observed_request)
    monkeypatch.setattr(original, "_approve_time", observed_approve)
    monkeypatch.setattr(
        original, "_TimeHelperTracker", observer.tracker_class(original._TimeHelperTracker)
    )
    monkeypatch.setattr(
        configuration,
        "WindowsTimeConfigurationGuard",
        observer.guard_class(configuration.WindowsTimeConfigurationGuard),
    )

    def diagnostic_property(name, value):
        # The original implementation's finite witnesses remain diagnostic:
        # normal verifier property/test names can never admit this replay.
        assert name.startswith("k5_native_time_")
        record_testsuite_property("diagnostic_only_b6_time_original_" + name, value)

    primary_error = None
    try:
        original.test_native_windows_time_configuration_readback_and_resync_with_restoration(
            diagnostic_property
        )
    except BaseException as error:
        primary_error = error
        raise
    finally:
        try:
            observed = observer.finish()
            observed["original_journey_passed"] = primary_error is None
            observed["original_journey_error"] = (
                None if primary_error is None else finite_error(primary_error)
            )
        except Exception as error:
            observed = {
                "diagnostic_only": True,
                "normal_qualification": False,
                "observer_finish_error": finite_error(error),
            }
        record_testsuite_property(
            "diagnostic_only_b6_time_observations",
            json.dumps(observed, sort_keys=True, allow_nan=False),
        )
