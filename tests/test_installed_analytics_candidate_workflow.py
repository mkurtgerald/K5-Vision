"""Exact trusted-branch admission for the installed normal-app native witness."""

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


def test_candidate_route_is_one_job_on_only_two_exact_trusted_feature_branches():
    text = WORKFLOW.read_text()
    events = text.split("on:\n", 1)[1].split("concurrency:", 1)[0]
    branches = events.split("    branches:\n", 1)[1].split("    paths:\n", 1)[0]
    assert branches == f"      - {BRANCH}\n      - {LAUNCHER_BRANCH}\n"
    assert "pull_request" not in events
    assert "workflow_dispatch" not in text
    assert "workflow_call" not in text
    assert "      - main\n" not in events
    assert "github.repository == 'mkurtgerald/K5-Vision'" in text
    assert f"github.ref == 'refs/heads/{BRANCH}'" in text
    assert f"github.ref == 'refs/heads/{LAUNCHER_BRANCH}'" in text
    assert text.count("runs-on:") == 1
    assert "runs-on: [self-hosted, Windows, X64, k5-physical, camera-lab]" in text
    assert "timeout-minutes: 35" in text


def test_launcher_branch_requires_only_its_three_applicable_hosted_gates():
    text = WORKFLOW.read_text()
    gate = text.split("- name: Require exact trusted head", 1)[1].split(
        "- name: Admit existing Git", 1
    )[0]
    assert f'$legacyBranch = "{BRANCH}"' in gate
    assert f'$launcherBranch = "{LAUNCHER_BRANCH}"' in gate
    assert "$env:K5_CANDIDATE_BRANCH -cnotin @($legacyBranch, $launcherBranch)" in gate
    base = gate.split("$required = @{", 1)[1].split("\n          }", 1)[0]
    assert set(re.findall(r'"([0-9]+)" = ', base)) == {"355859781", "369354936", "361865932"}
    assert "$env:K5_CANDIDATE_BRANCH -ceq $legacyBranch" in gate
    assert '$required["362514400"] = "Current Gate Viewport Editor Qualification"' in gate
    assert '$required["364801702"] = "Stage One Analytics Compatibility"' in gate
    assert "$_.head_branch -ceq $env:K5_CANDIDATE_BRANCH" in gate


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
    assert text.count("$head.commit.sha -cne $env:K5_EXPECTED_SHA") == 6
    assert text.count("[uri]::EscapeDataString($env:K5_CANDIDATE_BRANCH)") == 6
    first, provisioning, baseline, media = text.split("$head.commit.sha -cne $env:K5_EXPECTED_SHA")[
        :4
    ]
    assert "Checkout immutable candidate" not in first
    assert "Checkout immutable candidate" in provisioning
    assert "Prepare existing bounded rights-reviewed fixture" in baseline
    assert "Preserve installed Alpha baseline" in media
    assert "Prove installed normal-app analytics" not in media
    for action in re.findall(r"uses: (\S+)", text):
        assert re.fullmatch(r"actions/[a-z-]+@[0-9a-f]{40}", action)


def test_candidate_keeps_ownership_admission_baseline_and_separate_installed_proof():
    text = WORKFLOW.read_text()
    assert text.count("run: .\\scripts\\assert-stage-one-physical-admission.ps1") == 7
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
        '"K5_INSTALLED_CONTROLLER_OWNED", "K5_ALPHA_LAUNCHER_OWNED")' in cleanup
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
    required = text.split("$required = @{", 1)[1].split("\n          }", 1)[0]
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
    assert "$deadline = [DateTime]::UtcNow.AddMinutes(6)" in text
    assert "timeout-minutes: 7" in text
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
    return WORKFLOW.read_text().split(f"      - name: {name}\n", 1)[1].split("      - name: ", 1)[0]


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
        body = step.split("        run: |\n", 1)[1]
        lines = [line[10:] if line.startswith("          ") else line for line in body.splitlines()]
        script = tmp_path / f"candidate-step-{index}.ps1"
        script.write_text("\n".join(lines), encoding="utf-8")
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
    assert text.count("runs-on:") == 1
    assert "group: stage-one-operator-physical\n  cancel-in-progress: false" in text
