"""Hosted source-contract checks for deferred physical RTSP witness wiring."""

from pathlib import Path

WORKFLOW = Path(".github/workflows/stage-one-operator-physical.yml")
ADMISSION = Path("scripts/assert-stage-one-physical-admission.ps1")
LAUNCHER = Path("scripts/windows-alpha/Invoke-K5StageOneRtspJoinedAnalyticsWitness.ps1")


def _step(name: str) -> str:
    return (
        WORKFLOW.read_text(encoding="utf-8")
        .split(f"      - name: {name}\n", 1)[1]
        .split("      - name: ", 1)[0]
    )


def test_rtsp_wiring_preserves_existing_triggers_pins_and_dependency_isolation() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "on:\n  push:\n    branches:\n      - main\n  workflow_dispatch:\n" in text
    assert "if: github.ref == 'refs/heads/main'" in text
    assert "runs-on: [self-hosted, Windows, X64, k5-physical, camera-lab]" in text
    assert "group: stage-one-operator-physical\n  cancel-in-progress: false" in text
    assert "ANALYTICS_LAB_SHA: c8b347ae538991a0c0ce38eabc2dc17b566531d3" in text
    assert "K5_STAGE_ONE_REVISION: ${{ github.sha }}" in text
    assert '"openvino==2026.3.1" "opencv-python-headless==4.12.0.88"' in text
    assert "python -m venv $venvPath" in text
    assert '"PIP_REQUIRE_VIRTUALENV=true"' in text


def test_idle_host_is_observed_before_provisioning_and_again_after_alpha_cleanup() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    names = [
        "Checkout exact Stage One revision",
        "Require idle exact physical host before provisioning",
        "Provision isolated reviewed GStreamer runtime",
        "Prove installed alpha generated RTSP Windows presentation",
        "Recheck exact physical host before joined RTSP witness",
        "Prove reviewed clip RTSP joined analytics Windows presentation",
        "Verify joined RTSP process cleanup",
        "Assert retained witness is source-free",
    ]
    positions = [text.index(f"      - name: {name}\n") for name in names]
    assert positions == sorted(positions)
    for name in (names[1], names[4], names[6]):
        step = _step(name)
        assert "timeout-minutes: 1" in step
        assert r"run: .\scripts\assert-stage-one-physical-admission.ps1" in step
        assert "continue-on-error" not in step
    cleanup = _step(names[6])
    assert "always()" in cleanup
    assert "steps.rtsp_joined.outcome == 'success'" in cleanup
    assert "steps.rtsp_joined.outcome == 'failure'" in cleanup
    assert "steps.rtsp_joined.outcome == 'cancelled'" in cleanup


def test_workflow_reuses_existing_launcher_without_competing_test_invocation() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    step = _step("Prove reviewed clip RTSP joined analytics Windows presentation")
    assert "K5_STAGE_ONE_RTSP_JOINED_ANALYTICS_PHYSICAL" not in text
    assert "test_stage_one_rtsp_joined_analytics_physical.py" not in text
    assert "timeout-minutes: 5" in step
    assert (
        r"run: .\scripts\windows-alpha\Invoke-K5StageOneRtspJoinedAnalyticsWitness.ps1 "
        "-Output $env:K5_STAGE_ONE_RTSP_JOINED_ANALYTICS_OUTPUT"
    ) in step
    assert text.count("Invoke-K5StageOneRtspJoinedAnalyticsWitness.ps1") == 1
    assert "secrets." not in step
    assert "K5_STAGE03_SOURCE" not in step
    assert "continue-on-error" not in step


def test_launcher_preserves_opt_in_and_cleanup_then_reuses_strict_validation() -> None:
    text = LAUNCHER.read_text(encoding="utf-8")
    invocation = (
        "python -m pytest tests/integration/test_stage_one_rtsp_joined_analytics_physical.py "
        "-q --no-cov --tb=no --show-capture=no"
    )
    assert text.count(invocation) == 1
    assert '$env:K5_STAGE_ONE_RTSP_JOINED_ANALYTICS_PHYSICAL = "1"' in text
    assert "$env:K5_STAGE_ONE_RTSP_JOINED_ANALYTICS_OUTPUT = $outputPath" in text
    assert text.index("Remove-Item -LiteralPath $outputPath -Force") < text.index(invocation)
    assert text.index(invocation) < text.index("python scripts/validate_stage_one_rtsp_witness.py")
    assert text.count("if ($LASTEXITCODE -ne 0)") == 2
    assert text.count("if (-not (Test-Path -LiteralPath $outputPath))") == 1
    for required in (
        "K5_ANALYTICS_EVIDENCE_ROOT",
        "K5_GSTREAMER_ROOT",
        "ANALYTICS_LAB_SHA",
        "K5_STAGE_ONE_REVISION",
    ):
        assert f'"{required}"' in text
    for forbidden in ("Get-Content", "ConvertFrom-Json", "[int64]", "receipt: $outputPath"):
        assert forbidden not in text
    assert (
        'Write-Host "RTSP-joined Stage One witness passed; source-free receipt validated."' in text
    )


def test_fourth_receipt_is_required_strictly_validated_and_explicitly_published() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert (
        "K5_STAGE_ONE_RTSP_JOINED_ANALYTICS_OUTPUT: "
        r"artifacts\stage-one-rtsp-joined-analytics-physical.json"
    ) in text
    validation = _step("Assert retained witness is source-free")
    assert "$env:K5_STAGE_ONE_RTSP_JOINED_ANALYTICS_OUTPUT" not in validation
    assert "python scripts/validate_stage_one_rtsp_witness.py" in validation
    assert validation.index("python scripts/validate_stage_one_rtsp_witness.py") < validation.index(
        "Get-Content -LiteralPath $output -Raw"
    )
    assert "timeout-minutes: 1" in validation
    assert "if ($LASTEXITCODE -ne 0)" in validation
    upload = _step("Upload source-free Stage One witness")
    assert "if: success() && steps.safe_evidence.outcome == 'success'" in upload
    assert "            artifacts/stage-one-rtsp-joined-analytics-physical.json\n" in upload
    assert "if-no-files-found: error" in upload
    assert "*" not in upload
    assert upload.count("            artifacts/") == 4


def test_admission_binds_exact_host_and_current_worker_ancestry() -> None:
    text = ADMISSION.read_text(encoding="utf-8")
    assert '[Environment]::MachineName -cne "VLR-CYZ4PK3"' in text
    assert '[string]$env:RUNNER_NAME -cne "K5-Physical"' in text
    assert "-ClassName Win32_Process -OperationTimeoutSec 10 -ErrorAction Stop" in text
    assert '"Runner.Worker.exe"' in text
    assert "$workers.Count -ne 1" in text
    assert "$currentId = [int]$PID" in text
    assert "$depth -lt 32" in text
    assert "$seen.ContainsKey($currentId)" in text
    assert "$parent.CreationDate -gt $current.CreationDate" in text
    assert "if (-not $owned)" in text


def test_admission_refuses_conflicting_runtimes_and_listeners_without_mutation() -> None:
    text = ADMISSION.read_text(encoding="utf-8")
    for token in (
        "EdgeVMS-Gate001",
        "edgevms-deviceops",
        "mediamtx",
        r"gst-launch-1\.0",
        "ffmpeg",
        "python",
        "pythonw",
        "k5-vision",
        "Get-NetTCPConnection -State Listen -ErrorAction Stop",
        "Get-NetUDPEndpoint -ErrorAction Stop",
        "@(8000, 8554, 8780, 8781, 8888, 8889, 9996, 9997, 9998)",
        "$_.LocalPort -eq 8189",
    ):
        assert token in text
    for forbidden in (
        "Stop-Process",
        "taskkill",
        "Remove-Item",
        "Set-Item",
        "New-Item",
        "Start-Process",
        "CreateSubKey",
        "DeleteSubKey",
        "SilentlyContinue",
    ):
        assert forbidden not in text
    assert "not a cross-repository lock" in text
    assert "Existing owners were preserved" in text
    assert "$_.CommandLine" not in text
    assert "Write-Output $processes" not in text
