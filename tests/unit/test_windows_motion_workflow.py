"""The motion claim requires its own same-installer Windows recording witness."""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/operator-desktop-windows.yml"


def _job(name):
    text = WORKFLOW.read_text(encoding="utf-8")
    match = re.search(r"(?m)^  " + re.escape(name) + r":\n", text)
    assert match is not None
    remaining = text[match.end() :]
    end = re.search(r"(?m)^  [a-z0-9-]+:\n", remaining)
    return remaining if end is None else remaining[: end.start()]


def test_motion_is_a_ninth_independent_success_required_native_job():
    motion = _job("motion-recording")
    assert "name: Exercise installed motion recording, zones and actual clock corrections" in motion
    assert "needs: [native-desktop]" in motion
    assert "runs-on: windows-2025" in motion
    assert "    timeout-minutes: 10\n" in motion
    assert "continue-on-error:" not in motion and "focused-windows" not in motion
    assert 'K5_NATIVE_MOTION_ACCEPTANCE: "1"' in motion
    assert 'K5_VM_CLOCK_ACCEPTANCE: "1"' in motion
    assert "K5_OPERATOR_BROWSER_CHANNEL: msedge" in motion
    assert "        timeout-minutes: 8\n" in motion
    assert "matrix.clock_run" not in motion
    text = WORKFLOW.read_text(encoding="utf-8")
    assert set(re.findall(r"(?m)^  ([a-z0-9-]+):\n", text)) == {
        "focused-windows",
        "native-desktop",
        "camera-video",
        "motion-recording",
        "vm-clock",
        "cross-revision-upgrade",
        "service-recording",
        "time-administration",
    }
    assert "lane: [setup, onvif, cameras]" in text
    assert "clock_run: [1, 2]" in _job("vm-clock")


def test_motion_download_and_receipt_bind_the_actual_current_setup_bytes():
    motion = _job("motion-recording")
    assert "artifact-ids: ${{ needs.native-desktop.outputs.setup_artifact_id }}" in motion
    assert "ref: ${{ github.event.pull_request.head.sha || github.sha }}" in motion
    assert "persist-credentials: false" in motion
    for check in (
        "$package.head_sha -ne $env:K5_DESKTOP_HEAD_SHA",
        "$manifest.head_sha -ne $env:K5_DESKTOP_HEAD_SHA",
        "$package.setup_sha256 -ne $sha",
        "$package.setup_bytes -ne $setup.Length",
        "$package.payload_manifest_sha256 -ne (Get-FileHash",
        "$package.motion_recording -ne $true",
        "$package.camera_trigger_zones -ne $true",
        "$package.no_terminal_required -ne $true",
        "$package.workers_bundled -ne $true",
    ):
        assert check in motion
    assert "--expected-head $env:K5_DESKTOP_HEAD_SHA" in motion
    assert "--installer-sha256 $env:K5_DESKTOP_SETUP_SHA256" in motion
    assert "releases/latest" not in motion and "workflow_dispatch" not in motion


def test_motion_executes_real_test_then_strict_source_verifier_and_retains_raw_failure():
    motion = _job("motion-recording")
    exercise = motion.split("Exercise real installed motion zones", 1)[1]
    command = "python -m pytest tests/integration/test_windows_motion_recording.py"
    verifier = "python scripts/verify_windows_motion_recording_receipt.py"
    assert exercise.index(command) < exercise.index(verifier)
    assert "--junitxml=motion-windows.xml" in exercise
    assert exercise.count("if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }") == 2
    assert "--ignore" not in exercise and "|| true" not in exercise
    evidence = motion.split("Preserve finite motion evidence after UTC restoration", 1)[1]
    assert "if: always()" in evidence
    assert "name: motion-windows-witness-${{ github.run_id }}" in evidence
    assert "motion-windows.xml" in evidence and "motion-windows-receipt.json" in evidence
    assert "retention-days: 14" in evidence
    assert "uses: ./.github/actions/provision-operator-tools" in motion
    assert motion.index("Provision immutable generated-media tools") < motion.index(command)


def test_existing_clock_guards_remain_separate_and_keep_their_original_budget():
    clock = _job("vm-clock")
    assert "timeout-minutes: 15" in clock
    assert "clock_run: [1, 2]" in clock and "fail-fast: false" in clock
    assert "K5_NATIVE_MOTION_ACCEPTANCE" not in clock
    assert "--scope vm-clock-dedicated" in clock
    assert "Preserve finite VM clock evidence after UTC restoration" in clock
    assert "vm-clock-repeat-{0}-windows-witness-{1}" in clock


def test_new_sources_and_controls_trigger_native_qualification_and_run_on_windows():
    text = WORKFLOW.read_text(encoding="utf-8")
    for path in (
        "tests/integration/test_windows_motion_recording.py",
        "tests/integration/motion_camera_fixture.py",
        "scripts/verify_windows_motion_recording_receipt.py",
        "tests/unit/test_windows_motion_recording_receipt.py",
        "tests/unit/test_motion_camera_fixture.py",
        "tests/unit/test_operator_motion*.py",
        "tests/unit/test_operator_recording_policy*.py",
        "tests/unit/test_windows_motion_workflow.py",
    ):
        assert "      - " + path in text
    focused = _job("focused-windows")
    setup, rest = focused.split("Verify ONVIF and RTSP authentication", 1)
    onvif, cameras = rest.split("Verify camera administration", 1)
    for name in (
        "test_operator_motion.py",
        "test_operator_motion_process.py",
        "test_operator_recording_policy.py",
        "test_windows_motion_recording_receipt.py",
        "test_motion_camera_fixture.py",
        "test_windows_motion_workflow.py",
    ):
        assert "tests/unit/" + name in setup
    assert "tests/unit/test_operator_motion_ui.py" in onvif
    assert "tests/unit/test_operator_recording_policy_api.py" in cameras
    assert "tests/unit/test_operator_motion_authority.py" in cameras
    for source in (focused, (ROOT / ".github/workflows/ci.yml").read_text()):
        assert '"numpy==2.2.6"' in source
        assert '"opencv-python-headless==5.0.0.93"' in source
        assert "openvino==" not in source


def test_motion_feature_inventory_and_package_preflights_agree_without_claiming_camera_scope():
    tree = ast.parse((ROOT / "scripts/build_desktop_payload.py").read_text())
    flags = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if isinstance(key, ast.Constant) and key.value in {
                    "motion_recording",
                    "camera_trigger_zones",
                }:
                    flags[key.value] = ast.literal_eval(value)
    assert flags == {"motion_recording": True, "camera_trigger_zones": True}
    native = _job("native-desktop")
    assert "motion_recording=$true; camera_trigger_zones=$true" in native
    camera = _job("camera-video")
    assert "$package.motion_recording -ne $true" in camera
    assert "$package.camera_trigger_zones -ne $true" in camera
    # Its existing raw camera receipt still concerns encoder/wall operations;
    # motion qualification is performed by the separate exact-source verifier.
    assert "verify_windows_camera_video_settings_receipt.py" in camera
    assert "verify_windows_motion_recording_receipt.py" not in camera
