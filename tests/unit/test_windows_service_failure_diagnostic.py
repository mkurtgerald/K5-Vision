"""Native failure evidence keeps exact public locations without exception secrets."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_service_failure_note_retains_source_location_and_only_finite_scm_fields():
    source = Path(__file__).parents[1] / "integration/test_windows_service_recording.py"
    spec = importlib.util.spec_from_file_location("k5_service_failure_test", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    secret = "private-password-and-camera-uri-must-not-appear"
    diagnostic = {}
    try:
        module._require(False, secret)
    except pytest.fail.Exception as error:
        module._failure_note(
            error,
            diagnostic,
            SimpleNamespace(
                status=lambda: {
                    "state": 4,
                    "win32_exit": 5,
                    "service_exit": 0,
                    "secret": secret,
                }
            ),
        )
    material = json.dumps(diagnostic)
    assert secret not in material and str(source.parent) not in material
    assert diagnostic["exception_type"] == "Failed"
    assert diagnostic["scm_at_failure"] == {"state": 4, "win32_exit": 5, "service_exit": 0}
    assert diagnostic["source_frames"]
    assert all(
        frame["file"] == source.name and 0 < frame["line"] < 10000
        for frame in diagnostic["source_frames"]
    )


def test_winsw_log_projection_never_includes_private_exception_or_process_material():
    source = Path(__file__).parents[1] / "integration/windows_service_acceptance.py"
    spec = importlib.util.spec_from_file_location("k5_service_log_test", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    secret = "private-camera-password-and-path"
    result = module._classify_log(
        (
            "Started process 1234 " + secret + "\nSystem.UnauthorizedAccessException " + secret
        ).encode()
    )
    assert secret not in json.dumps(result)
    assert result["process_started"] == 1
    assert result["access_denied"] is True
    assert result["dotnet_exception"] is True
