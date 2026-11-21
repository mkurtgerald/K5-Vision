"""Native failure evidence keeps exact public locations without exception secrets."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def _snapshot_helper():
    source = Path(__file__).parents[1] / "integration/windows_service_acceptance.py"
    spec = importlib.util.spec_from_file_location("k5_service_snapshot_test", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _ordinary_helper():
    source = Path(__file__).parents[1] / "integration/windows_standard_user_acceptance.py"
    spec = importlib.util.spec_from_file_location("k5_ordinary_failure_test", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


def test_winsw_projection_keeps_only_fixed_exception_classes_and_bounded_native_numbers():
    helper = _snapshot_helper()
    result = helper._classify_log(
        b"System.ComponentModel.Win32Exception (0x80004005): private-secret\n"
        b"System.Security.Principal.IdentityNotMappedException private-user\n"
        b"HRESULT=-2147024891 NativeErrorCode=5 Win32 error code: 1069\n"
        b"System.PrivateUserSecretException HResult=4294967296 Win32Error=65536\n"
    )
    assert result["exception_types"] == [
        "System.ComponentModel.Win32Exception",
        "System.Security.Principal.IdentityNotMappedException",
    ]
    assert result["hresults"] == [0x80004005, 0x80070005]
    assert result["win32_errors"] == [5, 1069]
    assert "private" not in json.dumps(result).casefold()


def test_bootstrap_projection_preserves_only_complete_known_public_envelopes():
    helper = _snapshot_helper()
    progress = {
        "schema_version": 1,
        "k5_recorder_bootstrap": "identity_probe",
        "host_pid": 123,
        "host_created_100ns": 133000000000000000,
    }
    failure = {**progress, "failure_type": "IOException", "failure_code": 5}
    raw = (
        "private-password-and-path\n" + json.dumps(progress) + "\n" + json.dumps(failure)
    ).encode()
    assert helper._bootstrap_envelopes(raw) == [progress, failure]
    assert "private" not in json.dumps(helper._bootstrap_envelopes(raw))


@pytest.mark.parametrize(
    "value",
    [
        {"schema_version": True, "k5_recorder_bootstrap": "token"},
        {"schema_version": 2, "k5_recorder_bootstrap": "token"},
        {"schema_version": 1, "k5_recorder_bootstrap": "private-path"},
        {"schema_version": 1, "k5_recorder_bootstrap": ["token"]},
        {"schema_version": 1, "k5_recorder_bootstrap": "token", "path": "private"},
        {"schema_version": 1, "k5_recorder_bootstrap": "token", "failure_type": "private"},
        {
            "schema_version": 1,
            "k5_recorder_bootstrap": "token",
            "failure_type": "IOException",
            "failure_code": True,
        },
        {
            "schema_version": 1,
            "k5_recorder_bootstrap": "token",
            "failure_type": "IOException",
            "failure_code": -1,
        },
        {
            "schema_version": 1,
            "k5_recorder_bootstrap": "token",
            "failure_type": "IOException",
            "failure_code": 65536,
        },
        {
            "schema_version": 1,
            "k5_recorder_bootstrap": "token",
            "failure_type": "private",
            "failure_code": 1,
        },
    ],
    ids=[
        "bool-schema",
        "schema2",
        "unknown-stage",
        "nontext-stage",
        "extra-path",
        "partial-failure",
        "bool-code",
        "negative-code",
        "large-code",
        "unknown-class",
    ],
)
def test_bootstrap_projection_rejects_unknown_private_or_incomplete_fields(value):
    bound = {"host_pid": 123, "host_created_100ns": 133000000000000000, **value}
    assert _snapshot_helper()._bootstrap_envelopes(json.dumps(bound).encode()) == []


def test_bootstrap_projection_rejects_duplicates_and_caps_repeated_boot_events():
    helper = _snapshot_helper()
    duplicate = (
        b'{"schema_version":1,"schema_version":1,"k5_recorder_bootstrap":"token",'
        b'"host_pid":123,"host_created_100ns":133000000000000000}'
    )
    assert helper._bootstrap_envelopes(duplicate) == []
    line = (
        b'{"schema_version":1,"k5_recorder_bootstrap":"token",'
        b'"host_pid":123,"host_created_100ns":133000000000000000}\n'
    )
    assert len(helper._bootstrap_envelopes(line * 100)) == 16


def test_bootstrap_binding_discards_previous_appended_hosts_and_pid_reuse():
    helper = _snapshot_helper()
    binding = {"pid": 123, "created_100ns": 133000000000000000}
    current = {
        "schema_version": 1,
        "k5_recorder_bootstrap": "payload_verify",
        "host_pid": binding["pid"],
        "host_created_100ns": binding["created_100ns"],
    }
    previous = {**current, "host_created_100ns": current["host_created_100ns"] + 1728000000000}
    unrelated = {**current, "host_pid": 124}
    raw = "\n".join(json.dumps(value) for value in (previous, current, unrelated)).encode()
    assert helper._bound_bootstrap(raw, {"host": {"instances": [binding]}}) == [current]
    assert helper._bound_bootstrap(raw, {}) == []


def test_ordinary_probe_preserves_unwrapped_public_class_code_frames_and_completed_checks():
    diagnostic = {}
    _ordinary_helper()._probe_failure_note(
        {
            "failure_stage": "product_status",
            "failure_type": "UnauthorizedAccessException",
            "failure_outer_type": "TargetInvocationException",
            "failure_hresult": 0x80070005,
            "failure_win32_error": 5,
            "failure_message": "private-path-and-password",
            "failure_product_frames": [
                "RecorderNative.AdmitStatusOwner",
                "private-path-and-password",
                "RecorderClient.ExchangeOwned",
            ],
            "ordinary_token_user_verified": True,
            "token_elevated": False,
        },
        diagnostic,
    )
    assert diagnostic["ordinary_probe_exception_type"] == "UnauthorizedAccessException"
    assert diagnostic["ordinary_probe_outer_exception_type"] == "TargetInvocationException"
    assert diagnostic["ordinary_probe_hresult"] == 0x80070005
    assert diagnostic["ordinary_probe_win32_error"] == 5
    assert diagnostic["ordinary_probe_product_frames"] == [
        "RecorderNative.AdmitStatusOwner",
        "RecorderClient.ExchangeOwned",
    ]
    assert diagnostic["ordinary_probe_completed_checks"] == {
        "ordinary_token_user_verified": True,
        "token_elevated": False,
    }
    assert "private" not in json.dumps(diagnostic)


def test_ordinary_probe_projection_rejects_unbounded_numbers_unknown_classes_and_private_stages():
    diagnostic = {}
    _ordinary_helper()._probe_failure_note(
        {
            "failure_stage": ["private"],
            "failure_type": "private",
            "failure_outer_type": {},
            "failure_hresult": True,
            "failure_win32_error": 65536,
            "failure_product_frames": ["private"],
        },
        diagnostic,
    )
    assert diagnostic == {
        "ordinary_probe_product_frames": [],
        "ordinary_probe_completed_checks": {},
    }
