"""Exact trusted-branch admission for the installed normal-app native witness."""

import ast
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/installed-analytics-candidate.yml"
BASELINE = ROOT / ".github/workflows/stage-one-operator-physical.yml"
BRANCH = "feat/installed-analytics-operator-20261003"
LAUNCHER_BRANCH = "feat/alpha-analytics-preflight-20261004"
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
    assert "    timeout-minutes: 10\n" in hosted
    assert hosted.count("      - name: ") == 1
    assert "        timeout-minutes: 9\n" in hosted
    assert "    needs: hosted_admission\n" in native
    assert "needs.hosted_admission.result == 'success'" in native
    assert "needs.hosted_admission.outputs.qualified_sha == github.sha" in native
    assert "    timeout-minutes: 25\n" in native
    assert "runs-on: [self-hosted, Windows, X64, k5-physical, camera-lab]" in native
    assert "concurrency:" not in text.split("\njobs:\n", 1)[0]
    assert "concurrency:" not in hosted
    assert "    concurrency:\n      group: stage-one-operator-physical\n" in native
    assert "      cancel-in-progress: false\n" in native


def test_candidate_route_is_two_jobs_on_only_three_exact_trusted_branches():
    text = WORKFLOW.read_text()
    events = text.split("on:\n", 1)[1].split("\npermissions:", 1)[0]
    branches = events.split("    branches:\n", 1)[1].split("    paths:\n", 1)[0]
    assert branches == f"      - {BRANCH}\n      - {LAUNCHER_BRANCH}\n      - {FACADE_BRANCH}\n"
    assert "pull_request" not in events
    assert "workflow_dispatch" not in text
    assert "workflow_call" not in text
    assert "      - main\n" not in events
    assert "github.repository == 'mkurtgerald/K5-Vision'" in text
    assert f"github.ref == 'refs/heads/{BRANCH}'" in text
    assert f"github.ref == 'refs/heads/{LAUNCHER_BRANCH}'" in text
    assert f"github.ref == 'refs/heads/{FACADE_BRANCH}'" in text
    assert text.count("runs-on:") == 2
    assert text.count("runs-on: [self-hosted, Windows, X64, k5-physical, camera-lab]") == 1
    assert "    timeout-minutes: 25\n" in _job("installed-analytics-candidate")


def test_launcher_and_facade_branches_require_their_three_applicable_hosted_gates():
    gate = _run_script(_step("Require exact trusted head and green hosted PR qualification"))
    assert f'$legacyBranch = "{BRANCH}"' in gate
    assert f'$launcherBranch = "{LAUNCHER_BRANCH}"' in gate
    assert f'$facadeBranch = "{FACADE_BRANCH}"' in gate
    assert (
        "$env:K5_CANDIDATE_BRANCH -cnotin @($legacyBranch, $launcherBranch, $facadeBranch)" in gate
    )
    base = re.search(r"(?ms)\$required = @\{\n(.*?)^\s*\}", gate).group(1)
    assert set(re.findall(r'"([0-9]+)" = ', base)) == {"355859781", "369354936", "361865932"}
    assert "$env:K5_CANDIDATE_BRANCH -ceq $legacyBranch" in gate
    assert '$required["362514400"] = "Current Gate Viewport Editor Qualification"' in gate
    assert '$required["364801702"] = "Stage One Analytics Compatibility"' in gate
    assert "$_.head_branch -ceq $env:K5_CANDIDATE_BRANCH" in gate


def test_native_probe_regression_dependency_is_exact_and_hosted_qualified():
    dependency = "tests/test_windows_alpha_analytics.py"
    paths = WORKFLOW.read_text().split("    paths:\n", 1)[1].split("\n# Shares", 1)[0]
    expected = {
        ".github/workflows/installed-analytics-candidate.yml",
        "scripts/installed_analytics_witness.py",
        "scripts/installed_alpha_launcher_witness.py",
        "scripts/installed_alpha_facade_witness.py",
        "scripts/windows_owned_preflight.py",
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
        dependency,
    }
    assert set(paths.splitlines()) == {"      - " + path for path in expected}
    assert len(paths.splitlines()) == len(expected)
    hosted = (
        (ROOT / ".github/workflows/windows-alpha-script-smoke.yml")
        .read_text()
        .split("  pull_request:\n", 1)[1]
        .split("  push:\n", 1)[0]
    )
    assert hosted.count('      - "' + dependency + '"\n') == 1


def test_candidate_and_main_share_non_cancelling_lane_without_new_main_trigger():
    candidate, baseline = WORKFLOW.read_text(), BASELINE.read_text()
    assert "group: stage-one-operator-physical\n      cancel-in-progress: false" in candidate
    assert "group: stage-one-operator-physical\n  cancel-in-progress: false" in baseline
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
    first, native, provisioning, baseline, media = text.split(
        "$head.commit.sha -cne $env:K5_EXPECTED_SHA"
    )[:5]
    assert "Checkout immutable candidate" not in first
    assert "Checkout immutable candidate" in provisioning
    assert "Prepare existing bounded rights-reviewed fixture" in baseline
    assert "Preserve installed Alpha baseline" in media
    assert "Prove installed normal-app analytics" not in media
    for action in re.findall(r"uses: (\S+)", text):
        assert re.fullmatch(r"actions/[a-z-]+@[0-9a-f]{40}", action)


def test_candidate_keeps_ownership_admission_baseline_and_separate_installed_proof():
    text = WORKFLOW.read_text()
    assert text.count("run: .\\scripts\\assert-stage-one-physical-admission.ps1") == 9
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
    assert "$deadline = [DateTime]::UtcNow.AddMinutes(8)" in text
    assert "timeout-minutes: 9" in text
    assert text.index("if (-not $qualified") < text.index("Checkout immutable candidate")


def test_existing_git_is_admitted_after_hosted_gate_before_either_checkout():
    text = WORKFLOW.read_text()
    admission = text.split("- name: Admit existing Git before immutable checkouts", 1)[1]
    admission = admission.split("- name: Checkout immutable candidate", 1)[0]
    assert text.index("if (-not $qualified") < text.index("Admit existing Git")
    assert text.index("Admit existing Git") < text.index("Checkout immutable candidate")
    assert text.index("Admit existing Git") < text.index("Checkout immutable Analytics source")
    assert "timeout-minutes: 1" in admission
    assert "$git = 'C:\\Program Files\\Git\\cmd\\git.exe'" in admission
    assert "[IO.File]::GetAttributes($git)" in admission
    assert "[IO.FileAttributes]::Directory -bor [IO.FileAttributes]::ReparsePoint" in admission
    assert "while ($null -ne $parent)" in admission
    assert "-not $parent.Exists" in admission
    assert "$parent.Attributes -band [IO.FileAttributes]::ReparsePoint" in admission
    assert "$output = @(& $git --version)" in admission
    assert "$exitCode -ne 0 -or $output.Count -ne 1" in admission
    assert r"\.windows\.[0-9]+\z" in admission
    assert "$version -lt [Version]'2.18.0'" in admission
    assert "Get-FileHash -LiteralPath $git -Algorithm SHA256" in admission
    assert 'Write-Host "K5_CHECKOUT_GIT_VERSION=$versionText"' in admission
    assert 'Write-Host "K5_CHECKOUT_GIT_SHA256=$gitSha256"' in admission
    path_export = "(Split-Path -Parent $git) | Out-File -FilePath $env:GITHUB_PATH"
    assert admission.index("$version -lt") < admission.index(path_export)
    for forbidden in (
        "Invoke-WebRequest",
        "Invoke-RestMethod",
        "SetEnvironmentVariable",
        "Set-ItemProperty",
        "Set-ExecutionPolicy",
        "New-Item",
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
    assert "timeout-minutes: 1" in verification
    assert "Get-Command git -CommandType Application -ErrorAction Stop" in verification
    assert "-ine 'C:\\Program Files\\Git\\cmd\\git.exe'" in verification
    assert "$env:GITHUB_WORKSPACE = $env:K5_STAGE_ONE_REVISION" in verification
    assert "'analytics-lab') = $env:ANALYTICS_LAB_SHA" in verification
    assert "Join-Path $source '.git'" in verification
    assert "[IO.File]::GetAttributes($metadata)" in verification
    assert "[IO.FileAttributes]::Directory" in verification
    assert "[IO.FileAttributes]::ReparsePoint" in verification
    assert "@(git -C $source rev-parse HEAD)" in verification
    assert "$LASTEXITCODE -ne 0 -or $actual.Count -ne 1" in verification
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
    assert '--output="$root\\source.zip" $env:K5_STAGE_ONE_REVISION' in preparation
    assert '--output="$root\\analytics.zip" $env:ANALYTICS_LAB_SHA' in preparation
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
    paths = text.split("    paths:\n", 1)[1].split("\n# Shares", 1)[0]
    assert "      - scripts/windows_owned_preflight.py\n" in paths
    witness = (ROOT / "scripts/installed_alpha_launcher_witness.py").read_text()
    # The controller must bind the imported helper's exact bytes before Start.
    preparation = witness.split("def prepare(", 1)[1].split("def probe(", 1)[0]
    assert '"windows_owned_preflight.py"' in preparation
    assert "Path(__file__).with_name(name).read_bytes()" in preparation
    assert (ROOT / "scripts/windows_owned_preflight.py").is_file()
    assert text.count("runs-on:") == 2
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
        "github.ref": ref or f"refs/heads/{FACADE_BRANCH}",
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


@pytest.mark.parametrize("branch", [BRANCH, LAUNCHER_BRANCH, FACADE_BRANCH])
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
    assert "$deadline = [DateTime]::UtcNow.AddMinutes(8)" in script
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
@pytest.mark.parametrize("branch", [BRANCH, LAUNCHER_BRANCH, FACADE_BRANCH])
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
    for forbidden in (
        "fix/transactional-alpha-upgrade",
        "capture_installer",
        "installer_wheel_storage",
        "install_transaction",
        "Set-Acl",
        "icacls",
    ):
        assert forbidden not in text


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
