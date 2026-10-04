"""Exact trusted-branch admission for the installed normal-app native witness."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/installed-analytics-candidate.yml"
BASELINE = ROOT / ".github/workflows/stage-one-operator-physical.yml"
BRANCH = "feat/installed-analytics-operator-20261003"


def test_candidate_route_is_one_job_on_only_exact_trusted_feature_branch():
    text = WORKFLOW.read_text()
    events = text.split("on:\n", 1)[1].split("concurrency:", 1)[0]
    assert f"branches:\n      - {BRANCH}\n" in events
    assert "pull_request" not in events
    assert "workflow_dispatch" not in text
    assert "workflow_call" not in text
    assert "      - main\n" not in events
    assert "github.repository == 'mkurtgerald/K5-Vision'" in text
    assert f"github.ref == 'refs/heads/{BRANCH}'" in text
    assert text.count("runs-on:") == 1
    assert "runs-on: [self-hosted, Windows, X64, k5-physical, camera-lab]" in text
    assert "timeout-minutes: 35" in text


def test_candidate_and_main_share_non_cancelling_lane_without_new_main_trigger():
    candidate, baseline = WORKFLOW.read_text(), BASELINE.read_text()
    for text in (candidate, baseline):
        assert "group: stage-one-operator-physical\n  cancel-in-progress: false" in text
    assert "branches:\n      - main\n  workflow_dispatch:\n" in baseline
    assert "if: github.ref == 'refs/heads/main'" in baseline
    assert baseline.count("runs-on:") == 1
    assert "Invoke-K5InstalledAnalyticsWitness" not in baseline


def test_candidate_has_read_only_credentials_exact_checkouts_and_fresh_head_checks():
    text = WORKFLOW.read_text()
    assert "permissions:\n  contents: read\n  actions: read\n\njobs:" in text
    for forbidden in (
        "secrets.",
        "CAM_CRED",
        "K5_STAGE03_SOURCE",
        "write-all",
        "pull_request_target",
    ):
        assert forbidden not in text
    assert "ref: ${{ github.sha }}\n          persist-credentials: false" in text
    assert "ref: ${{ env.ANALYTICS_LAB_SHA }}" in text
    assert "ANALYTICS_LAB_SHA: c8b347ae538991a0c0ce38eabc2dc17b566531d3" in text
    assert text.count("persist-credentials: false") == 2
    assert text.count("$head.commit.sha -cne $env:K5_EXPECTED_SHA") == 3
    assert text.count(f'[uri]::EscapeDataString("{BRANCH}")') == 3
    first, provisioning, media = text.split("$head.commit.sha -cne $env:K5_EXPECTED_SHA")[:3]
    assert "Checkout immutable candidate" not in first
    assert "Checkout immutable candidate" in provisioning
    assert "Preserve installed Alpha baseline" in media
    assert "Prove installed normal-app analytics" not in media
    for action in re.findall(r"uses: (\S+)", text):
        assert re.fullmatch(r"actions/[a-z-]+@[0-9a-f]{40}", action)


def test_candidate_keeps_ownership_admission_baseline_and_separate_installed_proof():
    text = WORKFLOW.read_text()
    assert text.count("run: .\\scripts\\assert-stage-one-physical-admission.ps1") == 3
    ordered = [
        "Require idle exact physical host before provisioning",
        "Provision existing reviewed GStreamer runtime",
        "Prepare existing bounded rights-reviewed fixture",
        "Preserve installed Alpha baseline",
        "Recheck fresh exact candidate before installed media acceptance",
        "Recheck idle host before installed normal-app witness",
        "Prove installed normal-app analytics",
        "Verify owned media cleanup",
        "Validate exact source-free installed receipt",
    ]
    assert [text.index(label) for label in ordered] == sorted(
        text.index(label) for label in ordered
    )
    assert "Invoke-K5VisionAlphaWitness.ps1 -Output $env:K5_ALPHA_DIRECT_RTSP_OUTPUT" in text
    assert (
        "Invoke-K5InstalledAnalyticsWitness.ps1 -Output $env:K5_INSTALLED_ANALYTICS_OUTPUT" in text
    )
    assert "-Revision $env:K5_STAGE_ONE_REVISION" in text
    assert "test_stage_one_operator_physical.py" not in text
    assert "test_stage_one_rtsp_joined_analytics_physical.py" not in text


def test_controller_and_seed_are_isolated_and_only_owned_temp_data_is_removed():
    text = WORKFLOW.read_text()
    assert '"k5-installed-controller-" + $env:GITHUB_RUN_ID + "-" + $env:GITHUB_RUN_ATTEMPT' in text
    assert '"k5-installed-evidence-" + $env:GITHUB_RUN_ID + "-" + $env:GITHUB_RUN_ATTEMPT' in text
    assert '"PIP_REQUIRE_VIRTUALENV=true"' in text
    assert "site.ENABLE_USER_SITE is False" in text
    assert "PYTHONPATH: ${{ github.workspace }}\\analytics-lab" in text
    assert "python -B -m analytics_lab.validation_seed" in text
    assert "New-Item -ItemType Directory -Path $controller -ErrorAction Stop" in text
    assert "New-Item -ItemType Directory -Path $evidence -ErrorAction Stop" in text
    assert "New-Item -ItemType Directory -Path $work -ErrorAction Stop" in text
    assert '"K5_INSTALLED_WORK_OWNED=$work"' in text
    assert "-WorkRoot $work" in text
    assert text.index('"K5_INSTALLED_WORK_OWNED=$work"') < text.index("-WorkRoot $work")
    cleanup = text.split("- name: Remove only this job's disposable fixture and controller", 1)[1]
    assert "if: always()" in cleanup
    for name in ("WORK", "EVIDENCE", "CONTROLLER"):
        assert f'K5_INSTALLED_{name}_OWNED: ""' in text
    assert "[IO.Path]::GetFullPath($owned) -cne [IO.Path]::GetFullPath($expected)" in cleanup
    assert "catch { $cleanupComplete = $false }" in cleanup
    assert (
        '@("K5_INSTALLED_WORK_OWNED", "K5_INSTALLED_EVIDENCE_OWNED", '
        '"K5_INSTALLED_CONTROLLER_OWNED")' in cleanup
    )
    assert "Remove-Item -LiteralPath $owned -Recurse -Force -ErrorAction Stop" in cleanup
    assert "Stop-Process" not in text and "taskkill" not in text


def test_only_strictly_validated_scalar_receipt_is_uploaded():
    text = WORKFLOW.read_text()
    assert "--validate-receipt --output $env:K5_INSTALLED_ANALYTICS_OUTPUT" in text
    assert "if: success() && steps.safe_evidence.outcome == 'success'" in text
    assert "name: installed-analytics-candidate-witness" in text
    assert "path: artifacts/installed-analytics-candidate.json" in text
    upload = text.split("- name: Upload only validated installed normal-app receipt", 1)[1]
    for forbidden in ("logs", "media", "evidence-root", "alpha-direct", "*.json", "**"):
        assert forbidden not in upload
    assert "retention-days: 14" in upload
    assert "if-no-files-found: error" in upload


def test_required_hosted_gates_match_audited_runtime_pr_paths_and_exact_sha():
    text = WORKFLOW.read_text()
    assert '"355859781" = "CI"' in text
    assert '"369354936" = "Windows Alpha Script Smoke"' in text
    assert '"362514400" = "Current Gate Viewport Editor Qualification"' in text
    assert '"364801702" = "Stage One Analytics Compatibility"' in text
    assert '"361865932" = "PR Run Dedupe"' in text
    required = text.split("$required = @{", 1)[1].split("\n          }", 1)[0]
    assert len(re.findall(r'"[0-9]+" = ', required)) == 5
    assert "Installed Analytics Candidate" not in required
    for name in (
        "windows-alpha-script-smoke.yml",
        "current-gate-viewport-editor.yml",
        "stage-one-analytics-compat.yml",
    ):
        trigger = (
            (ROOT / ".github/workflows" / name)
            .read_text()
            .split("pull_request:", 1)[1]
            .split("  push:", 1)[0]
        )
        assert "src/k5vision/operator_runtime.py" in trigger
    assert "event=pull_request&head_sha=$env:K5_EXPECTED_SHA" in text
    assert "$_.head_sha -ceq $env:K5_EXPECTED_SHA" in text
    assert '$_.event -ceq "pull_request"' in text
    assert "$_.name -ceq $required[$id]" in text
    assert "$_." + 'conclusion -cne "success"' in text
    assert "$deadline = [DateTime]::UtcNow.AddMinutes(6)" in text
    assert "timeout-minutes: 7" in text
    assert text.index("if (-not $qualified") < text.index("Checkout immutable candidate")
