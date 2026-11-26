"""Retain a failed install's finite guard boundary without its private caption."""

from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_operator_time_admin import private_profile as private_profile_fixture

private_profile = private_profile_fixture


def _module(filename, name):
    path = Path(__file__).resolve().parents[1] / "integration" / filename
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_install_refusal_reaches_actual_outer_failure_property_without_private_caption(
    private_profile, monkeypatch
):
    harness = _module("test_windows_desktop_upgrade.py", "legacy_install_failure_harness")
    desktop = _module("test_windows_desktop_app.py", "legacy_install_failure_formatter")
    emitted, installer_calls, cleanup = [], [], []
    primary = pytest.fail.Exception("Generated installer refusal")
    private = "PRIVATE-CAPTION rtsp://generated-user:private-password@private-camera/live"
    guard_code = (27 << 16) | 5
    installed, machine_profile = private_profile / "application", private_profile / "machine"

    class Cohort:
        console_seen = failed = False

        def finish(self):
            cleanup.append("cohort")

        def snapshot(self):
            return []

    def install(setup, target, api, *, diagnostic=None):
        installer_calls.append(diagnostic)
        # Match _install_gui's existing behavior: an omitted diagnostic uses an
        # internal dictionary, so its finite failure fields never reach callers.
        diagnostic = {} if diagnostic is None else diagnostic
        # Use the actual production-harness refusal parser. Native windows are
        # replaced only at this unit fixture boundary; no native result claimed.
        assert desktop._installer_finish_refusal(
            {
                "K5 Vision installation failed": 1,
                f"Install check: start/{guard_code}. {private} C:\\private\\profile": 2,
            },
            diagnostic,
        )
        diagnostic["installer_exit_code"] = None
        diagnostic["installer_steps_seen"] = ["install", "next"]
        raise primary

    def remove(path):
        cleanup.append("temporary")
        shutil.rmtree(path)

    runtime = SimpleNamespace(_helpers=lambda: SimpleNamespace())
    facade = SimpleNamespace(
        _helpers=lambda name: runtime if name == "test_operator_product_runtime" else object(),
        _machine_paths=lambda: (installed, machine_profile),
        _Windows=lambda: object(),
        _Cohort=lambda api: Cohort(),
        _install_gui=install,
        _remove_owned_temporary_tree=remove,
    )
    baseline, candidate = private_profile / "baseline.exe", private_profile / "candidate.exe"
    baseline.write_bytes(b"generated baseline input")
    candidate.write_bytes(b"generated candidate input")
    inputs = {
        "K5_DESKTOP_BASELINE_SETUP": str(baseline),
        "K5_DESKTOP_BASELINE_HEAD_SHA": "a" * 40,
        "K5_DESKTOP_BASELINE_SETUP_SHA256": "b" * 64,
        "K5_DESKTOP_BASELINE_LAUNCHER_SHA256": "c" * 64,
        "K5_DESKTOP_SETUP": str(candidate),
        "K5_DESKTOP_HEAD_SHA": "d" * 40,
        "K5_DESKTOP_SETUP_SHA256": "e" * 64,
        "K5_DESKTOP_LAUNCHER_SHA256": "f" * 64,
        "LOCALAPPDATA": str(private_profile),
        "GITHUB_ACTIONS": "true",
        "RUNNER_ENVIRONMENT": "github-hosted",
    }
    for name, value in inputs.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(harness, "_desktop_helpers", lambda: facade)
    monkeypatch.setattr(harness, "_admit_setup", lambda *arguments: None)
    with pytest.raises(pytest.fail.Exception) as error:
        harness.test_native_windows_legacy_service_migration_and_manual_reversal(
            lambda name, value: emitted.append((name, json.loads(value)))
        )
    assert error.value is primary
    assert len(installer_calls) == 1
    assert cleanup == ["cohort", "temporary"]
    notes = dict(emitted)
    assert set(notes) == {"legacy_migration_windows_diagnostic"}
    diagnostic = notes["legacy_migration_windows_diagnostic"]
    assert diagnostic == {
        "phase": "baseline_install",
        "installer_action": "final_admission_refused",
        "installer_failed_stage": "start",
        "installer_guard_exit_code": guard_code,
        "installer_guard_phase": 27,
        "installer_guard_win32": 5,
        "installer_exit_code": None,
        "installer_steps_seen": ["install", "next"],
        "cleanup_failures": [],
        "no_console_windows": True,
    }
    serialized = json.dumps(diagnostic, allow_nan=False)
    assert private not in serialized and "private-password" not in serialized
    assert "C:\\private\\profile" not in serialized
