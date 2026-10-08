"""Exact trusted-branch admission for the installed normal-app native witness."""

import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest


def canonical_checkout_bytes(raw):
    """Match the hosted source reader's sole allowed checkout transformation."""
    text = raw.decode("utf-8", errors="strict").replace("\r\n", "\n")
    if "\r" in text:
        raise ValueError("source_encoding")
    return text.encode("utf-8", errors="strict")


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/installed-analytics-candidate.yml"
BASELINE = ROOT / ".github/workflows/stage-one-operator-physical.yml"
BRANCH = "feat/installed-analytics-operator-20261003"
LAUNCHER_BRANCH = "feat/alpha-analytics-preflight-20261004"
UPGRADE_BRANCH = "fix/transactional-alpha-upgrade-20261003"
FACADE_BRANCH = "feat/installed-alpha-facade-witness-20261005"


def _job(name):
    text = WORKFLOW.read_text()
    marker = f"  {name}:\n"
    assert marker in text
    return re.split(r"(?m)^  [a-z][a-z0-9_-]*:\n", text.split(marker, 1)[1], maxsplit=1)[0]


def test_hosted_admission_precedes_exactly_one_physical_job():
    text = WORKFLOW.read_text()
    jobs = text.split("\njobs:\n", 1)[1]
    assert re.findall(r"(?m)^  ([a-z][a-z0-9_-]*):$", jobs) == [
        "hosted_admission",
        "installed-analytics-candidate",
    ]
    hosted = _job("hosted_admission")
    native = _job("installed-analytics-candidate")
    assert "    runs-on: windows-latest\n" in hosted
    assert "    timeout-minutes: 18\n" in hosted
    assert hosted.count("      - name: ") == 1
    assert "        timeout-minutes: 17\n" in hosted
    assert "    needs: hosted_admission\n" in native
    assert "needs.hosted_admission.result == 'success'" in native
    assert "needs.hosted_admission.outputs.qualified_sha == github.sha" in native
    assert "    timeout-minutes: 25\n" in native
    assert "runs-on: [self-hosted, Windows, X64, k5-physical, camera-lab]" in native
    assert "concurrency:" not in text.split("\njobs:\n", 1)[0]
    assert "concurrency:" not in hosted
    assert "    concurrency:\n      group: stage-one-operator-physical\n" in native
    assert "      cancel-in-progress: false\n" in native


def test_candidate_route_is_two_jobs_on_only_four_exact_trusted_branches():
    text = WORKFLOW.read_text()
    events = text.split("on:\n", 1)[1].split("\npermissions:", 1)[0]
    branches = events.split("    branches:\n", 1)[1].split("    paths:\n", 1)[0]
    assert branches == (
        f"      - {BRANCH}\n      - {LAUNCHER_BRANCH}\n"
        f"      - {UPGRADE_BRANCH}\n      - {FACADE_BRANCH}\n"
    )
    assert "pull_request" not in events
    assert "workflow_dispatch" not in text
    assert "workflow_call" not in text
    assert "      - main\n" not in events
    assert "github.repository == 'mkurtgerald/K5-Vision'" in text
    assert f"github.ref == 'refs/heads/{BRANCH}'" in text
    assert f"github.ref == 'refs/heads/{LAUNCHER_BRANCH}'" in text
    assert f"github.ref == 'refs/heads/{UPGRADE_BRANCH}'" in text
    assert f"github.ref == 'refs/heads/{FACADE_BRANCH}'" in text
    assert text.count("runs-on:") == 2
    assert text.count("runs-on: [self-hosted, Windows, X64, k5-physical, camera-lab]") == 1
    assert "    timeout-minutes: 25\n" in _job("installed-analytics-candidate")


def test_hosted_wait_covers_the_existing_smoke_bound_with_nested_hard_limits():
    hosted = _job("hosted_admission")
    smoke = (ROOT / ".github/workflows/windows-alpha-script-smoke.yml").read_text()
    smoke_minutes = int(re.search(r"    timeout-minutes: (\d+)", smoke).group(1))
    poll_minutes = int(
        re.search(r"\$deadline = \[DateTime\]::UtcNow.AddMinutes\((\d+)\)", hosted)[1]
    )
    step_minutes = int(re.search(r"        timeout-minutes: (\d+)", hosted)[1])
    job_minutes = int(re.search(r"    timeout-minutes: (\d+)", hosted)[1])
    assert (smoke_minutes, poll_minutes, step_minutes, job_minutes) == (15, 16, 17, 18)
    assert smoke_minutes + 1 == poll_minutes == step_minutes - 1 == job_minutes - 2
    assert "    timeout-minutes: 25\n" in _job("installed-analytics-candidate")
    assert "Start-Sleep -Seconds 15" in hosted
    assert "[DateTime]::UtcNow -ge $deadline" in hosted
    assert (
        'throw "An exact-candidate hosted qualification failed; native execution refused."'
        in hosted
    )


def test_launcher_upgrade_and_facade_branches_require_their_three_applicable_hosted_gates():
    gate = _run_script(_step("Require exact trusted head and green hosted PR qualification"))
    assert f'$legacyBranch = "{BRANCH}"' in gate
    assert f'$launcherBranch = "{LAUNCHER_BRANCH}"' in gate
    assert f'$upgradeBranch = "{UPGRADE_BRANCH}"' in gate
    assert f'$facadeBranch = "{FACADE_BRANCH}"' in gate
    assert (
        "$env:K5_CANDIDATE_BRANCH -cnotin "
        "@($legacyBranch, $launcherBranch, $upgradeBranch, $facadeBranch)" in gate
    )
    base = re.search(r"(?ms)\$required = @\{\n(.*?)^\s*\}", gate).group(1)
    assert set(re.findall(r'"([0-9]+)" = ', base)) == {"355859781", "369354936", "361865932"}
    assert "$env:K5_CANDIDATE_BRANCH -ceq $legacyBranch" in gate
    assert '$required["362514400"] = "Current Gate Viewport Editor Qualification"' in gate
    assert '$required["364801702"] = "Stage One Analytics Compatibility"' in gate
    assert "$_.head_branch -ceq $env:K5_CANDIDATE_BRANCH" in gate


def test_native_probe_and_host_admission_dependencies_are_exact_and_hosted_qualified():
    dependencies = {
        "tests/test_windows_alpha_analytics.py",
        "scripts/assert-installed-git-alias.ps1",
        "scripts/admit_installed_git_alias.cs",
        "tests/test_installed_git_alias_admission.py",
        "scripts/assert-stage-one-physical-admission.ps1",
        "tests/test_stage_one_physical_admission_diagnostics.py",
    }
    paths = WORKFLOW.read_text().split("    paths:\n", 1)[1].split("\n\n", 1)[0] + "\n"
    expected = {
        ".github/workflows/installed-analytics-candidate.yml",
        "scripts/installed_analytics_witness.py",
        "scripts/installed_alpha_launcher_witness.py",
        "scripts/installed_alpha_facade_witness.py",
        "scripts/windows_owned_preflight.py",
        "scripts/capture_installer_wheel_provenance.py",
        "scripts/installer_wheel_requirements.py",
        "scripts/installer_wheel_storage.py",
        "scripts/windows-alpha/Install-K5VisionAlpha.ps1",
        "scripts/windows-alpha/install_transaction.py",
        "tests/test_capture_installer_wheel_provenance.py",
        "tests/test_installer_wheel_requirements.py",
        "tests/test_installer_wheel_storage.py",
        "tests/test_installer_runtime_platform_dependencies.py",
        "tests/test_windows_alpha_upgrade.py",
        "tests/test_windows_alpha_bootstrap.py",
        "scripts/build_analytics_runtime_wheel.py",
        "scripts/windows-alpha/Invoke-K5InstalledAnalyticsWitness.ps1",
        "scripts/windows-alpha/Start-K5VisionAlpha.ps1",
        "scripts/windows-alpha/Test-K5VisionAlpha.ps1",
        "scripts/windows-alpha/Run-K5VisionAlpha.ps1",
        "scripts/windows-alpha/Invoke-K5VisionAlphaWitness.ps1",
        "src/k5vision/**",
        "pyproject.toml",
        "scripts/windows-alpha/runtime-requirements.txt",
        "tests/test_installed_analytics_witness.py",
        "tests/test_installed_alpha_launcher_witness.py",
        "tests/test_installed_alpha_facade_witness.py",
        "tests/test_windows_alpha_facade_witness.py",
        "tests/test_installed_analytics_candidate_workflow.py",
        *dependencies,
    }
    assert set(paths.splitlines()) == {"      - " + path for path in expected}
    assert len(paths.splitlines()) == len(expected)
    hosted = (
        (ROOT / ".github/workflows/windows-alpha-script-smoke.yml")
        .read_text()
        .split("  pull_request:\n", 1)[1]
        .split("  push:\n", 1)[0]
    )
    for dependency in dependencies:
        assert hosted.count('      - "' + dependency + '"\n') == 1


def test_candidate_and_main_share_non_cancelling_lane_without_new_main_trigger():
    candidate, baseline = WORKFLOW.read_text(), BASELINE.read_text()
    assert "group: stage-one-operator-physical\n  cancel-in-progress: false" in baseline
    native = _job("installed-analytics-candidate")
    assert "group: stage-one-operator-physical\n      cancel-in-progress: false" in native
    assert "\nconcurrency:" not in candidate
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
    assert text.count("$head.commit.sha -cne $env:K5_EXPECTED_SHA") == 8
    assert text.count("[uri]::EscapeDataString($env:K5_CANDIDATE_BRANCH)") == 8
    hosted, first, provisioning, baseline, media = text.split(
        "$head.commit.sha -cne $env:K5_EXPECTED_SHA"
    )[:5]
    assert "Checkout immutable candidate" not in hosted
    assert "Checkout immutable candidate" not in first
    assert "Checkout immutable candidate" in provisioning
    assert "Prepare existing bounded rights-reviewed fixture" in baseline
    assert "Preserve installed Alpha baseline" in media
    assert "Prove installed normal-app analytics" not in media
    for action in re.findall(r"uses: (\S+)", text):
        assert re.fullmatch(r"actions/[a-z-]+@[0-9a-f]{40}", action)


def test_candidate_keeps_ownership_admission_baseline_and_separate_installed_proof():
    text = WORKFLOW.read_text()
    assert text.count("run: .\\scripts\\assert-stage-one-physical-admission.ps1") == 8
    initial = _run_script(_step("Require idle exact physical host before provisioning"))
    assert "& $script -DiagnosticContext $context" in initial
    assert "Read-K5GitObject" in initial
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
        "Prepare independently bound offline Start-script inputs",
        "Recheck fresh exact candidate before installed Start-script acceptance",
        "Recheck idle host before installed Start-script witness",
        "Prove installed Start-script refusal and two analytics launches",
        "Verify installed Start-script owned media cleanup",
        "Validate exact source-free installed Start-script receipt",
        "Prepare independently bound offline facade inputs",
        "Recheck fresh exact candidate before installed facade acceptance",
        "Recheck idle host before installed facade witness",
        "Prove actual installed Test and two Run facades",
        "Verify installed facade owned media cleanup",
        "Validate exact source-free installed facade receipt",
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
        '"K5_INSTALLED_CONTROLLER_OWNED", "K5_ALPHA_LAUNCHER_OWNED", "K5_ALPHA_FACADE_OWNED")'
        in cleanup
    )
    assert "Remove-Item -LiteralPath $owned -Recurse -Force -ErrorAction Stop" in cleanup
    assert "Stop-Process" not in text and "taskkill" not in text


def test_only_strictly_validated_scalar_receipt_is_uploaded():
    text = WORKFLOW.read_text()
    assert "--validate-receipt --output $env:K5_INSTALLED_ANALYTICS_OUTPUT" in text
    assert "if: success() && steps.safe_evidence.outcome == 'success'" in text
    assert "name: installed-analytics-candidate-witness" in text
    assert "path: artifacts/installed-analytics-candidate.json" in text
    upload = text.split("- name: Upload only validated installed normal-app receipt", 1)[1].split(
        "- name: ", 1
    )[0]
    for forbidden in ("logs", "media", "evidence-root", "alpha-direct", "*.json", "**"):
        assert forbidden not in upload
    assert "retention-days: 14" in upload
    assert "if-no-files-found: error" in upload


def test_required_hosted_gates_match_audited_runtime_pr_paths_and_exact_sha():
    text = WORKFLOW.read_text()
    assert '"355859781" = "CI"' in text
    assert '"369354936" = "Windows Alpha Script Smoke"' in text
    assert '$required["362514400"] = "Current Gate Viewport Editor Qualification"' in text
    assert '$required["364801702"] = "Stage One Analytics Compatibility"' in text
    assert '"361865932" = "PR Run Dedupe"' in text
    required = re.search(r"(?ms)\$required = @\{\n(.*?)^\s*\}", text).group(1)
    assert len(re.findall(r'"[0-9]+" = ', required)) == 3
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
    assert "$deadline = [DateTime]::UtcNow.AddMinutes(16)" in _job("hosted_admission")
    assert "        timeout-minutes: 17\n" in _job("hosted_admission")
    assert text.index("if (-not $qualified") < text.index("Checkout immutable candidate")


def test_existing_git_is_admitted_after_hosted_gate_before_either_checkout():
    text = WORKFLOW.read_text()
    admission = _run_script(_step("Admit existing Git before immutable checkouts"))
    assert text.index("if (-not $qualified") < text.index("Admit existing Git")
    assert text.index("Admit existing Git") < text.index("Checkout immutable candidate")
    assert text.index("Admit existing Git") < text.index("Checkout immutable Analytics source")
    assert "timeout-minutes: 3" in _step("Admit existing Git before immutable checkouts")
    assert "$output = @(& $ownedGitText $AdmittedGitPath '--version')" in admission
    assert admission.index("$invokeGit = &") < admission.index("$output = @(&")
    assert "$output.Count -ne 1" in admission
    assert "$process.ExitCode -ne 0" in admission
    assert r"\.windows\.[0-9]+\z" in admission
    assert "$version -lt [Version]'2.18.0'" in admission
    assert "$text -cne 'git version 2.55.0.windows.5'" in admission
    assert "78211c7ed73988da93a6d8a33d47ec6187f464d7ea2a9a00c182bbd7a1ecf30f" in admission
    assert 'Write-Host "K5_CHECKOUT_GIT_SHA256=$gitSha256"' in admission
    assert admission.index("$version -lt") < admission.index("$env:GITHUB_PATH")
    assert admission.rstrip().endswith(
        "& $invokeGit -Operation { param($AdmittedGitPath) } | Out-Null"
    )
    assert "not a continuous lease" in admission
    for forbidden in (
        "Invoke-WebRequest",
        "Invoke-RestMethod",
        "SetEnvironmentVariable",
        "Set-ItemProperty",
        "Set-ExecutionPolicy",
        "Remove-Item",
        "Start-Process",
    ):
        assert forbidden not in admission


def test_git_backed_exact_revisions_are_required_before_provisioning():
    text = WORKFLOW.read_text()
    verification = text.split("- name: Require exact Git-backed source checkouts", 1)[1]
    verification = verification.split("- name: Provision existing reviewed GStreamer runtime", 1)[0]
    assert text.index("Checkout immutable Analytics source") < text.index(
        "Require exact Git-backed"
    )
    assert text.index("Require exact Git-backed") < text.index(
        "Provision existing reviewed GStreamer"
    )
    assert "timeout-minutes: 3" in verification
    assert "Get-Command git -CommandType Application -ErrorAction Stop" in verification
    assert "-ine 'C:\\Program Files\\Git\\cmd\\git.exe'" in verification
    assert "$env:GITHUB_WORKSPACE = $env:K5_STAGE_ONE_REVISION" in verification
    assert "'analytics-lab') = $env:ANALYTICS_LAB_SHA" in verification
    assert "Join-Path $source '.git'" in verification
    assert "[IO.File]::GetAttributes($metadata)" in verification
    assert "[IO.FileAttributes]::Directory" in verification
    assert "[IO.FileAttributes]::ReparsePoint" in verification
    assert "@(& $ownedGitText $AdmittedGitPath" in verification
    assert '`"$source`" rev-parse HEAD' in verification
    assert "$actual.Count -ne 1" in verification
    assert "$process.ExitCode -ne 0" in verification
    assert "$actual[0] -cne $sources[$source]" in verification


def _step(name):
    block = WORKFLOW.read_text().split(f"      - name: {name}\n", 1)[1]
    return re.split(r"(?m)^(?:      - name: |  [a-z][a-z0-9_-]*:\n)", block, maxsplit=1)[0]


def _run_script(step):
    body = step.split("        run: |\n", 1)[1]
    lines = []
    for line in body.splitlines():
        if line and not line.startswith("          "):
            break
        lines.append(line[10:] if line else "")
    return "\n".join(lines).rstrip() + "\n"


def test_start_inputs_are_prepared_independently_before_offline_witness():
    preparation = _step("Prepare independently bound offline Start-script inputs")
    assert f"if: github.ref == 'refs/heads/{LAUNCHER_BRANCH}'" in preparation
    assert "K5_GITHUB_TOKEN: ${{ github.token }}" in preparation
    assert preparation.index("$head.commit.sha -cne") < preparation.index("New-Item")
    assert preparation.count("-c core.autocrlf=false -c core.eol=lf") == 2
    assert '--output=`"$root\\source.zip`" $env:K5_STAGE_ONE_REVISION' in preparation
    assert '--output=`"$root\\analytics.zip`" $env:ANALYTICS_LAB_SHA' in preparation
    code = preparation.split("@'\n", 1)[1].split("\n          '@", 1)[0]
    code = "\n".join(line[10:] for line in code.splitlines())
    compile(code, "candidate-launcher-inputs", "exec")
    for admission in (
        '"download", "--no-deps", "--only-binary=:all:"',
        'operation="download_dependencies"',
        "**common.RUNTIME_VERSIONS, **common.WINDOWS_RUNTIME_VERSIONS",
        'verify_artifact_set(evidence / "artifacts", OPENVINO_OMZ_2023_FP16)',
        "verify_seed_media(clip, sample)",
        "clip.is_relative_to(evidence)",
        'common.read_json(evidence / "validation-manifest.json") == _manifest(identities)',
        '"run_nonce": uuid.uuid4().hex',
        '"source_tree_sha256": common.digest(launcher.tree_manifest(source))',
        '"k5_payload_sha256": common.digest(launcher.source_payload(source))',
        '"native_cache_sha256": common.digest(launcher.native_manifest(',
        '"wheelhouse_sha256": common.digest(launcher.tree_manifest(',
        "launcher.validate_expectations(expected, installed=False)",
    ):
        assert admission in code
    assert "receipt" not in code
    assert "installed_alpha_launcher_witness.py" in code
    assert "-I -B -S - $root" in preparation


def test_base_python_is_bound_to_explicit_setup_output_and_reverified():
    setup = _step("Set up Python")
    controller = _step("Create isolated witness controller")
    assert "id: runtime_python" in setup
    assert "K5_SETUP_PYTHON: ${{ steps.runtime_python.outputs.python-path }}" in controller
    assert "[IO.File]::GetAttributes($base)" in controller
    assert "FileAttributes]::Directory -bor [IO.FileAttributes]::ReparsePoint" in controller
    assert "sys.prefix == sys.base_prefix" in controller
    assert "Get-FileHash -LiteralPath $base -Algorithm SHA256" in controller
    assert '"K5_WITNESS_BASE_PYTHON=$base"' in controller
    assert '"K5_WITNESS_BASE_PYTHON_SHA256=$baseHash"' in controller
    for name in (
        "Prepare independently bound offline Start-script inputs",
        "Prove installed Start-script refusal and two analytics launches",
    ):
        step = _step(name)
        assert "$base = $env:K5_WITNESS_BASE_PYTHON" in step
        assert "-cne $env:K5_WITNESS_BASE_PYTHON_SHA256" in step


def test_start_proof_remains_separate_from_person_clip_and_baseline():
    witness = _step("Prove installed Start-script refusal and two analytics launches")
    assert "id: alpha_launcher_witness" in witness
    assert f"if: github.ref == 'refs/heads/{LAUNCHER_BRANCH}'" in witness
    assert "-I -B -S" in witness
    assert "source\\scripts\\installed_alpha_launcher_witness.py" in witness
    for argument in (
        "--expectations",
        "--admitted-expectations",
        "--k5-wheel",
        "--wheelhouse",
        "--evidence-root",
        "--local-appdata",
        "--git",
        "--temp-root",
        "--work-root",
    ):
        assert argument in witness
    assert "--output $env:K5_ALPHA_LAUNCHER_OUTPUT" in witness
    assert '"$root\\work"' in witness
    assert "Invoke-K5InstalledAnalyticsWitness" not in witness
    assert "Invoke-K5VisionAlphaWitness" not in witness
    cleanup = _step("Verify installed Start-script owned media cleanup")
    assert "if: always() && (steps.alpha_launcher_witness.outcome == 'success'" in cleanup
    assert "assert-stage-one-physical-admission.ps1" in cleanup


def test_start_receipt_is_validated_against_prelaunch_expectations_before_upload():
    validation = _step("Validate exact source-free installed Start-script receipt")
    assert "id: safe_alpha_evidence" in validation
    assert "if: steps.alpha_launcher_witness.outcome == 'success'" in validation
    assert "--validate-receipt --expectations" in validation
    assert "installed-alpha-start-script-expectations.json" in validation
    assert "--output $env:K5_ALPHA_LAUNCHER_OUTPUT" in validation
    upload = _step("Upload only validated installed Start-script receipt")
    assert "if: success() && steps.safe_alpha_evidence.outcome == 'success'" in upload
    assert "name: installed-alpha-start-script-witness" in upload
    assert "path: artifacts/installed-alpha-start-script-witness.json" in upload
    assert "retention-days: 14" in upload
    for forbidden in ("expectations.json", "logs", "wheel", "media", "**", "*.json"):
        assert forbidden not in upload
    cleanup = _step("Remove only this job's disposable fixture and controller")
    assert 'K5_ALPHA_LAUNCHER_OWNED = "k5-alpha-launcher-"' in cleanup
    assert 'K5_ALPHA_LAUNCHER_OWNED: ""' in WORKFLOW.read_text()


def test_new_windows_cases_are_added_without_replacing_existing_selection():
    text = (ROOT / ".github/workflows/windows-alpha-script-smoke.yml").read_text()
    for path in (
        "scripts/installed_analytics_witness.py",
        "scripts/installed_alpha_launcher_witness.py",
        "tests/test_installed_analytics_witness.py",
        "tests/test_installed_alpha_launcher_witness.py",
        "tests/test_installed_alpha_facade_witness.py",
        "tests/test_windows_alpha_facade_witness.py",
        "tests/test_installed_analytics_candidate_workflow.py",
        ".github/workflows/installed-analytics-candidate.yml",
    ):
        assert text.count(f'      - "{path}"') == 2
    assert (
        "python -m pytest tests/test_installed_analytics_candidate_workflow.py --no-cov -q" in text
    )
    assert "K5_SETUP_PYTHON: ${{ steps.runtime_python.outputs.python-path }}" in text
    assert "$env:K5_WITNESS_BASE_PYTHON_SHA256 = (Get-FileHash" in text
    assert "windows_real_venv_child_is_owned" in text
    assert "test_windows_real_directory_guard_remembers_created_then_deleted_session" in text
    assert "tests/test_windows_alpha_analytics.py" in text
    assert "tests/test_app_resource_lifetime.py" in text
    assert "tests/test_cli.py" in text
    assert text.count("runs-on:") == 1
    assert "runs-on: windows-latest" in text
    assert "permissions:\n  contents: read" in text


@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell parser required")
def test_candidate_workflow_powershell_parses_without_execution(tmp_path):
    scripts = []
    for index, step in enumerate(WORKFLOW.read_text().split("      - name: ")[1:]):
        if "shell: powershell" not in step or "        run: |\n" not in step:
            continue
        script = tmp_path / f"candidate-step-{index}.ps1"
        script.write_text(_run_script(step), encoding="utf-8")
        scripts.append(script)
    assert len(scripts) >= 15
    literals = ",".join("'" + str(path).replace("'", "''") + "'" for path in scripts)
    command = (
        "$ErrorActionPreference='Stop'; "
        f"foreach ($path in @({literals})) {{ "
        "$tokens=$null; $errors=$null; "
        "[System.Management.Automation.Language.Parser]::ParseFile("
        "$path,[ref]$tokens,[ref]$errors) | Out-Null; "
        "if ($errors.Count -ne 0) { throw 'candidate workflow syntax rejected' } }; "
        "Write-Output 'K5_CANDIDATE_POWERSHELL_PARSE_PASS'; exit 0"
    )
    result = subprocess.run(
        [shutil.which("powershell"), "-NoProfile", "-NonInteractive", "-Command", command],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "K5_CANDIDATE_POWERSHELL_PARSE_PASS"
    assert result.stderr == ""


def test_input_preparation_exception_boundary_never_echoes_raw_error():
    step = _step("Prepare independently bound offline Start-script inputs")
    code = step.split("@'\n", 1)[1].split("\n          '@", 1)[0]
    code = "\n".join(line[10:] for line in code.splitlines())
    assert code.startswith("try:\n")
    handler = code.split("except BaseException as error:", 1)[1]
    assert "common.emit_diagnostic(error.diagnostic)" in handler
    assert 'print("K5_ALPHA_INPUT_PREPARATION_FAILED")' in handler
    assert "raise SystemExit(1)" in handler
    for forbidden in ("traceback", "print(error)", "str(error)", "repr(error)"):
        assert forbidden not in handler


def test_qualified_preflight_helper_changes_require_future_native_qualification():
    text = WORKFLOW.read_text()
    paths = text.split("    paths:\n", 1)[1].split("\n\n", 1)[0] + "\n"
    assert "      - scripts/windows_owned_preflight.py\n" in paths
    witness = (ROOT / "scripts/installed_alpha_launcher_witness.py").read_text()
    # The controller must bind the imported helper's exact bytes before Start.
    preparation = witness.split("def prepare(", 1)[1].split("def probe(", 1)[0]
    assert '"windows_owned_preflight.py"' in preparation
    assert "Path(__file__).with_name(name).read_bytes()" in preparation
    assert (ROOT / "scripts/windows_owned_preflight.py").is_file()
    assert text.count("runs-on:") == 2
    assert text.count("runs-on: [self-hosted,") == 1
    assert "group: stage-one-operator-physical\n      cancel-in-progress: false" in text


def _physical_guard_accepts(result="success", output="a" * 40, repository=None, ref=None):
    """Evaluate only the workflow's source-bound Boolean/string job admission subset."""
    header = _job("installed-analytics-candidate").split("    runs-on:", 1)[0]
    expression = header.split("    if:", 1)[1].strip()
    if expression.startswith(">-"):
        expression = " ".join(expression[2:].split())
    values = {
        "needs.hosted_admission.result": result,
        "needs.hosted_admission.outputs.qualified_sha": output,
        "github.repository": repository or "mkurtgerald/K5-Vision",
        "github.ref": ref or f"refs/heads/{UPGRADE_BRANCH}",
        "github.sha": "a" * 40,
    }
    for name, value in values.items():
        expression = expression.replace(name, repr(value))
    tree = ast.parse(expression.replace("&&", " and ").replace("||", " or "), mode="eval")
    assert all(
        isinstance(
            node, (ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.Compare, ast.Eq, ast.Constant)
        )
        for node in ast.walk(tree)
    ), "Job admission must remain an explicit Boolean/string identity check"
    return eval(compile(tree, "candidate-native-job-admission", "eval"), {"__builtins__": {}})


@pytest.mark.parametrize(
    "result", ["failure", "failed", "timed_out", "cancelled", "skipped", "", None]
)
@pytest.mark.parametrize("output", ["a" * 40, "", None, "b" * 40])
def test_non_successful_or_missing_hosted_admission_never_allocates_native(result, output):
    assert not _physical_guard_accepts(result=result, output=output)


@pytest.mark.parametrize("output", ["", None, "b" * 40, "a" * 39, "a" * 41, "malformed"])
def test_success_without_the_exact_hosted_output_never_allocates_native(output):
    assert not _physical_guard_accepts(output=output)


@pytest.mark.parametrize("branch", [BRANCH, LAUNCHER_BRANCH, UPGRADE_BRANCH, FACADE_BRANCH])
def test_exact_hosted_success_admits_only_each_trusted_candidate_branch(branch):
    assert _physical_guard_accepts(ref=f"refs/heads/{branch}")
    assert not _physical_guard_accepts(ref=f"refs/heads/{branch}", repository="fork/K5-Vision")
    assert not _physical_guard_accepts(ref=f"refs/tags/{branch}")
    assert not _physical_guard_accepts(ref=f"refs/heads/{branch}-unreviewed")
    assert not _physical_guard_accepts(ref="refs/heads/main")


def test_hosted_output_is_written_only_after_bounded_green_admission():
    hosted = _job("hosted_admission")
    gate = _step("Require exact trusted head and green hosted PR qualification")
    script = _run_script(gate)
    assert "    outputs:\n      qualified_sha: ${{ steps.qualify.outputs.qualified_sha }}" in hosted
    assert "        id: qualify\n" in gate
    assert "K5_EXPECTED_SHA: ${{ github.sha }}" in hosted
    assert "K5_CANDIDATE_BRANCH: ${{ github.ref_name }}" in hosted
    assert script.count("$env:GITHUB_OUTPUT") == 1
    assert '"qualified_sha=$env:K5_EXPECTED_SHA"' in script
    assert script.index("if (-not $qualified") < script.index("$env:GITHUB_OUTPUT")
    assert "$deadline = [DateTime]::UtcNow.AddMinutes(16)" in script
    assert "[DateTime]::UtcNow -ge $deadline" in script
    assert "while ([DateTime]::UtcNow -lt $deadline)" in script
    assert "Start-Sleep -Seconds 15" in script
    for forbidden in (
        "uses:",
        "actions/checkout",
        "pip ",
        "New-Item",
        "self-hosted",
        "GITHUB_ENV",
        "continue-on-error:",
        "always()",
    ):
        assert forbidden not in hosted


def test_native_entry_rechecks_the_same_gate_once_without_polling():
    hosted = _run_script(_step("Require exact trusted head and green hosted PR qualification"))
    native = _job("installed-analytics-candidate")
    entry = _step("Revalidate exact admitted candidate before native work")
    script = _run_script(entry)
    assert native.split("    steps:\n", 1)[1].startswith(
        "      - name: Revalidate exact admitted candidate before native work\n"
    )
    assert "        timeout-minutes: 1\n" in entry
    assert "        if:" not in entry
    assert "K5_ADMITTED_SHA: ${{ needs.hosted_admission.outputs.qualified_sha }}" in entry
    assert "$env:K5_ADMITTED_SHA -cne $env:K5_EXPECTED_SHA" in script
    function = r"(?ms)^function Test-K5CandidateHostedGates \{\n.*?^\}\n"
    hosted_function = re.search(function, hosted).group()
    assert re.search(function, script).group() == hosted_function
    assert script.count("Test-K5CandidateHostedGates") == 2  # Definition and one fresh call.
    assert script.count("Invoke-RestMethod") == 2  # One branch lookup and one gate snapshot.
    for forbidden in (
        "Start-Sleep",
        "$deadline",
        "while (",
        "$env:GITHUB_OUTPUT",
        "continue-on-error",
    ):
        assert forbidden not in script
    for identity in (
        '$env:GITHUB_REPOSITORY -cne "mkurtgerald/K5-Vision"',
        "$env:K5_EXPECTED_SHA -cnotmatch '^[0-9a-f]{40}$'",
        "$head.name -cne $env:K5_CANDIDATE_BRANCH",
        "$head.commit.sha -cne $env:K5_EXPECTED_SHA",
        '$_.repository.full_name -ceq "mkurtgerald/K5-Vision"',
        '$_.head_repository.full_name -ceq "mkurtgerald/K5-Vision"',
        "$_.head_branch -ceq $env:K5_CANDIDATE_BRANCH",
        "$_.head_sha -ceq $env:K5_EXPECTED_SHA",
        '$_.event -ceq "pull_request"',
        "$_.name -ceq $required[$id]",
        "$null -eq $response -or $response.workflow_runs -isnot [System.Array]",
    ):
        assert identity in hosted_function


def test_powershell_literal_extraction_stops_before_job_metadata():
    hosted = _job("hosted_admission")
    script = _run_script(hosted)
    assert script == _run_script(
        _step("Require exact trusted head and green hosted PR qualification")
    )
    assert "installed-analytics-candidate:" not in script
    assert "needs: hosted_admission" not in script
    assert "runs-on:" not in script
    synthetic = (
        "        run: |\n          Write-Output 'safe'\n\n"
        "  next-job:\n    runs-on: windows-latest\n"
    )
    assert _run_script(synthetic) == "Write-Output 'safe'\n"


def _mock_gate_cases(branch):
    """Small synthetic GitHub replies; none are fetched from the API."""
    import copy

    sha = "a" * 40
    required = {
        355859781: "CI",
        369354936: "Windows Alpha Script Smoke",
        361865932: "PR Run Dedupe",
    }
    if branch == BRANCH:
        required.update(
            {
                362514400: "Current Gate Viewport Editor Qualification",
                364801702: "Stage One Analytics Compatibility",
            }
        )
    runs = [
        {
            "id": index + 1,
            "workflow_id": workflow_id,
            "name": name,
            "head_sha": sha,
            "head_branch": branch,
            "event": "pull_request",
            "repository": {"full_name": "mkurtgerald/K5-Vision"},
            "head_repository": {"full_name": "mkurtgerald/K5-Vision"},
            "status": "completed",
            "conclusion": "success",
        }
        for index, (workflow_id, name) in enumerate(required.items())
    ]
    good = {
        "name": "exact-success",
        "accepted": True,
        "repository": "mkurtgerald/K5-Vision",
        "branch": branch,
        "sha": sha,
        "admitted_sha": sha,
        "head": {"name": branch, "commit": {"sha": sha}},
        "response": {"workflow_runs": runs},
        "api_failure": False,
    }
    cases = [good]

    def case(name):
        item = copy.deepcopy(good)
        item.update(name=name, accepted=False)
        cases.append(item)
        return item

    case("fork-event-repository")["repository"] = "fork/K5-Vision"
    case("unreviewed-branch")["branch"] = "main"
    for value in ("", "A" * 40, "a" * 39, "z" * 40):
        case("malformed-expected-sha-" + value)["sha"] = value
    case("branch-moved-after-hosted-admission")["head"]["commit"]["sha"] = "b" * 40
    case("wrong-returned-branch")["head"]["name"] = "main"
    case("missing-returned-branch")["head"].pop("name")
    case("missing-returned-sha")["head"]["commit"].pop("sha")
    case("missing-head-response")["head"] = None
    case("missing-runs-field")["response"] = {}
    case("missing-runs-response")["response"] = None
    case("malformed-runs-field")["response"]["workflow_runs"] = "success"
    case("object-instead-of-runs-array")["response"]["workflow_runs"] = runs[0]
    case("empty-runs")["response"]["workflow_runs"] = []
    case("api-failure")["api_failure"] = True
    for index in range(len(runs)):
        case(f"missing-required-gate-{index}")["response"]["workflow_runs"].pop(index)
        case(f"failed-required-gate-{index}")["response"]["workflow_runs"][index]["conclusion"] = (
            "failure"
        )
    for field, value in (
        ("workflow_id", 999),
        ("name", "Unrelated native gate"),
        ("event", "push"),
        ("head_branch", "main"),
        ("head_sha", "b" * 40),
        ("repository", {"full_name": "fork/K5-Vision"}),
        ("head_repository", {"full_name": "fork/K5-Vision"}),
        ("status", "queued"),
        ("status", "in_progress"),
        ("status", None),
        ("conclusion", "cancelled"),
        ("conclusion", "timed_out"),
        ("conclusion", "skipped"),
        ("conclusion", None),
    ):
        case(f"wrong-{field}-{value}")["response"]["workflow_runs"][0][field] = value
    for field in (
        "workflow_id",
        "name",
        "event",
        "head_branch",
        "head_sha",
        "repository",
        "head_repository",
        "status",
        "conclusion",
    ):
        case("missing-run-" + field)["response"]["workflow_runs"][0].pop(field)
    older_failure = case("older-exact-failure-cannot-be-hidden-by-newer-green")
    failed = copy.deepcopy(runs[0])
    failed.update(id=0, conclusion="failure")
    older_failure["response"]["workflow_runs"].append(failed)
    newer_pending = case("older-green-cannot-hide-newer-pending")
    pending = copy.deepcopy(runs[0])
    pending.update(id=100, status="in_progress", conclusion=None)
    newer_pending["response"]["workflow_runs"].append(pending)
    unrelated = case("unrelated-failure-does-not-replace-or-expand-applicable-gates")
    unrelated["accepted"] = True
    irrelevant = copy.deepcopy(runs[0])
    irrelevant.update(id=100, workflow_id=999, name="Unrelated native gate", conclusion="failure")
    unrelated["response"]["workflow_runs"].append(irrelevant)
    return cases


@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell required")
@pytest.mark.parametrize("branch", [BRANCH, LAUNCHER_BRANCH, UPGRADE_BRANCH, FACADE_BRANCH])
@pytest.mark.parametrize("hosted", [True, False], ids=["hosted-admission", "native-revalidation"])
def test_workflow_gate_source_refuses_untrusted_or_incomplete_mock_snapshots(
    tmp_path, branch, hosted
):
    """Execute the actual inline script with mocked network and sleep commands only."""
    name = (
        "Require exact trusted head and green hosted PR qualification"
        if hosted
        else "Revalidate exact admitted candidate before native work"
    )
    gate = tmp_path / "actual-workflow-gate.ps1"
    gate.write_text(_run_script(_step(name)), encoding="utf-8")
    cases = _mock_gate_cases(branch)
    if not hosted:
        for output in ("", "b" * 40, "A" * 40):
            item = dict(
                cases[0],
                name="wrong-admitted-output-" + output,
                admitted_sha=output,
                accepted=False,
            )
            cases.append(item)
    fixtures = tmp_path / "gate-fixtures.json"
    fixtures.write_text(json.dumps(cases), encoding="utf-8")
    harness = tmp_path / "mock-github.ps1"
    harness.write_text(
        r"""
param([string]$Fixtures, [string]$Gate, [string]$OutputRoot)
$ErrorActionPreference = "Stop"
function Invoke-RestMethod {
    param($Method, $Headers, $TimeoutSec, $Uri)
    $global:K5MockCalls++
    if ($Method -cne 'Get' -or $TimeoutSec -ne 20) { throw 'Unexpected request policy.' }
    if ($global:K5MockCurrent.api_failure) { throw 'Mock API unavailable.' }
    $branchUri = 'https://api.github.com/repos/mkurtgerald/K5-Vision/branches/' +
        [uri]::EscapeDataString($env:K5_CANDIDATE_BRANCH)
    $runsUri = 'https://api.github.com/repos/mkurtgerald/K5-Vision/actions/runs?' +
        'event=pull_request&head_sha=' + $env:K5_EXPECTED_SHA + '&per_page=100'
    if ($Uri -ceq $branchUri) { return $global:K5MockCurrent.head }
    if ($Uri -ceq $runsUri) { return $global:K5MockCurrent.response }
    throw 'Unexpected URI; no real network request is permitted.'
}
function Start-Sleep {
    param($Seconds)
    $global:K5MockSleeps++
    throw 'Mock pending snapshot ends without a real sleep.'
}
$results = @()
$index = 0
foreach ($scenario in (Get-Content -LiteralPath $Fixtures -Raw | ConvertFrom-Json)) {
    $global:K5MockCurrent = $scenario
    $global:K5MockCalls = 0
    $global:K5MockSleeps = 0
    $env:GITHUB_REPOSITORY = $scenario.repository
    $env:K5_CANDIDATE_BRANCH = $scenario.branch
    $env:K5_EXPECTED_SHA = $scenario.sha
    $env:K5_ADMITTED_SHA = $scenario.admitted_sha
    $env:K5_GITHUB_TOKEN = 'mock-only-no-credentials'
    $env:GITHUB_OUTPUT = Join-Path $OutputRoot ("output-" + $index + ".txt")
    $accepted = $false
    try { & $Gate; $accepted = $true } catch { $accepted = $false }
    $output = if (Test-Path -LiteralPath $env:GITHUB_OUTPUT) {
        (Get-Content -LiteralPath $env:GITHUB_OUTPUT -Raw).Trim()
    } else { '' }
    $results += [pscustomobject]@{
        name = $scenario.name; accepted = $accepted; output = $output
        calls = $global:K5MockCalls; sleeps = $global:K5MockSleeps
    }
    $index++
}
ConvertTo-Json -InputObject $results -Compress -Depth 10
""",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            shutil.which("powershell"),
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(harness),
            "-Fixtures",
            str(fixtures),
            "-Gate",
            str(gate),
            "-OutputRoot",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    observed = json.loads(result.stdout)
    assert len(observed) == len(cases)
    for expected, actual in zip(cases, observed, strict=True):
        assert actual["name"] == expected["name"]
        assert actual["accepted"] is expected["accepted"], actual
        assert actual["calls"] <= 2, actual
        if expected["accepted"]:
            assert actual["calls"] == 2 and actual["sleeps"] == 0, actual
        if not hosted:
            assert actual["sleeps"] == 0, actual
        assert actual["output"] == (
            "qualified_sha=" + "a" * 40 if hosted and expected["accepted"] else ""
        ), actual


def _inline_python(step):
    code = _run_script(_step(step)).split("@'\n", 1)[1].split("\n'@", 1)[0]
    compile(code, step, "exec")
    return code


def test_facade_inputs_are_distinct_prelaunch_authority_without_receipt_derivation():
    preparation = _step("Prepare independently bound offline facade inputs")
    assert f"if: github.ref == 'refs/heads/{FACADE_BRANCH}'" in preparation
    code = _inline_python("Prepare independently bound offline facade inputs")
    assert 'common.read_json(root / "inputs.json")' in code
    assert "launcher.validate_expectations(initial, installed=False)" in code
    assert '"run_nonce": uuid.uuid4().hex' in code
    assert 'expected["run_nonce"] != initial["run_nonce"]' in code
    assert '"facade_controller_sha256": common.file_hash(controller)' in code
    assert 'common.digest(launcher.tree_manifest(source)) == initial["source_tree_sha256"]' in code
    assert "facade.validate_expectations(expected, installed=False)" in code
    assert 'facade_root / "work"' in code
    assert 'with output.open("xb") as stream:' in code
    assert "K5_ALPHA_FACADE_INPUTS" in code
    assert "receipt" not in code
    assert "runtime_identity_sha256" not in code  # Independently probed by the new layout.
    assert "K5_ALPHA_FACADE_OWNED=$facade" in preparation
    assert "if (Test-Path -LiteralPath $facade)" in preparation
    assert "-cne $env:K5_WITNESS_BASE_PYTHON_SHA256" in preparation
    assert "-I -B -S - $root $facade" in preparation


def test_facade_invocation_uses_exact_cli_and_separate_owned_inputs():
    witness = _step("Prove actual installed Test and two Run facades")
    assert "id: alpha_facade_witness" in witness
    assert f"if: github.ref == 'refs/heads/{FACADE_BRANCH}'" in witness
    assert "source\\scripts\\installed_alpha_facade_witness.py" in witness
    assert "--expectations $env:K5_ALPHA_FACADE_INPUTS" in witness
    assert "--admitted-expectations $env:K5_ALPHA_FACADE_EXPECTATIONS" in witness
    assert "--output $env:K5_ALPHA_FACADE_OUTPUT" in witness
    assert '--temp-root $facade --work-root "$facade\\work"' in witness
    for argument in (
        "--repo",
        "--analytics-source",
        "--k5-wheel",
        "--wheelhouse",
        "--evidence-root",
        "--local-appdata",
        "--git",
    ):
        assert argument in witness
    assert "-I -B -S" in witness
    assert "-cne $env:K5_WITNESS_BASE_PYTHON_SHA256" in witness
    assert "$LASTEXITCODE -ne 0" in witness
    for forbidden in ("--probe", "pytest", "Install-K5VisionAlpha", "provenance", "storage", "pip"):
        assert forbidden not in witness


def test_facade_head_idle_cleanup_and_receipt_are_separate_fail_closed_steps():
    head = _step("Recheck fresh exact candidate before installed facade acceptance")
    assert f'$env:K5_CANDIDATE_BRANCH -cne "{FACADE_BRANCH}"' in head
    assert "$head.name -cne $env:K5_CANDIDATE_BRANCH" in head
    assert "$head.commit.sha -cne $env:K5_EXPECTED_SHA" in head
    idle = _step("Recheck idle host before installed facade witness")
    cleanup = _step("Verify installed facade owned media cleanup")
    for step in (idle, cleanup):
        assert "assert-stage-one-physical-admission.ps1" in step
        assert "continue-on-error" not in step
    assert "if: always() && (steps.alpha_facade_witness.outcome == 'success'" in cleanup
    validate = _step("Validate exact source-free installed facade receipt")
    assert "id: safe_facade_evidence" in validate
    assert "if: success() && steps.alpha_facade_witness.outcome == 'success'" in validate
    assert "--validate-receipt --expectations $env:K5_ALPHA_FACADE_EXPECTATIONS" in validate
    assert "--output $env:K5_ALPHA_FACADE_OUTPUT" in validate
    assert "$LASTEXITCODE -ne 0" in validate
    code = _inline_python("Validate exact source-free installed facade receipt")
    assert "facade.validate_expectations(initial, installed=False)" in code
    assert "facade.validate_expectations(admitted)" in code
    assert "all(admitted[key] == value for key, value in initial.items())" in code
    assert 'not (facade_root / "work").exists()' in code
    assert "facade.validate_receipt" not in code  # The actual read-only CLI handles the receipt.


def test_outer_cleanup_never_overrides_a_preserved_controller_work_root():
    cleanup = _run_script(_step("Remove only this job's disposable fixture and controller"))
    assert cleanup.index("$guardedRoots = @{") < cleanup.index("$prefixes = @{")
    guard = cleanup.split("$prefixes = @{", 1)[0]
    assert 'K5_ALPHA_FACADE_OWNED = "k5-alpha-facades-"' in guard
    assert 'K5_ALPHA_LAUNCHER_OWNED = "k5-alpha-launcher-"' in guard
    assert "$preserveOwnedRoots = $true" in guard
    assert '[IO.File]::GetAttributes((Join-Path $owned "work"))' in guard
    assert "catch [IO.FileNotFoundException]" in guard
    assert "catch [IO.DirectoryNotFoundException]" in guard
    assert "catch { $preserveOwnedRoots = $true }" in guard
    assert "if ($preserveOwnedRoots) { throw " in guard
    assert "Remove-Item" not in guard
    assert "Stop-Process" not in cleanup and "taskkill" not in cleanup


def test_facade_uploads_only_validated_hash_authority_and_receipt_after_owned_cleanup():
    text = WORKFLOW.read_text()
    receipt = _step("Upload only validated installed facade receipt")
    expectations = _step("Retain only validated source-free facade expectations")
    for step in (receipt, expectations):
        assert "if: success() && steps.safe_facade_evidence.outcome == 'success'" in step
        assert "retention-days: 14" in step
        assert "if-no-files-found: error" in step
        for forbidden in ("logs", "media", "source/", "**", "*.json", "inputs.json", "RUNNER_TEMP"):
            assert forbidden not in step
    assert "path: artifacts/installed-alpha-facades-witness.json" in receipt
    assert "path: artifacts/installed-alpha-facades-expectations.json" in expectations
    assert text.index("Remove only this job's disposable fixture and controller") < text.index(
        "Upload only validated installed facade receipt"
    )
    for forbidden in ("Set-Acl", "icacls"):
        assert forbidden not in text
    # Both reviewed routes share this workflow, but facade steps must never
    # acquire the upgrade route's retained-wheel or transactional responsibilities.
    facade_route = "\n".join(
        step for step in text.split("      - name: ")[1:] if "facade" in step.splitlines()[0]
    )
    for forbidden in (
        "fix/transactional-alpha-upgrade",
        "capture_installer",
        "installer_wheel_storage",
        "install_transaction",
    ):
        assert forbidden not in facade_route


def _route_guard_accepts(step_name, branch, normal="success", alpha="success"):
    """Evaluate actual branch/receipt conditions without invoking either route."""
    header = _step(step_name).split("        shell:", 1)[0]
    expression = header.split("        if: ", 1)[1].splitlines()[0]
    values = {
        "github.ref": f"refs/heads/{branch}",
        "steps.safe_evidence.outcome": normal,
        "steps.safe_alpha_evidence.outcome": alpha,
    }
    for name, value in values.items():
        expression = expression.replace(name, repr(value))
    tree = ast.parse(expression.replace("&&", " and ").replace("||", " or "), mode="eval")
    assert all(
        isinstance(
            node, (ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.Compare, ast.Eq, ast.Constant)
        )
        for node in ast.walk(tree)
    )
    return eval(compile(tree, "candidate-route-admission", "eval"), {"__builtins__": {}})


@pytest.mark.parametrize(
    "branch", [BRANCH, LAUNCHER_BRANCH, UPGRADE_BRANCH, FACADE_BRANCH, "main", "unreviewed"]
)
def test_reconciled_routes_preserve_exact_shared_and_exclusive_branch_selection(branch):
    for name in (
        "Prepare independently bound offline Start-script inputs",
        "Recheck fresh exact candidate before installed Start-script acceptance",
        "Recheck idle host before installed Start-script witness",
        "Prove installed Start-script refusal and two analytics launches",
    ):
        assert _route_guard_accepts(name, branch) is (
            branch in {LAUNCHER_BRANCH, UPGRADE_BRANCH, FACADE_BRANCH}
        )
    for name in (
        "Prepare independently bound offline facade inputs",
        "Recheck fresh exact candidate before installed facade acceptance",
        "Recheck idle host before installed facade witness",
        "Prove actual installed Test and two Run facades",
    ):
        assert _route_guard_accepts(name, branch) is (branch == FACADE_BRANCH)
    assert _route_guard_accepts("Retain exact qualified wheel provenance locally", branch) is (
        branch == UPGRADE_BRANCH
    )


@pytest.mark.parametrize("normal", ["success", "failure", "cancelled", "skipped", ""])
@pytest.mark.parametrize("alpha", ["success", "failure", "cancelled", "skipped", ""])
def test_reconciled_upgrade_capture_still_requires_both_successful_receipts(normal, alpha):
    assert _route_guard_accepts(
        "Retain exact qualified wheel provenance locally", UPGRADE_BRANCH, normal, alpha
    ) is (normal == alpha == "success")


@pytest.fixture
def inline_facade_authorities(tmp_path, monkeypatch):
    """Run the actual inline Python with unchanged pure validators and owned local files."""
    import importlib.util
    import sys
    from types import SimpleNamespace

    spec = importlib.util.spec_from_file_location(
        "inline_facade_contract", ROOT / "scripts/installed_alpha_facade_witness.py"
    )
    facade = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(facade)
    root, owned, evidence = tmp_path / "launcher", tmp_path / "facade", tmp_path / "evidence"
    source = root / "source"
    (source / "scripts").mkdir(parents=True)
    owned.mkdir()
    evidence.mkdir()
    controller = source / "scripts/installed_alpha_facade_witness.py"
    controller.write_bytes(b"# Synthetic raw exported controller identity fixture.\n")
    initial = dict.fromkeys(facade.alpha.INPUT_IDENTITIES, "a" * 64)
    initial.update(revision="b" * 40, run_nonce="c" * 32)
    initial["source_tree_sha256"] = facade.common.digest(facade.alpha.tree_manifest(source))
    (root / "inputs.json").write_bytes(facade.common.canonical(initial))
    inputs = evidence / "installed-alpha-facades-inputs.json"
    admitted = evidence / facade.EXPECTATIONS_NAME
    monkeypatch.setenv("K5_STAGE_ONE_REVISION", initial["revision"])
    monkeypatch.setenv("K5_ALPHA_FACADE_INPUTS", str(inputs))
    monkeypatch.setenv("K5_ALPHA_FACADE_EXPECTATIONS", str(admitted))
    monkeypatch.setattr(sys, "argv", ["-", str(root), str(owned)])
    loader = SimpleNamespace(exec_module=lambda module: None)
    monkeypatch.setattr(
        importlib.util, "spec_from_file_location", lambda *args: SimpleNamespace(loader=loader)
    )
    monkeypatch.setattr(importlib.util, "module_from_spec", lambda spec: facade)
    return SimpleNamespace(
        facade=facade,
        root=root,
        owned=owned,
        controller=controller,
        initial=initial,
        inputs=inputs,
        admitted=admitted,
    )


def test_inline_preparation_writes_hash_only_fresh_authority_without_runtime_or_success(
    inline_facade_authorities,
):
    env = inline_facade_authorities
    exec(_inline_python("Prepare independently bound offline facade inputs"), {})
    result = env.facade.common.read_json(env.inputs)
    env.facade.validate_expectations(result, installed=False)
    assert result["run_nonce"] != env.initial["run_nonce"]
    assert result["facade_controller_sha256"] == env.facade.common.file_hash(env.controller)
    assert all(result[key] == value for key, value in env.initial.items() if key != "run_nonce")
    assert (env.owned / "work").is_dir()
    assert not env.admitted.exists()
    assert "runtime_identity_sha256" not in result


@pytest.mark.parametrize(
    "fault",
    [
        "revision",
        "source-drift",
        "missing-authority",
        "unexpected-field",
        "stale-output",
        "reused-nonce",
    ],
)
def test_inline_preparation_refuses_stale_or_unbound_inputs(
    inline_facade_authorities, monkeypatch, fault
):
    import uuid

    env = inline_facade_authorities
    if fault == "revision":
        monkeypatch.setenv("K5_STAGE_ONE_REVISION", "d" * 40)
    elif fault == "source-drift":
        env.controller.write_bytes(b"modified")
    elif fault == "missing-authority":
        (env.root / "inputs.json").unlink()
    elif fault == "unexpected-field":
        (env.root / "inputs.json").write_text(json.dumps({**env.initial, "path": "forbidden"}))
    elif fault == "stale-output":
        env.inputs.write_bytes(b"previous run")
    elif fault == "reused-nonce":
        monkeypatch.setattr(uuid, "uuid4", lambda: uuid.UUID(env.initial["run_nonce"]))
    with pytest.raises(SystemExit) as error:
        exec(_inline_python("Prepare independently bound offline facade inputs"), {})
    assert error.value.code == 1
    assert not (env.owned / "work").exists()
    assert not env.admitted.exists()


@pytest.mark.parametrize(
    "fault",
    [
        "none",
        "nonce",
        "revision",
        "controller-drift",
        "runtime-missing",
        "runtime-zero",
        "extra-path",
        "work-retained",
        "missing-inputs",
    ],
)
def test_inline_receipt_authority_refuses_drift_or_preserved_work(
    inline_facade_authorities, monkeypatch, fault
):
    env = inline_facade_authorities
    exec(_inline_python("Prepare independently bound offline facade inputs"), {})
    initial = env.facade.common.read_json(env.inputs)
    admitted = {**initial, "runtime_identity_sha256": "e" * 64}
    if fault != "work-retained":
        (env.owned / "work").rmdir()
    if fault == "nonce":
        admitted["run_nonce"] = "f" * 32
    elif fault == "revision":
        monkeypatch.setenv("K5_STAGE_ONE_REVISION", "d" * 40)
    elif fault == "controller-drift":
        env.controller.write_bytes(b"modified")
    elif fault == "runtime-missing":
        admitted.pop("runtime_identity_sha256")
    elif fault == "runtime-zero":
        admitted["runtime_identity_sha256"] = "0" * 64
    elif fault == "extra-path":
        admitted["path"] = str(env.root)
    elif fault == "missing-inputs":
        env.inputs.unlink()
    env.admitted.write_text(json.dumps(admitted))
    code = _inline_python("Validate exact source-free installed facade receipt")
    if fault == "none":
        exec(code, {})
    else:
        with pytest.raises(SystemExit) as error:
            exec(code, {})
        assert error.value.code == 1


@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell required")
@pytest.mark.parametrize(
    "state", ["quiescent", "facade-retained", "start-retained", "both-retained", "wrong-owner"]
)
def test_actual_outer_cleanup_preserves_every_owned_root_when_a_controller_is_not_quiescent(
    tmp_path, state
):
    """No child is launched; retained work directories stand for a refused controller drain."""
    import os

    prefixes = {
        "K5_INSTALLED_WORK_OWNED": "k5-installed-work-",
        "K5_INSTALLED_EVIDENCE_OWNED": "k5-installed-evidence-",
        "K5_INSTALLED_CONTROLLER_OWNED": "k5-installed-controller-",
        "K5_ALPHA_LAUNCHER_OWNED": "k5-alpha-launcher-",
        "K5_ALPHA_FACADE_OWNED": "k5-alpha-facades-",
    }
    env = {
        **os.environ,
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_RUN_ID": "123",
        "GITHUB_RUN_ATTEMPT": "1",
    }
    roots = {}
    for name, prefix in prefixes.items():
        root = tmp_path / (prefix + "123-1")
        root.mkdir()
        (root / "sentinel.txt").write_text("preserve until controller drain is proved")
        env[name], roots[name] = str(root), root
    if state in {"facade-retained", "both-retained"}:
        (roots["K5_ALPHA_FACADE_OWNED"] / "work").mkdir()
    if state in {"start-retained", "both-retained"}:
        (roots["K5_ALPHA_LAUNCHER_OWNED"] / "work").mkdir()
    if state == "wrong-owner":
        env["K5_ALPHA_FACADE_OWNED"] += "-unowned"
    script = tmp_path / "actual-outer-cleanup.ps1"
    script.write_text(
        _run_script(_step("Remove only this job's disposable fixture and controller"))
    )
    result = subprocess.run(
        [shutil.which("powershell"), "-NoProfile", "-NonInteractive", "-File", str(script)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert (result.returncode == 0) is (state == "quiescent")
    for root in roots.values():
        if state == "quiescent":
            assert not root.exists()
        else:
            assert (root / "sentinel.txt").is_file()


@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell required")
@pytest.mark.parametrize(
    "case", ["exact", "advanced", "wrong-branch", "untrusted", "missing-head", "api-failure"]
)
def test_actual_facade_final_head_check_refuses_stale_admission(tmp_path, case):
    import os

    head = {"name": FACADE_BRANCH, "commit": {"sha": "a" * 40}}
    if case == "advanced":
        head["commit"]["sha"] = "b" * 40
    elif case == "wrong-branch":
        head["name"] = LAUNCHER_BRANCH
    elif case == "missing-head":
        head = None
    fixture = tmp_path / "head.json"
    fixture.write_text(json.dumps(head))
    script = tmp_path / "facade-head.ps1"
    script.write_text(
        """function Invoke-RestMethod {
      param($Method, $Headers, $TimeoutSec, $Uri)
      if ($env:MOCK_API_FAILURE -ceq '1') { throw 'mock request failure' }
      if ($Method -cne 'Get' -or $TimeoutSec -ne 20) { throw 'unexpected request' }
      return (Get-Content -LiteralPath $env:MOCK_HEAD -Raw | ConvertFrom-Json)
    }
"""
        + _run_script(_step("Recheck fresh exact candidate before installed facade acceptance"))
    )
    env = {
        **os.environ,
        "K5_CANDIDATE_BRANCH": "main" if case == "untrusted" else FACADE_BRANCH,
        "K5_EXPECTED_SHA": "a" * 40,
        "MOCK_HEAD": str(fixture),
        "MOCK_API_FAILURE": "1" if case == "api-failure" else "0",
    }
    result = subprocess.run(
        [shutil.which("powershell"), "-NoProfile", "-NonInteractive", "-File", str(script)],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert (result.returncode == 0) is (case == "exact")


def _early_storage_bootstrap():
    script = _run_script(_step("Observe read-only storage admission before provisioning"))
    return script.split("$bootstrap = @'\n", 1)[1].split("\n'@", 1)[0]


def _early_reader_namespace():
    source = _early_storage_bootstrap().split("\nrecord = dict(schema_version=", 1)[0]
    namespace = {}
    exec(compile(source, "<tested-storage-bootstrap>", "exec"), namespace)
    return namespace


def test_storage_source_failure_codes_are_fixed_and_match_safe_reprojection():
    namespace = _early_reader_namespace()
    script = _run_script(_step("Observe read-only storage admission before provisioning"))
    projected = re.search(r"source_binding = @\((.*?)\)", script, re.S).group(1)
    codes = set(re.findall(r"'([^']+)'", projected))
    assert codes == namespace["SOURCE_BINDING_CODES"] | {"source_cleanup_failed"}
    assert all(re.fullmatch(r"source_[a-z_]{1,40}", code) for code in codes)
    assert "error.code in SOURCE_BINDING_CODES" in script
    assert "str(error)" not in script and "repr(error)" not in script
    assert "record['stage'] == 'source_binding'" in script


def test_storage_source_metadata_keeps_hardlink_refusal_with_a_fixed_reason(tmp_path):
    namespace = _early_reader_namespace()
    primary, alias = tmp_path / "git.exe", tmp_path / "git-lfs.exe"
    primary.write_bytes(b"inert fixture; never executed")
    namespace["ordinary"](primary, False)
    os.link(primary, alias)
    with pytest.raises(namespace["SourceBindingError"]) as error:
        namespace["ordinary"](primary, False)
    assert error.value.code == "source_file_links"
    assert primary.stat().st_nlink == alias.stat().st_nlink == 2
    assert primary.read_bytes() == alias.read_bytes() == b"inert fixture; never executed"


@pytest.mark.skipif(os.name != "nt", reason="Installed Windows Git metadata required")
def test_real_installed_git_metadata_preserves_admission_and_reports_bounded_count(capsys):
    namespace = _early_reader_namespace()
    git = Path(r"C:\Program Files\Git\cmd\git.exe")
    record = {
        "schema_version": "installed-git-metadata-v1",
        "scope": "diagnostic-only-source-tool-metadata",
        "classification": "metadata_unavailable",
        "link_count": None,
        "storage_admission": False,
        "retry_authority": False,
    }
    try:
        count = git.lstat().st_nlink
        if type(count) is int and 1 <= count <= 32768:
            record.update(
                classification="single_link" if count == 1 else "multiple_links",
                link_count=count,
            )
    finally:
        # Only this fixed record is exposed, never paths, identities or exception text.
        with capsys.disabled():
            print("\nK5_INSTALLED_GIT_METADATA=" + json.dumps(record, separators=(",", ":")))
    if record["classification"] == "single_link":
        namespace["ordinary"](git, False)
        assert namespace["file_digest"](git) == hashlib.sha256(git.read_bytes()).hexdigest()
    elif record["classification"] == "multiple_links":
        with pytest.raises(namespace["SourceBindingError"]) as error:
            namespace["ordinary"](git, False)
        assert error.value.code == "source_file_links"
    else:
        pytest.fail("Fixed installed Git metadata is unavailable or outside its reporting bound")


@pytest.mark.skipif(os.name != "nt", reason="Installed Windows Git binary reader required")
@pytest.mark.parametrize(
    "case", ["lf", "crlf", "trailing", "binary", "module", "missing", "kind", "limit"]
)
def test_real_windows_python_git_reader_preserves_bytes_and_refusals(tmp_path, case):
    # Exercise the reader layer only. This is not storage/ACL admission, and the
    # separate metadata witness continues to require refusal of any hardlink.
    namespace = _early_reader_namespace()
    git = Path(r"C:\Program Files\Git\cmd\git.exe")
    assert git.is_file()
    environment = {
        key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")
    }
    environment.update(
        GIT_AUTHOR_NAME="source-fixture",
        GIT_AUTHOR_EMAIL="source-fixture@example.invalid",
        GIT_COMMITTER_NAME="source-fixture",
        GIT_COMMITTER_EMAIL="source-fixture@example.invalid",
        GIT_AUTHOR_DATE="2000-01-01T00:00:00+0000",
        GIT_COMMITTER_DATE="2000-01-01T00:00:00+0000",
        GIT_NO_REPLACE_OBJECTS="1",
        GIT_NO_LAZY_FETCH="1",
        GIT_ALLOW_PROTOCOL="",
    )

    def git_bytes(*arguments, data=None):
        return subprocess.run(
            [str(git), "-c", "protocol.allow=never", "-C", str(tmp_path), *arguments],
            input=data,
            capture_output=True,
            env=environment,
            timeout=10,
            check=True,
        ).stdout

    git_bytes("-c", "init.templateDir=", "init", "--quiet", "--object-format=sha1")
    body = {
        "lf": b"value = 1\n",
        "crlf": b"value = 1\r\n",
        "trailing": b"value = 1\r\n\r\n",
        "binary": b"\xef\xbb\xbf\x00\xff\n",
    }.get(case, b"value = 1\n")
    if case == "module":
        body = (ROOT / "scripts/installer_wheel_storage.py").read_bytes()
    blob = git_bytes("hash-object", "-w", "--stdin", data=body).decode().strip()
    import base64

    scripts_tree = (
        git_bytes("mktree", data=f"100644 blob {blob}\tinstaller_wheel_storage.py\n".encode())
        .decode()
        .strip()
    )
    tree = (
        git_bytes("mktree", data=f"040000 tree {scripts_tree}\tscripts\n".encode()).decode().strip()
    )
    revision = git_bytes("commit-tree", tree, data=b"immutable reader witness\n").decode().strip()
    packet = [
        [oid, kind, base64.b64encode(git_bytes("cat-file", kind, oid)).decode()]
        for oid, kind in (
            (revision, "commit"),
            (tree, "tree"),
            (scripts_tree, "tree"),
            (blob, "blob"),
        )
    ]
    if case == "missing":
        packet.pop()
    if case == "kind":
        packet[3][1] = "commit"
    if case == "limit":
        packet[3][2] = "A" * 174765
    if case in {"missing", "kind", "limit"}:
        with pytest.raises(namespace["SourceBindingError"]):
            namespace["verified_storage_source"](packet, revision)
    else:
        received = namespace["verified_storage_source"](packet, revision)
        assert received == body
        if case == "module":
            import types

            module = types.ModuleType("_k5_early_storage_test")
            module.__file__ = str(ROOT / "scripts/installer_wheel_storage.py")
            exec(compile(received, "<verified-test-storage-source>", "exec"), module.__dict__)
            assert callable(module.storage_preflight)  # Never query the real storage policy.


def test_early_preflight_is_branch_only_before_provisioning_without_acceptance_reuse():
    text = WORKFLOW.read_text()
    step = _step("Observe read-only storage admission before provisioning")
    assert f"if: github.ref == 'refs/heads/{UPGRADE_BRANCH}'" in step
    names = [
        "Require idle exact physical host before provisioning",
        "Require exact Git-backed source checkouts",
        "Bind early storage source under held Git admission",
        "Observe read-only storage admission before provisioning",
        "Provision existing reviewed GStreamer runtime",
        "Set up Python",
        "Create isolated witness controller",
    ]
    assert [text.index("- name: " + name) for name in names] == sorted(
        text.index("- name: " + name) for name in names
    )
    for forbidden in (
        "setup-python",
        "Remove-Item",
        "pip ",
        "Invoke-WebRequest",
        "Invoke-RestMethod",
        "GITHUB_OUTPUT",
        "GITHUB_ENV",
        "retain_bundle",
        "shutil",
        "Get-Command",
        "where.exe",
        "python -m",
        "LookupAccount",
    ):
        assert forbidden not in step
    assert "New-Item" not in step
    assert "_tool\\Python\\3.12.10\\x64\\python.exe" in step
    assert "4d6f5f81a4bca11191c4c7c6b43632694d0a4ce74e068619d8fdc161d469859a" in step
    assert "& $base -I -B -S - 2>$null" in step
    assert '"code":"bootstrap_unavailable"' in step
    assert "later_admission_required" in step
    assert "exec(compile(source, '<k5-verified-storage-preflight>', 'exec')" in step
    assert "module.__file__ = str(workspace / 'scripts/installer_wheel_storage.py')" in step
    assert "importlib" not in step
    assert "sys.prefix == sys.base_prefix" in step
    assert "path.resolve(strict=True) == path" in step
    later = text.split("- name: Provision existing reviewed GStreamer runtime", 1)[1]
    assert "K5_STORAGE_PREFLIGHT" not in later
    assert "capture_installer_wheel_provenance.py" in later


def test_host_wrapper_is_raw_binary_git_bound_and_finishes_before_same_process_guard():
    script = _run_script(_step("Require idle exact physical host before provisioning"))
    assert script.count("& $script -DiagnosticContext $context") == 1
    assert "--no-replace-objects --no-lazy-fetch -c protocol.allow=never cat-file --batch" in script
    assert "Read-K5GitObject $env:K5_STAGE_ONE_REVISION 'commit' 65536" in script
    assert "$commit.oid -cne $env:K5_STAGE_ONE_REVISION" in script
    assert "':scripts/assert-stage-one-physical-admission.ps1') 'blob' 131072" in script
    assert "[Text.UTF8Encoding]::new($false, $true)" in script
    assert "$sha256.ComputeHash($producer.bytes)" in script
    assert "$producerText = $utf8.GetString($producer.bytes)" in script
    assert "[scriptblock]::Create($producerText)" in script
    assert "return ,$buffer" in script
    assert "CopyToAsync([IO.Stream]::Null)" in script
    assert "$drain.Wait($remaining)" in script
    assert "$process.WaitForExit(2000)" in script
    assert script.index("$process.Dispose()") < script.index("& $script -DiagnosticContext")
    for forbidden in (
        "Out-String",
        "ReadAllLines",
        "ReadToEnd",
        "Get-CimInstance",
        "python",
        "Get-Process",
    ):
        assert forbidden not in script


def test_host_source_binding_has_bounded_failure_stages_and_restores_input_encoding():
    script = _run_script(_step("Require idle exact physical host before provisioning"))
    assert script.count("[Console]::InputEncoding = $inputEncoding") == 1
    assert script.count("[Console]::InputEncoding = $previousInputEncoding") == 1
    assert "$previousInputEncoding.CodePage -eq 65001" in script
    assert "$previousInputEncoding.GetPreamble().Length -gt 0" in script
    assert "$inputEncoding.GetPreamble().Length -ne 0" in script
    assert "[Convert]::ToBase64String($inputEncoding.GetBytes($request))" in script
    assert "[Convert]::ToBase64String($asciiRequest)" in script
    assert (
        script.index("$previousInputEncoding = [Console]::InputEncoding")
        < script.index("if ($normalizeInputEncoding) { [Console]::InputEncoding = $inputEncoding }")
        < script.index("$process.Start()")
        < script.index(
            "if ($normalizeInputEncoding) { [Console]::InputEncoding = $previousInputEncoding }"
        )
        < script.index("CopyToAsync([IO.Stream]::Null)")
    )
    assert "$started = $false" in script and "$started = $process.Start()" in script
    assert script.index("if ($started) {") < script.index("$process.HasExited")
    assert set(re.findall(r"\$sourceBindingDiagnostic.stage = '([^']+)'", script)) == {
        "git_binary",
        "commit_read",
        "commit_identity",
        "commit_decode",
        "commit_tree",
        "producer_read",
        "producer_hash",
        "producer_decode",
        "script_compile",
        "context",
    }
    assert set(re.findall(r"\$sourceBindingDiagnostic.reason = '([^']+)'", script)) == {
        "process_setup",
        "input_encoding",
        "process_start",
        "stderr_drain",
        "request_write",
        "header_read",
        "header_format",
        "object_kind_or_size",
        "body_read",
        "terminator",
        "end_of_output",
        "process_exit",
        "object_hash",
        "process_cleanup",
        "identity",
        "object_id",
        "utf8",
        "shape",
        "sha256",
        "syntax",
        "construct",
    }
    failure = script.split("Write-Host 'K5_HOST_NAME_OBSERVATION_UNAVAILABLE'", 1)[1]
    assert "schema_version = 'host-source-binding-v1'" in failure
    assert "scope = 'diagnostic-only-source-binding'" in failure
    assert "edge_admission = $false" in failure and "retry_authority = $false" in failure
    assert "} catch {}" in failure
    assert "throw 'Exact host diagnostic source binding refused.'" in failure
    for forbidden in ("$_.", "$env:", "$Expression", "$producer", "$commit", "$git"):
        assert forbidden not in failure


def _git_frame(body, kind="blob", oid=None):
    oid = (
        oid
        or hashlib.sha1(kind.encode() + b" " + str(len(body)).encode() + b"\0" + body).hexdigest()
    )
    return (
        oid.encode() + b" " + kind.encode() + b" " + str(len(body)).encode() + b"\n" + body + b"\n"
    )


def _source_packet(
    body=b"value = 1\n",
    *,
    root_name=b"scripts",
    module_name=b"installer_wheel_storage.py",
    module_mode=b"100644",
):
    import base64

    def entry(kind, data):
        oid = hashlib.sha1(
            kind.encode() + b" " + str(len(data)).encode() + b"\0" + data
        ).hexdigest()
        return [oid, kind, base64.b64encode(data).decode()]

    blob = entry("blob", body)
    scripts = entry("tree", module_mode + b" " + module_name + b"\0" + bytes.fromhex(blob[0]))
    root = entry("tree", b"40000 " + root_name + b"\0" + bytes.fromhex(scripts[0]))
    commit = entry("commit", b"tree " + root[0].encode() + b"\n\nfixed immutable fixture\n")
    return [commit, root, scripts, blob]


@pytest.mark.parametrize(
    "body", [b"x=1\n", b"x=1\r\n", b"x=1\r\n\r\n", b"x=1", b"\0\xff\xef\xbb\xbf"]
)
def test_fixed_storage_packet_preserves_binary_bytes(body):
    namespace = _early_reader_namespace()
    packet = _source_packet(body)
    assert namespace["verified_storage_source"](packet, packet[0][0]) == body


@pytest.mark.parametrize(
    "case",
    [
        "not-array",
        "extra-object",
        "missing-object",
        "entry-shape",
        "bad-oid",
        "bad-kind",
        "empty",
        "oversize",
        "bad-base64",
        "noncanonical-base64",
        "changed-body",
        "wrong-commit",
        "wrong-root",
        "wrong-scripts",
        "wrong-module",
        "wrong-root-name",
        "wrong-module-name",
        "symlink-module",
    ],
)
def test_fixed_storage_packet_requires_exact_candidate_and_module_membership(case):
    import base64

    namespace = _early_reader_namespace()
    packet = _source_packet()
    revision = packet[0][0]
    if case == "not-array":
        packet = "PRIVATE_CANARY"
    elif case == "extra-object":
        packet.append(packet[-1])
    elif case == "missing-object":
        packet.pop()
    elif case == "entry-shape":
        packet[0].append("PRIVATE_CANARY")
    elif case == "bad-oid":
        packet[0][0] = "A" * 40
    elif case == "bad-kind":
        packet[0][1] = "blob"
    elif case == "empty":
        packet[0][2] = ""
    elif case == "oversize":
        packet[0][2] = "A" * 87385
    elif case == "bad-base64":
        packet[0][2] = "PRIVATE_CANARY%"
    elif case == "noncanonical-base64":
        packet[0][2] = "YR=="
    elif case == "changed-body":
        packet[0][2] = base64.b64encode(b"other commit\n").decode()
    elif case == "wrong-commit":
        revision = "a" * 40
    elif case == "wrong-root":
        packet[1] = _source_packet(b"other")[1]
    elif case == "wrong-scripts":
        packet[2] = _source_packet(b"other")[2]
    elif case == "wrong-module":
        packet[3] = _source_packet(b"other")[3]
    else:
        packet = _source_packet(
            **{
                "wrong-root-name": {"root_name": b"other"},
                "wrong-module-name": {"module_name": b"other.py"},
                "symlink-module": {"module_mode": b"120000"},
            }[case]
        )
        revision = packet[0][0]
    with pytest.raises(namespace["SourceBindingError"], match="binding"):
        namespace["verified_storage_source"](packet, revision)


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"40000 scripts\0",
        b"40000 scripts\0" + b"a" * 19,
        b"00000 scripts\0" + b"a" * 20,
        b"40000 ../scripts\0" + b"a" * 20,
        b"40000 scripts\0" + b"a" * 20 + b"40000 scripts\0" + b"b" * 20,
    ],
)
def test_fixed_tree_path_proof_refuses_missing_malformed_and_duplicate_entries(body):
    namespace = _early_reader_namespace()
    with pytest.raises(namespace["SourceBindingError"]):
        namespace["tree_member"](body, b"scripts", b"40000")


def test_storage_python_has_no_process_or_git_alias_authority():
    source = _early_storage_bootstrap()
    for forbidden in (
        "subprocess",
        "threading",
        "read_object(",
        "ordinary(git",
        "GIT_ALIAS",
        "receipt",
        "st_nlink >",
    ):
        assert forbidden not in source
    assert "require(info.st_nlink == 1, 'source_file_links')" in source
    assert "verified_storage_source(read_source_packet(packet_path), revision)" in source
    assert source.index("source = verified_storage_source") < source.index("exec(compile(source")


def test_raw_storage_reader_is_identical_to_host_reader_and_held_per_call():
    readers = []
    for name in (
        "Require idle exact physical host before provisioning",
        "Bind early storage source under held Git admission",
    ):
        script = _run_script(_step(name))
        start = script.index("function Read-K5GitObject(")
        finish = script.index("return & $invokeGit -Operation $operation", start)
        reader = script[start:finish]
        # indentation differs because host diagnostics retain their branch block
        readers.append("\n".join(line.strip() for line in reader.splitlines()))
        assert "$start.FileName = $AdmittedGitPath" in reader
        assert "10000 - [int]$watch.ElapsedMilliseconds" in reader
        assert "$process.WaitForExit(2000)" in reader
        assert "if (-not $process.HasExited) { $process.Kill() }" in reader
        assert "$process.Dispose()" in reader
        assert "CopyToAsync([IO.Stream]::Null)" in reader
        assert (
            "--no-replace-objects --no-lazy-fetch -c protocol.allow=never cat-file --batch"
            in reader
        )
        assert "$start.EnvironmentVariables.Remove($key)" in reader
    assert readers[0] == readers[1]


def test_each_checkout_is_adjacent_to_fresh_full_boundary_steps():
    text = WORKFLOW.read_text()
    names = re.findall(r"^      - name: (.+)$", text, re.M)
    for checkout, before, after in (
        (
            "Checkout immutable candidate",
            "Admit existing Git before immutable checkouts",
            "Require idle exact physical host before provisioning",
        ),
        (
            "Checkout immutable Analytics source",
            "Require idle exact physical host before provisioning",
            "Require exact Git-backed source checkouts",
        ),
    ):
        index = names.index(checkout)
        assert names[index - 1 : index + 2] == [before, checkout, after]
        assert (
            _run_script(_step(before))
            .rstrip()
            .endswith("& $invokeGit -Operation { param($AdmittedGitPath) } | Out-Null")
        )
        post = _run_script(_step(after))
        proof = post.index("& $invokeGit -Operation { param($AdmittedGitPath) } | Out-Null")
        assert proof > post.index("$invokeGit = &")
    assert "background" not in text.lower()


def test_all_initializer_sources_match_frozen_local_blob_bytes():
    text = WORKFLOW.read_text()
    expected = {
        "assert-stage-one-physical-admission.ps1": (
            7643,
            "9faae324ffaf008a7dc389aaab2d70198c5f4ea1",
            "d7a38b5278802d9ba768d9987b4582a219d490923b0cc4da0c297d29a250b45d",
        ),
    }
    for filename in ("assert-installed-git-alias.ps1", "admit_installed_git_alias.cs"):
        data = canonical_checkout_bytes((ROOT / "scripts" / filename).read_bytes())
        expected[filename] = (
            len(data),
            hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest(),
            hashlib.sha256(data).hexdigest(),
        )
    pins = re.findall(
        r"name = '([^']+)'; size = ([0-9]+); blob = '([0-9a-f]{40})'; sha256 = '([0-9a-f]{64})'",
        text,
    )
    assert len(pins) == 27
    for filename, size, blob, sha256 in pins:
        assert (int(size), blob, sha256) == expected[filename]


def test_compiler_posts_are_separate_always_run_file_only_and_non_mutating():
    for name, owner_ids in (
        (
            "Independently admit early Git compiler artifacts",
            (
                "git_before_candidate",
                "git_between_checkouts",
                "git_after_analytics",
                "git_storage_source",
            ),
        ),
        ("Independently admit fixture Git compiler artifacts", ("git_fixture_revision",)),
        ("Independently admit launcher Git compiler artifacts", ("git_launcher_archives",)),
    ):
        post = _step(name)
        assert "if: always()" in post
        assert "Assert-K5OrdinaryCompilerPath $entry $false" in post
        assert "EnumerateFileSystemEntries($path)" in post
        assert "$count -gt 64" in post and "$bytes -gt 16777216" in post
        assert "'K5_GIT_COMPILER_POST=passed'" in post
        assert post.count("& $trustedGuard | Out-Null") == 2
        for owner_id in owner_ids:
            assert "'" + owner_id + "'" in post
            assert WORKFLOW.read_text().index("id: " + owner_id) < WORKFLOW.read_text().index(
                "- name: " + name
            )
        for forbidden in (
            "Add-Type",
            "Remove-Item",
            "-Recurse",
            "$entry $true",
            "Set-Acl",
            "icacls",
            "[IO.File]::Delete",
        ):
            assert forbidden not in post


@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell required")
@pytest.mark.parametrize(
    "case",
    [
        "lf",
        "crlf",
        "trailing",
        "missing",
        "bad-header",
        "oversize",
        "short",
        "extra",
        "hash",
        "wrong-kind",
        "stderr-flood",
        "nonzero",
        "timeout",
    ],
    ids=str,
)
def test_actual_powershell_binary_reader_bounds_and_raw_bytes(tmp_path, case):
    import base64

    script = _run_script(_step("Require idle exact physical host before provisioning"))
    start = script.index("    function Read-K5GitObject(")
    end = script.index("    try {\n        if ($env:GITHUB_REPOSITORY", start)
    reader = script[start:end]
    body = {"lf": b"x=1\n", "crlf": b"x=1\r\n", "trailing": b"x=1\r\n\n"}.get(case, b"x=1\n")
    frame = _git_frame(body)
    if case == "missing":
        frame = b"a" * 40 + b" missing\n"
    if case == "bad-header":
        frame = b"PRIVATE_CANARY" * 20 + b"\n"
    if case == "oversize":
        frame = b"a" * 40 + b" blob 131073\n"
    if case == "short":
        frame = frame[:-3]
    if case == "extra":
        frame += b"PRIVATE_CANARY"
    if case == "hash":
        frame = _git_frame(body, oid="a" * 40)
    if case == "wrong-kind":
        frame = _git_frame(body, "commit")
    emitter = (
        "$bytes=[Convert]::FromBase64String('" + base64.b64encode(frame).decode() + "');"
        "[Console]::OpenStandardOutput().Write($bytes,0,$bytes.Length);"
    )
    if case == "stderr-flood":
        emitter = (
            "$err=[byte[]]::new(1048576);[Console]::OpenStandardError().Write($err,0,$err.Length);"
            + emitter
        )
    if case == "timeout":
        emitter = "Start-Sleep -Seconds 30;" + emitter
    emitter += "exit " + ("3" if case == "nonzero" else "0")
    encoded = base64.b64encode(emitter.encode("utf-16-le")).decode()
    # This sole test seam substitutes a local synthetic child for Git. All actual
    # binary framing, deadline, hashing and process/pipe cleanup code executes.
    reader = reader.replace(
        "$start.FileName = $AdmittedGitPath",
        "$start.FileName = (Get-Command powershell).Source",
    )
    reader = re.sub(
        r"\$start.Arguments = '[^']+'",
        "$start.Arguments = '-NoProfile -NonInteractive -EncodedCommand " + encoded + "'",
        reader,
    )
    script_path = tmp_path / "reader.ps1"
    script_path.write_text(
        "$ErrorActionPreference='Stop'\n$env:GITHUB_WORKSPACE=$PSScriptRoot\n"
        "$sourceBindingDiagnostic=@{stage='test';reason='initial'}\n"
        "$invokeGit={param($Operation) & $Operation 'C:\\Program Files\\Git\\cmd\\git.exe'}\n"
        + reader
        + "\ntry { $value=Read-K5GitObject ('a'*40) 'blob' 131072;"
        " Write-Output ('BYTES='+[Convert]::ToBase64String($value.bytes)); exit 0 }"
        " catch { Write-Output 'SOURCE_REFUSED'; exit 1 }\n"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-File", str(script_path)],
        capture_output=True,
        text=True,
        timeout=25,
    )
    assert "PRIVATE_CANARY" not in result.stdout + result.stderr
    passed = case in {"lf", "crlf", "trailing", "stderr-flood"}
    assert result.returncode == (0 if passed else 1)
    if passed:
        assert result.stdout.strip() == "BYTES=" + base64.b64encode(body).decode()
    else:
        assert result.stdout.strip() == "SOURCE_REFUSED"


@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell required")
@pytest.mark.parametrize(
    "case",
    [
        "oem437",
        "utf8",
        "utf8-bom",
        "unsupported-encoding",
        "wrong-sha",
        "bad-git-hash",
        "missing-commit",
        "wrong-kind",
        "missing-producer",
        "invalid-utf8",
        "invalid-script",
        "start-failure",
    ],
)
def test_real_git_powershell_immutable_binding_and_encoding_restoration(tmp_path, case):
    git = Path(r"C:\Program Files\Git\cmd\git.exe")
    assert git.is_file(), (
        "The hosted witness requires the same fixed Git installation as the reader"
    )
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(
        GIT_AUTHOR_NAME="source-fixture",
        GIT_AUTHOR_EMAIL="source-fixture@example.invalid",
        GIT_COMMITTER_NAME="source-fixture",
        GIT_COMMITTER_EMAIL="source-fixture@example.invalid",
        GIT_AUTHOR_DATE="2000-01-01T00:00:00+0000",
        GIT_COMMITTER_DATE="2000-01-01T00:00:00+0000",
        GIT_NO_REPLACE_OBJECTS="1",
        GIT_NO_LAZY_FETCH="1",
        GIT_ALLOW_PROTOCOL="",
    )
    repository = tmp_path / "repository"
    repository.mkdir()

    def git_bytes(*arguments, data=None):
        return subprocess.run(
            [str(git), "-c", "protocol.allow=never", "-C", str(repository), *arguments],
            input=data,
            capture_output=True,
            env=environment,
            timeout=10,
            check=True,
        ).stdout

    git_bytes("-c", "init.templateDir=", "init", "--quiet", "--object-format=sha1")
    body = (ROOT / "scripts/assert-stage-one-physical-admission.ps1").read_bytes()
    if case == "utf8-bom":
        body = body.replace(b"\n", b"\r\n") + b"\r\n\n"
    elif case == "invalid-utf8":
        body = b"\xff"
    elif case == "invalid-script":
        body = b"param(\n"
    blob = git_bytes("hash-object", "-w", "--stdin", data=body).decode().strip()
    scripts_tree = (
        git_bytes(
            "mktree", data=f"100644 blob {blob}\tassert-stage-one-physical-admission.ps1\n".encode()
        )
        .decode()
        .strip()
    )
    tree = (
        git_bytes(
            "mktree",
            data=b""
            if case == "missing-producer"
            else f"040000 tree {scripts_tree}\tscripts\n".encode(),
        )
        .decode()
        .strip()
    )
    revision = git_bytes("commit-tree", tree, data=b"immutable source witness\n").decode().strip()
    assert git_bytes("cat-file", "blob", blob) == body
    # A different working file must not influence immutable loading or execution.
    (repository / "scripts").mkdir()
    (repository / "scripts/assert-stage-one-physical-admission.ps1").write_text(
        "throw 'PRIVATE_CANARY mutable source executed'\n"
    )
    environment.update(
        GITHUB_WORKSPACE=str(repository),
        GITHUB_REPOSITORY="mkurtgerald/K5-Vision",
        K5_CANDIDATE_BRANCH=UPGRADE_BRANCH,
        GITHUB_SHA=revision,
        K5_STAGE_ONE_REVISION=revision,
        GITHUB_RUN_ID="100",
        GITHUB_RUN_ATTEMPT="2",
        K5_CHECKOUT_GIT_SHA256=hashlib.sha256(git.read_bytes()).hexdigest(),
    )
    if case == "wrong-sha":
        environment["GITHUB_SHA"] = "0" * 40
    elif case == "bad-git-hash":
        environment["K5_CHECKOUT_GIT_SHA256"] = "0" * 64
    elif case in {"missing-commit", "wrong-kind"}:
        environment["GITHUB_SHA"] = environment["K5_STAGE_ONE_REVISION"] = (
            "0" * 40 if case == "missing-commit" else blob
        )
    wrapper = _run_script(_step("Require idle exact physical host before provisioning"))
    wrapper = wrapper[wrapper.index("if ($env:K5_CANDIDATE_BRANCH") :]
    wrapper = (
        "$invokeGit={param($Operation) & $Operation 'C:\\Program Files\\Git\\cmd\\git.exe'}\n"
        + wrapper
    )
    invocation = "& $script -DiagnosticContext $context"
    assert wrapper.count(invocation) == 1
    # Exercise the exact binding wrapper, but never invoke the physical guard.
    wrapper = wrapper.replace(
        invocation,
        "$compiledHash = [Security.Cryptography.SHA256]::Create(); "
        "try { $compiled = ([BitConverter]::ToString($compiledHash.ComputeHash("
        "[Text.Encoding]::UTF8.GetBytes($script.ToString()))))"
        ".Replace('-', '').ToLowerInvariant() } "
        "finally { $compiledHash.Dispose() }; "
        "Write-Output ('BOUND=' + (@{source_sha=$context.source_sha; "
        "source_tree=$context.source_tree; producer_sha256=$context.producer_sha256; "
        "compiled_sha256=$compiled} | ConvertTo-Json -Compress))",
    )
    if case == "start-failure":
        wrapper = wrapper.replace(
            "$start.FileName = $AdmittedGitPath",
            "$start.FileName = (Join-Path $env:GITHUB_WORKSPACE 'missing-git.exe')",
        )
    encoding = {
        "oem437": "[Text.Encoding]::GetEncoding(437)",
        "utf8": "[Text.UTF8Encoding]::new($false)",
        "unsupported-encoding": "[Text.UnicodeEncoding]::new($false, $false)",
    }.get(case, "[Text.UTF8Encoding]::new($true)")
    witness = tmp_path / "binding-witness.ps1"
    witness.write_text(
        "$ErrorActionPreference='Stop'\n$before=[Console]::InputEncoding\n$exitCode=0\n"
        "try {\n$requested=" + encoding + "\n[Console]::InputEncoding=$requested\n"
        "$expected=[Console]::InputEncoding\n"
        "if ($expected.CodePage -ne $requested.CodePage -or "
        "[Convert]::ToBase64String($expected.GetPreamble()) -cne "
        "[Convert]::ToBase64String($requested.GetPreamble())) "
        "{ throw 'ENCODING_FIXTURE_UNAVAILABLE' }\ntry {\n" + wrapper + "\n} catch {\n"
        "Write-Output 'BINDING_WITNESS_REFUSED'; $exitCode=1\n} finally {\n"
        "$actual=[Console]::InputEncoding\n"
        "$restored=$actual.CodePage -eq $expected.CodePage -and "
        "[Convert]::ToBase64String($actual.GetPreamble()) -ceq "
        "[Convert]::ToBase64String($expected.GetPreamble())\n"
        "Write-Output ('ENCODING_RESTORED='+$restored)\n}\n"
        "} finally { [Console]::InputEncoding=$before }\nexit $exitCode\n"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-File", str(witness)],
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
    )
    assert "PRIVATE_CANARY" not in result.stdout + result.stderr
    lines = result.stdout.splitlines()
    assert lines.count("ENCODING_RESTORED=True") == 1, result.stdout + result.stderr
    refused = {
        "wrong-sha": ("environment", "context"),
        "unsupported-encoding": ("commit_read", "input_encoding"),
        "bad-git-hash": ("git_binary", "identity"),
        "missing-commit": ("commit_read", "header_format"),
        "wrong-kind": ("commit_read", "object_kind_or_size"),
        "missing-producer": ("producer_read", "header_format"),
        "invalid-utf8": ("producer_decode", "utf8"),
        "invalid-script": ("script_compile", "syntax"),
        "start-failure": ("commit_read", "process_start"),
    }
    records = [
        line.split("=", 1)[1] for line in lines if line.startswith("K5_HOST_SOURCE_BINDING=")
    ]
    bound = [line.split("=", 1)[1] for line in lines if line.startswith("BOUND=")]
    if case in refused:
        assert result.returncode == 1 and not bound
        assert lines.count("BINDING_WITNESS_REFUSED") == 1
        assert lines.count("K5_HOST_NAME_OBSERVATION_UNAVAILABLE") == 1
        assert len(records) == 1
        stage, reason = refused[case]
        assert json.loads(records[0]) == {
            "schema_version": "host-source-binding-v1",
            "scope": "diagnostic-only-source-binding",
            "status": "refused",
            "stage": stage,
            "reason": reason,
            "edge_admission": False,
            "retry_authority": False,
        }
    else:
        assert result.returncode == 0 and not records, result.stdout + result.stderr
        assert "K5_HOST_NAME_OBSERVATION_UNAVAILABLE" not in lines
        assert len(bound) == 1
        assert json.loads(bound[0]) == {
            "source_sha": revision,
            "source_tree": tree,
            "producer_sha256": hashlib.sha256(body).hexdigest(),
            "compiled_sha256": hashlib.sha256(body).hexdigest(),
        }


@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell required")
@pytest.mark.parametrize(
    "case",
    [
        "passed",
        "refused",
        "stderr",
        "empty",
        "malformed",
        "identity-canary",
        "bad-status",
        "mask-overflow",
        "distance-overflow",
        "bool-stage",
        "array-code",
        "valid-ace",
        "constructor-identity",
        "source-links",
        "source-object-hash",
        "source-wrong-stage",
        "source-unknown-code",
    ],
    ids=str,
)
def test_actual_powershell_bootstrap_invocation_suppresses_startup_output(tmp_path, case):
    record = dict(
        schema_version="storage-preflight-v1",
        scope="read-only-early-storage-admission",
        status="passed" if case == "passed" else "refused",
        stage="storage_root" if case == "passed" else "source_binding",
        code="passed" if case == "passed" else "source_unavailable",
        storage_acl=None,
        retention_state="not_started",
        later_admission_required=True,
    )
    if case == "constructor-identity":
        record.update(stage="storage_construction", code="storage_identity")
    if case in {"source-links", "source-wrong-stage"}:
        record["code"] = "source_file_links"
        if case == "source-wrong-stage":
            record["stage"] = "runtime_binding"
    if case == "source-object-hash":
        record["code"] = "source_object_hash"
    if case == "source-unknown-code":
        record["code"] = "PRIVATE_CANARY"
    if case == "identity-canary":
        record["storage_acl"] = {"owner": "PRIVATE_CANARY"}
    if case in {"mask-overflow", "distance-overflow", "valid-ace"}:
        record.update(stage="storage_root", code="storage_acl")
        record["storage_acl"] = dict(
            schema_version="storage-acl-admission-v2",
            path_role="ancestor",
            path_context="runner_workspace",
            admission_mode="ancestor",
            phase="ace_policy",
            native_call="GetNamedSecurityInfoW",
            native_error="success",
            reason="foreign_mutating_allow",
            ancestor_distance=2,
            ace=dict(
                ace_type="allow",
                principal_category="builtin_users",
                effective_principal_category="builtin_users",
                access_mask=0x40000000,
                expanded_access_mask=0x00120116,
                inheritance_flags=0x13,
            ),
        )
        if case == "mask-overflow":
            record["storage_acl"]["ace"]["access_mask"] = 2**64
        if case == "distance-overflow":
            record["storage_acl"]["ancestor_distance"] = 65
    if case == "bool-stage":
        record["stage"] = True
    if case == "array-code":
        record["code"] = ["source_unavailable"]
    if case == "bad-status":
        record["status"] = "PRIVATE_CANARY"
    line = "K5_STORAGE_PREFLIGHT=" + json.dumps(record, separators=(",", ":"))
    if case == "malformed":
        line = "PRIVATE_CANARY startup"
    script = _run_script(_step("Observe read-only storage admission before provisioning"))
    invocation = script.split("\n'@\n", 1)[1]
    emitter = ""
    if case != "empty":
        emitter = "Write-Output '" + line + "'\n"
    if case == "stderr":
        emitter += "Write-Error 'PRIVATE_CANARY startup' -ErrorAction Continue\n"
    harness = (
        "$ErrorActionPreference='Stop'\n$bootstrap='fixed bootstrap'\n"
        "$sourcePacket='[]'\n$base='Invoke-TestProbe'\n"
        "function Invoke-TestProbe {\n"
        + emitter
        + "$global:LASTEXITCODE="
        + ("0" if case == "passed" else "1")
        + "\n}\n"
        "try {\n" + invocation + "\n} catch { Write-Output 'TERMINAL_REFUSAL' }\n"
    )
    path = tmp_path / "invoke.ps1"
    path.write_text(harness)
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-File", str(path)],
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    assert "PRIVATE_CANARY" not in result.stdout + result.stderr
    logs = [line for line in result.stdout.splitlines() if line.startswith("K5_STORAGE_PREFLIGHT=")]
    assert len(logs) == 1
    emitted = json.loads(logs[0].split("=", 1)[1])
    assert emitted["retention_state"] == "not_started"
    assert emitted["later_admission_required"] is True
    if case == "passed":
        assert emitted["status"] == "passed" and "TERMINAL_REFUSAL" not in result.stdout
    else:
        assert emitted["status"] == "refused" and "TERMINAL_REFUSAL" in result.stdout
        if case == "constructor-identity":
            assert emitted["code"] == "storage_identity"
        if case in {"source-links", "source-object-hash"}:
            assert emitted["code"] == record["code"] and emitted["stage"] == "source_binding"
        if case == "valid-ace":
            assert emitted["storage_acl"]["ace"]["access_mask"] == 0x40000000
        elif case in {
            "mask-overflow",
            "distance-overflow",
            "bool-stage",
            "array-code",
            "source-wrong-stage",
            "source-unknown-code",
        }:
            assert emitted["code"] == "bootstrap_unavailable"


@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell required")
@pytest.mark.parametrize(
    "case", ["present", "missing", "changed", "hardlink", "reparse", "alias"], ids=str
)
def test_actual_early_powershell_runtime_binding_has_no_setup_or_fallback(tmp_path, case):
    work = tmp_path / "work"
    runner = work / "K5-Vision"
    workspace = runner / "K5-Vision"
    temporary = work / "_temp"
    runtime = work / "_tool/Python/3.12.10/x64/python.exe"
    workspace.mkdir(parents=True)
    temporary.mkdir()
    runtime.parent.mkdir(parents=True)
    if case != "missing":
        runtime.write_bytes(b"inert fixture, never executed")
    script = _run_script(_step("Observe read-only storage admission before provisioning"))
    admission = script.split("$bootstrap = @'", 1)[0]
    setup = (
        "$env:RUNNER_WORKSPACE='" + str(runner) + "'\n"
        "$env:GITHUB_WORKSPACE='" + str(workspace) + "'\n"
        "$env:RUNNER_TEMP='" + str(temporary) + "'\n"
    )
    if case == "alias":
        setup += "$env:RUNNER_WORKSPACE += '\\..\\K5-Vision'\n"
    if case != "changed":
        setup += (
            "function Get-FileHash { param($LiteralPath,$Algorithm); [pscustomobject]@{"
            "Hash='4d6f5f81a4bca11191c4c7c6b43632694d0a4ce74e068619d8fdc161d469859a'} }\n"
        )
    if case in {"hardlink", "reparse"}:
        setup += (
            "function Get-Item { param($LiteralPath,[switch]$Force,$ErrorAction);"
            "$item=Microsoft.PowerShell.Management\\Get-Item @PSBoundParameters;"
            " if($LiteralPath.EndsWith('python.exe')) { return [pscustomobject]@{"
            "FullName=$LiteralPath;PSIsContainer=$false;Parent=$null;"
            + (
                "LinkType='HardLink';Attributes=0"
                if case == "hardlink"
                else "LinkType='';Attributes=[IO.FileAttributes]::ReparsePoint"
            )
            + "} }; return $item }\n"
        )
    path = tmp_path / "runtime.ps1"
    path.write_text(
        setup
        + "try {\n"
        + admission
        + "\nWrite-Output 'RUNTIME_BOUND' } catch { Write-Output 'RUNTIME_REFUSED' }\n"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-File", str(path)],
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    assert "PRIVATE_CANARY" not in result.stdout + result.stderr
    if case == "present":
        assert "RUNTIME_BOUND" in result.stdout
    else:
        assert "RUNTIME_REFUSED" in result.stdout
        emitted = next(
            line for line in result.stdout.splitlines() if line.startswith("K5_STORAGE_PREFLIGHT=")
        )
        assert json.loads(emitted.split("=", 1)[1])["code"] == "runtime_unavailable"


@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell required")
def test_real_powershell_stdin_bootstrap_refuses_impossible_layout_before_native(tmp_path):
    import sys

    # Use only the existing hosted interpreter to exercise transport; it cannot
    # satisfy the designated cache/layout and must never reach NativeStoragePolicy.
    script = _run_script(_step("Observe read-only storage admission before provisioning"))
    bootstrap_and_invocation = "$bootstrap = @'" + script.split("$bootstrap = @'", 1)[1]
    path = tmp_path / "stdin.ps1"
    path.write_text(
        "$ErrorActionPreference='Stop'\n$base='" + sys.executable.replace("'", "''") + "'\n"
        "$env:RUNNER_WORKSPACE=$PSScriptRoot\n$env:GITHUB_WORKSPACE=$PSScriptRoot\n"
        "$env:RUNNER_TEMP=$PSScriptRoot\n$sourcePacket='[]'\ntry {\n"
        + bootstrap_and_invocation
        + "\n} catch { Write-Output ('BOOTSTRAP_EXIT=' + $probeExit) }\n"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-File", str(path)],
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    lines = result.stdout.splitlines()
    assert "BOOTSTRAP_EXIT=1" in lines
    records = [
        json.loads(line.split("=", 1)[1])
        for line in lines
        if line.startswith("K5_STORAGE_PREFLIGHT=")
    ]
    assert len(records) == 1 and records[0]["code"] == "runtime_unavailable"
    assert records[0]["status"] == "refused" and records[0]["storage_acl"] is None
    assert str(tmp_path) not in result.stdout + result.stderr
    assert "Traceback" not in result.stdout + result.stderr


@pytest.mark.parametrize("fault", ["runtime-links", "object-hash", "unknown-code", "exception"])
def test_bootstrap_source_failure_is_a_distinct_bounded_refusal(tmp_path, capsys, fault):
    from types import SimpleNamespace

    namespace = _early_reader_namespace()
    runner = tmp_path / "K5-Vision"
    workspace = runner / "K5-Vision"
    temporary = tmp_path / "_temp"
    runtime = tmp_path / "_tool/Python/3.12.10/x64/python.exe"
    namespace["os"] = SimpleNamespace(
        name="nt",
        environ={
            "RUNNER_WORKSPACE": str(runner),
            "GITHUB_WORKSPACE": str(workspace),
            "RUNNER_TEMP": str(temporary),
            "GITHUB_REPOSITORY": "mkurtgerald/K5-Vision",
            "K5_STAGE_ONE_REVISION": "a" * 40,
            "GITHUB_SHA": "a" * 40,
            "K5_CANDIDATE_BRANCH": UPGRADE_BRANCH,
            "GITHUB_RUN_ID": "100",
            "GITHUB_RUN_ATTEMPT": "2",
            "K5_CHECKOUT_GIT_SHA256": (
                "78211c7ed73988da93a6d8a33d47ec6187f464d7ea2a9a00c182bbd7a1ecf30f"
            ),
        },
    )
    namespace["sys"] = SimpleNamespace(
        implementation=SimpleNamespace(name="cpython"),
        version_info=(3, 12, 10),
        prefix="base",
        base_prefix="base",
        flags=SimpleNamespace(isolated=1, no_site=1),
        dont_write_bytecode=True,
        executable=str(runtime),
    )

    def ordinary(path, directory):
        if (fault == "file-links" and str(path) == r"C:\Program Files\Git\cmd\git.exe") or (
            fault == "runtime-links" and path == runtime
        ):
            raise namespace["SourceBindingError"]("source_file_links")

    namespace["ordinary"] = ordinary
    namespace["file_digest"] = lambda path: (
        "4d6f5f81a4bca11191c4c7c6b43632694d0a4ce74e068619d8fdc161d469859a"
        if path == runtime
        else "b" * 64
    )

    def fail(*args):
        if fault == "object-hash":
            raise namespace["SourceBindingError"]("source_object_hash")
        if fault == "unknown-code":
            raise namespace["SourceBindingError"]("PRIVATE_CANARY")
        if fault == "exception":
            raise OSError("PRIVATE_CANARY")
        raise RuntimeError("PRIVATE_CANARY")

    namespace["read_source_packet"] = lambda path: []
    namespace["verified_storage_source"] = fail
    source = (
        "record = dict(schema_version="
        + _early_storage_bootstrap().split("record = dict(schema_version=", 1)[1]
    )
    with pytest.raises(SystemExit) as caught:
        exec(compile(source, "<tested-bootstrap-entry>", "exec"), namespace)
    assert caught.value.code == 1
    output = capsys.readouterr()
    assert "PRIVATE_CANARY" not in output.out + output.err
    record = json.loads(output.out.strip().split("=", 1)[1])
    assert record["stage"] == ("runtime_binding" if fault == "runtime-links" else "source_binding")
    assert (
        record["code"]
        == {
            "cleanup": "source_cleanup_failed",
            "file-links": "source_file_links",
            "runtime-links": "runtime_unavailable",
            "object-hash": "source_object_hash",
            "unknown-code": "source_commit_read",
            "exception": "source_commit_read",
        }[fault]
    )
    assert record["status"] == "refused" and record["storage_acl"] is None


@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell required")
def test_actual_windows_early_storage_packet_preserves_four_binary_git_objects(tmp_path):
    import base64

    binder = _run_script(_step("Bind early storage source under held Git admission"))
    start = binder.index("$sourceObjects = [object[]]::new(4)")
    end = binder.index("if ($sourcePacket.Length -gt 620000)", start)
    producer = binder[start:end]
    names = ("commit", "rootTree", "scriptsTree", "storage")
    kinds = ("commit", "tree", "tree", "blob")
    bodies = (b"commit-bytes", b"root-tree", b"scripts-tree", b"storage-source")
    script = "$ErrorActionPreference = 'Stop'\n"
    for index, (name, body) in enumerate(zip(names, bodies)):
        encoded = base64.b64encode(body).decode("ascii")
        script += (
            f"${name} = @{{ oid = ('{chr(97 + index)}' * 40); "
            f"bytes = [Convert]::FromBase64String('{encoded}') }}\n"
        )
    script += producer + "\nWrite-Output $sourcePacket\n"
    script_path = tmp_path / "source-packet.ps1"
    script_path.write_text(script, encoding="utf-8")
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-File", str(script_path)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr[-1000:]
    packet = json.loads(result.stdout)
    assert type(packet) is list and len(packet) == 4
    for index, (entry, kind, body) in enumerate(zip(packet, kinds, bodies)):
        assert type(entry) is list and len(entry) == 3
        assert entry[0] == chr(97 + index) * 40 and entry[1] == kind
        assert base64.b64decode(entry[2], validate=True) == body


def test_storage_packet_handoff_is_bounded_data_with_strict_single_links(tmp_path):
    namespace = _early_reader_namespace()
    path = tmp_path / "storage-source.json"
    packet = _source_packet(b"value = 1\r\n")
    path.write_text(json.dumps(packet))
    assert namespace["read_source_packet"](path) == packet
    alias = tmp_path / "alias.json"
    os.link(path, alias)
    with pytest.raises(namespace["SourceBindingError"]) as error:
        namespace["read_source_packet"](path)
    assert error.value.code == "source_file_links"
    binder = _step("Bind early storage source under held Git admission")
    assert "[IO.FileMode]::CreateNew" in binder
    assert "$packetBytes.Length -gt 620000" in binder
    assert "'storage-source.json'" in binder
    assert "GITHUB_ENV" not in binder and "GITHUB_OUTPUT" not in binder
    assert "Assert-K5OrdinaryCompilerPath $packetPath $false" in binder


def test_workflow_run_blocks_fit_actions_hard_character_limit():
    text = WORKFLOW.read_text()
    for name in re.findall(r"^      - name: (.+)$", text, re.M):
        step = _step(name)
        if "run: |" in step:
            assert len(_run_script(step)) <= 21000, name


def test_controlled_direct_git_calls_have_owned_deadlines_and_closed_output():
    utilities = []
    for name in (
        "Admit existing Git before immutable checkouts",
        "Require exact Git-backed source checkouts",
        "Prepare existing bounded rights-reviewed fixture",
        "Prepare independently bound offline Start-script inputs",
    ):
        script = _run_script(_step(name))
        utility = script.split("function Invoke-K5OwnedGitText(", 1)[1].split("$ownedGitText =", 1)[
            0
        ]
        utilities.append(utility)
        assert "$memory.Length + $read.Result -gt 4096" in utility
        assert "$Budget - [int]$watch.ElapsedMilliseconds" in utility
        assert "$process.ExitCode -ne 0" in utility
        assert "$process.WaitForExit(2000)" in utility
        assert "$process.Kill()" in utility and "$process.Dispose()" in utility
        assert "CopyToAsync([IO.Stream]::Null)" in utility
        assert "$start.EnvironmentVariables.Remove($key)" in utility
        assert "$start.FileName = $AdmittedGitPath" in utility
        assert ".GetNewClosure())" in script
    assert all(utility == utilities[0] for utility in utilities)


@pytest.mark.parametrize(
    "raw,expected",
    [
        (b"first\nsecond\n", b"first\nsecond\n"),
        (b"first\r\nsecond\r\n", b"first\nsecond\n"),
        (b"first\r\nsecond\n", b"first\nsecond\n"),
        (b"\xef\xbb\xbffirst\r\n", b"\xef\xbb\xbffirst\n"),
    ],
)
def test_source_pin_reader_only_canonicalizes_checkout_crlf(raw, expected):
    # BOMs and all other content remain byte-visible to the unchanged exact pins.
    assert canonical_checkout_bytes(raw) == expected


@pytest.mark.parametrize("raw", [b"first\rsecond\n", b"first\r\r\n", b"first\n\xff"])
def test_source_pin_reader_rejects_bare_cr_and_invalid_utf8(raw):
    with pytest.raises(ValueError):
        canonical_checkout_bytes(raw)
