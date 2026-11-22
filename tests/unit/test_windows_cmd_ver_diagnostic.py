"""Finite command classification never admits a rejected service descendant."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

_PATH = Path(__file__).resolve().parents[1] / "integration/windows_process_identity.py"
_SPEC = importlib.util.spec_from_file_location("cmd_classification_native", _PATH)
_NATIVE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_NATIVE)
_SYSTEM = r"C:\Windows\System32"
_CMD = _SYSTEM + r"\cmd.exe"
_SID, _BIRTH = b"original service sid", 134398096549898289


def _details():
    return {
        "pid": 100,
        "created_100ns": _BIRTH,
        "parent_pid": 99,
        "sid": _SID,
        "session": 0,
        "image": _CMD,
    }


def _native(monkeypatch, *, fault=None):
    state = {"closed": [], "details": 0, "read": 0}

    def details(_identity):
        state["details"] += 1
        row = _details()
        if fault == "foreign_sid":
            row["sid"] = b"foreign"
        if fault == "wrong_session":
            row["session"] = 1
        if fault == "parent_change" and state["details"] > 1:
            row["parent_pid"] = 98
        return row

    native = SimpleNamespace(
        api=SimpleNamespace(
            OpenProcess=lambda *args: 101,
            CloseHandle=lambda handle: state["closed"].append(handle) or fault != "close_failure",
        ),
        system_directory=lambda: _SYSTEM,
        _handle_creation=lambda handle: _BIRTH + (1 if fault == "reuse" and state["read"] else 0),
        _live_parent_birth=lambda handle: None if fault == "exited" else _BIRTH,
        details=details,
    )
    monkeypatch.setattr(_NATIVE, "_same_local_file", lambda actual, expected: fault != "copied_cmd")

    def command_line(pid):
        assert pid == 100
        state["read"] += 1
        if fault == "query_failure":
            raise OSError("rtsp://private:secret-password@camera")
        return [_CMD, "/c", "ver"]

    return native, command_line, state


@pytest.mark.parametrize("tail", ["ver", "VER", '"ver"'])
def test_exact_cpython_version_query_arguments_are_finite_only(tail):
    assert _NATIVE._cmd_ver_arguments([_CMD, "/c", tail], _SYSTEM)


@pytest.mark.parametrize(
    "argv",
    [
        [_CMD, "/c", "ver & private-command"],
        [_CMD, "/c", "ver;whoami"],
        [_CMD, "/c", "cmd /c ver"],
        [_CMD, "/k", "ver"],
        [_CMD, "/c", "ver", "private-token"],
        [r"C:\private\cmd.exe", "/c", "ver"],
        ["cmd.exe", "/c", "ver"],
        [_CMD, "/c", " ver"],
        [_CMD, "/c", "ver\nwhoami"],
        [_CMD, "/c", None],
    ],
)
def test_nested_payload_or_changed_commands_never_match(argv):
    assert not _NATIVE._cmd_ver_arguments(argv, _SYSTEM)


def test_native_original_process_is_bound_before_and_after_private_read(monkeypatch):
    native, read, state = _native(monkeypatch)
    result = _NATIVE._cmd_ver_diagnostic(_details(), _SID, native=native, command_line=read)
    assert result["status"] == "observed" and result["argv_class"] == "cmd_ver"
    assert result["admission_changed"] is False and result["original_native_process_bound"]
    assert state["details"] == 2 and state["read"] == 1 and state["closed"] == [101]
    assert _CMD not in str(result) and "ver &" not in str(result)


@pytest.mark.parametrize(
    "fault",
    [
        "foreign_sid",
        "wrong_session",
        "parent_change",
        "reuse",
        "exited",
        "copied_cmd",
        "query_failure",
        "close_failure",
    ],
)
def test_unavailable_or_changed_process_never_supplies_command_witness(monkeypatch, fault):
    native, read, state = _native(monkeypatch, fault=fault)
    result = _NATIVE._cmd_ver_diagnostic(_details(), _SID, native=native, command_line=read)
    assert result["status"] == "unavailable" and "argv_class" not in result
    assert result["admission_changed"] is False and state["closed"] == [101]
    assert "secret-password" not in str(result)


def test_even_observed_cmd_ver_retains_original_ownership_failure(monkeypatch):
    monkeypatch.setattr(
        _NATIVE,
        "_cmd_ver_diagnostic",
        lambda *args: {
            "status": "observed",
            "argv_class": "cmd_ver",
            "admission_changed": False,
        },
    )
    note = {
        "basename_category": "cmd",
        "fixed_known_file": {
            "status": "observed",
            "scope": "fixed_system32",
            "same_file": True,
            "trusted_code_custody_verified": True,
        },
    }
    with pytest.raises(_NATIVE.OwnershipFailure) as failure:
        _NATIVE.service_candidate(_details(), _SID, {}, diagnose_image=lambda *args: note)
    result = failure.value.diagnostic
    assert result["accepted"] is False and result["outcome"] == "unexpected_service_image"
    assert result["image_diagnostic"]["command_observation"]["argv_class"] == "cmd_ver"


@pytest.mark.parametrize("fixed", [None, {}, {"status": "different_fixed_file"}])
def test_unverified_fixed_file_never_reads_process_arguments(monkeypatch, fixed):
    def forbidden(*args):
        pytest.fail("Unverified file attempted private argument read")

    monkeypatch.setattr(_NATIVE, "_cmd_ver_diagnostic", forbidden)
    with pytest.raises(_NATIVE.OwnershipFailure):
        _NATIVE.service_candidate(
            _details(),
            _SID,
            {},
            diagnose_image=lambda *args: {
                "basename_category": "cmd",
                "fixed_known_file": fixed,
            },
        )


def test_rejected_other_command_keeps_raw_arguments_out_of_actual_failure_property(monkeypatch):
    import ast
    import json

    native, _read, _state = _native(monkeypatch)
    secret = "ver & rtsp://private-user:secret-password@private-camera"
    observed = _NATIVE._cmd_ver_diagnostic(
        _details(), _SID, native=native, command_line=lambda _pid: [_CMD, "/c", secret]
    )
    assert observed["argv_class"] == "other"
    note = {
        "accepted": False,
        "outcome": "unexpected_service_image",
        "image_diagnostic": {"command_observation": observed},
    }
    source = ast.parse(_PATH.with_name("test_windows_motion_recording.py").read_text())
    branch = next(
        node
        for node in ast.walk(source)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.BoolOp)
        and ast.unparse(node.test) == "failed or cleanup_failed"
    )
    emitted = []
    context = {
        "diagnostic": {
            "stage": "owned_stop_and_uninstall",
            "process_ownership": {"instances": [note]},
            "private_source": secret,
        },
        "failed": True,
        "cleanup_failed": True,
        "json": json,
        "record": lambda name, value: emitted.append((name, value)),
    }
    exec(
        compile(
            ast.fix_missing_locations(ast.Module(body=branch.body[:3], type_ignores=[])),
            "actual_failure_projection",
            "exec",
        ),
        context,
    )
    assert emitted[0][0] == "motion_windows_diagnostic"
    assert "secret-password" not in emitted[0][1] and "private-camera" not in emitted[0][1]
    assert json.loads(emitted[0][1])["process_ownership"]["instances"][0]["accepted"] is False
