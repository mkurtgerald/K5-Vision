"""Keep SCM qualification independent, byte-bound and honest about hosted boot limits."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/operator-desktop-windows.yml"


def _job(name):
    source = WORKFLOW.read_text(encoding="utf-8")
    match = re.search(r"(?m)^  " + re.escape(name) + r":\n", source)
    assert match is not None
    remaining = source[match.end() :]
    end = re.search(r"(?m)^  [a-z0-9-]+:\n", remaining)
    return remaining if end is None else remaining[: end.start()]


def test_service_lane_runs_when_the_original_gui_journey_fails_after_build():
    service = _job("service-recording")
    native = _job("native-desktop")
    assert "needs: [native-desktop]" in service
    assert "always() && !cancelled()" in service
    assert "needs.native-desktop.result == 'failure'" in service
    assert "needs.native-desktop.outputs.candidate_artifact_id != ''" in service
    assert "runs-on: windows-2025" in service
    assert "continue-on-error:" not in service
    assert 'K5_NATIVE_SERVICE_ACCEPTANCE: "1"' in service
    assert (
        "candidate_artifact_id: ${{ steps.unqualified_desktop_candidate.outputs.artifact-id }}"
        in native
    )
    assert "id: unqualified_desktop_candidate" in native
    assert native.index("id: unqualified_desktop_candidate") < native.index(
        "Exercise actual installer"
    )


def test_service_lane_uses_original_current_attempt_artifact_and_exact_candidate_bytes():
    service = _job("service-recording")
    assert "EXECUTION_HEAD_SHA: ${{ github.event.pull_request.head.sha || github.sha }}" in service
    assert "actions: read" in service
    assert "artifact-ids: ${{ needs.native-desktop.outputs.candidate_artifact_id }}" in service
    assert "actions/artifacts/$env:CANDIDATE_ARTIFACT_ID" in service
    for check in (
        "$meta.id -ne $env:CANDIDATE_ARTIFACT_ID",
        "$meta.name -ne $expectedName",
        "$meta.workflow_run.id -ne $env:GITHUB_RUN_ID",
        "$meta.expired -ne $false",
        "$meta.workflow_run.head_sha -ne $env:EXECUTION_HEAD_SHA",
        '$package.qualification_status -ne "unqualified"',
        "$package.head_sha -ne $env:K5_DESKTOP_HEAD_SHA",
        "$manifest.head_sha -ne $env:K5_DESKTOP_HEAD_SHA",
        "$package.setup_sha256 -ne $sha",
        "$package.setup_bytes -ne $setup.Length",
        "$package.payload_manifest_sha256 -ne (Get-FileHash",
        '$manifest.files.path -notcontains "K5VisionRecorder.exe"',
        '$manifest.files.path -notcontains "K5VisionRecorder.xml"',
    ):
        assert check in service
    assert (
        "build_desktop_payload.py" not in service and "compile_desktop_launcher.py" not in service
    )
    assert "releases/latest" not in service


def test_time_gate_qualifies_the_installed_service_from_the_same_candidate():
    time = _job("time-administration")
    assert "needs: [native-desktop]" in time and "always() && !cancelled()" in time
    assert "artifact-ids: ${{ needs.native-desktop.outputs.candidate_artifact_id }}" in time
    assert 'K5_NATIVE_TIME_ACCEPTANCE: "1"' in time
    assert 'K5_VM_CLOCK_ACCEPTANCE: "1"' in time
    assert "tests/integration/test_windows_time_administration.py" in time
    assert "scripts/verify_windows_time_administration_receipt.py" in time
    assert "--installer-sha256 $env:K5_DESKTOP_SETUP_SHA256" in time
    assert "continue-on-error:" not in time
    assert "time-administration-windows-receipt.json" in time


def test_service_lane_runs_real_installed_test_then_strict_receipt_and_retains_failure():
    service = _job("service-recording")
    test = "python -m pytest tests/integration/test_windows_service_recording.py"
    verifier = "python scripts/verify_windows_service_receipt.py"
    assert service.index(test) < service.index(verifier)
    assert "--junitxml=service-windows.xml" in service
    assert "--expected-head $env:K5_DESKTOP_HEAD_SHA" in service
    assert "--installer-sha256 $env:K5_DESKTOP_SETUP_SHA256" in service
    assert "--ignore" not in service and "|| true" not in service
    assert "service-candidate-provenance.json" in service
    evidence = service.split("Preserve finite service evidence", 1)[1]
    assert "if: always()" in evidence
    assert "service-windows.xml" in evidence and "service-windows-receipt.json" in evidence
    assert "service-windows-witness-${{ github.run_id }}" in evidence
    assert "unqualified" in service


def test_service_lane_addition_preserves_original_real_clock_vm_budgets():
    clock, motion = _job("vm-clock"), _job("motion-recording")
    assert "timeout-minutes: 15" in clock and "clock_run: [1, 2]" in clock
    assert "--scope vm-clock-dedicated" in clock
    assert "timeout-minutes: 10" in motion and 'K5_VM_CLOCK_ACCEPTANCE: "1"' in motion
    assert "service-recording" not in clock and "service-recording" not in motion


def test_native_qualification_is_triggered_by_each_service_acceptance_source():
    source = WORKFLOW.read_text(encoding="utf-8")
    for name in (
        "tests/integration/test_windows_service_recording.py",
        "tests/integration/windows_service_acceptance.py",
        "tests/integration/windows_standard_user_acceptance.py",
        "tests/integration/windows_standard_user_probe.cs",
        "scripts/verify_windows_service_receipt.py",
        "tests/unit/test_windows_service_receipt.py",
        "tests/unit/test_windows_service_workflow.py",
    ):
        assert "      - " + name in source
