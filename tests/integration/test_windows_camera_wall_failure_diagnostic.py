"""Replay exact f354 camera criteria with a separately identified diagnostic harness."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path

import pytest

PRODUCT_HEAD = "f354dc3c4412ef6e074545ee523dea0cf2a888f9"
SETUP_SHA256 = "44ae455d8937c8b39cd07085c981303b140616205ae7dfcc1235b37982f73942"
OVERLAY_SHA256 = "3502790fcf730bf0479265af2e3719e721f4d0c3721eca1a4e86c424ea008289"
SOURCE_PINS = {
    "test_windows_desktop_app.py": (
        "066a9cabf28b578cae675dc31a8dfeb04c7957a02e20a304d2cfb5b5aedcaaac"
    ),
    "windows_process_identity.py": (
        "49c8f6e726de75b3c0c17ff2bd8033eb262eb6789a415db72252bc2e77644f0d"
    ),
    "test_operator_product_runtime.py": (
        "7e662e77bb3c072119d0ae545d2193c9f909bf0229478659c5698272aca20050"
    ),
    "test_operator_beta_browser.py": (
        "cc148ea9da45d291e0031d1c04e5b1f63ad833bad4618f9c49c1a0e72e61549e"
    ),
    "onvif_video_camera_fixture.py": (
        "8d473fab0ff452274e9c04a64650f817f29fa41b3c5c7725bb2415495389d213"
    ),
}
PREFIX = "diagnostic_only_camera_wall_"
pytestmark = pytest.mark.skipif(
    os.name != "nt" or os.environ.get("K5_NATIVE_CAMERA_WALL_DIAGNOSTIC_ONLY") != "1",
    reason="Exact camera wall replay requires its separate disposable Windows diagnostic VM",
)


def _record_original_property(record, name, value):
    assert name in {"camera_video_windows_diagnostic", "camera_video_windows_receipt"}
    record(PREFIX + "original_" + name, value)


def test_diagnostic_only_exact_f354_camera_wall(record_testsuite_property):
    import test_windows_camera_video_settings as original

    harness_head = os.environ.get("K5_DIAGNOSTIC_HARNESS_HEAD_SHA", "")
    assert re.fullmatch(r"[a-f0-9]{40}", harness_head) and harness_head != PRODUCT_HEAD
    assert os.environ.get("K5_DESKTOP_HEAD_SHA") == PRODUCT_HEAD
    assert os.environ.get("K5_DESKTOP_SETUP_SHA256") == SETUP_SHA256
    assert os.environ.get("K5_NATIVE_CAMERA_VIDEO_ACCEPTANCE") == "1"
    assert all(os.environ.get(name) for name in original._INPUTS)
    directory = Path(__file__).parent
    for name, sha in SOURCE_PINS.items():
        assert (
            hashlib.sha256((directory / name).read_bytes().replace(b"\r\n", b"\n")).hexdigest()
            == sha
        )
    assert (
        hashlib.sha256(
            (directory / "test_windows_camera_video_settings.py").read_bytes()
        ).hexdigest()
        == OVERLAY_SHA256
    )
    wrapper_sha = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    assert wrapper_sha == os.environ["K5_CAMERA_WALL_WRAPPER_SHA256"]
    setup = Path(os.environ["K5_DESKTOP_SETUP"])
    assert setup.stat().st_size == 286579944
    with setup.open("rb") as stream:
        assert hashlib.file_digest(stream, "sha256").hexdigest() == SETUP_SHA256
    binding = {
        "schema_version": 1,
        "diagnostic_only": True,
        "product_qualification": False,
        "installer_head": PRODUCT_HEAD,
        "harness_head": harness_head,
        "installer_sha256": SETUP_SHA256,
        "installer_bytes": 286579944,
        "candidate_artifact_id": 14741238112,
        "candidate_run_id": 44989807369,
        "candidate_run_attempt": 1,
        "candidate_producer_job_id": 137108170949,
        "original_helper_canonical_lf_sha256": SOURCE_PINS,
        "observer_overlay_sha256": OVERLAY_SHA256,
        "wrapper_sha256": wrapper_sha,
        "original_deadlines_and_criteria": True,
        "product_source_changed": False,
    }
    record_testsuite_property(PREFIX + "binding", json.dumps(binding, sort_keys=True))
    started, error_type = time.monotonic(), None
    try:
        original.test_native_windows_camera_video_settings_and_automatic_wall(
            lambda name, value: _record_original_property(record_testsuite_property, name, value)
        )
    except BaseException as error:
        kind = type(error).__name__
        error_type = (
            kind
            if kind
            in {
                "Failed",
                "AssertionError",
                "TimeoutError",
                "RuntimeError",
                "OSError",
                "ValueError",
                "TypeError",
                "Error",
            }
            else "other"
        )
        raise
    finally:
        record_testsuite_property(
            PREFIX + "outcome",
            json.dumps(
                {
                    "diagnostic_only": True,
                    "product_qualification": False,
                    "original_journey_passed": error_type is None,
                    "original_error_type": error_type,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                },
                sort_keys=True,
            ),
        )
