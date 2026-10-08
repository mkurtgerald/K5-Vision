"""Pure source contracts: never compile, load native code, query Windows, or spawn.

These checks do not establish Windows parsing, inbox compilation, runtime
behavior, or physical acceptance. The final transport is separately reviewed.
"""

import hashlib
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "scripts/observe-installed-git-links.ps1"
GUARD = ROOT / "scripts/assert-stage-one-physical-admission.ps1"
COLLECTOR = ROOT / "scripts/observe_installed_git_links.cs"
TEXT = WRAPPER.read_text(encoding="utf-8")
REFUSALS = {
    "platform",
    "abi",
    "anchor_open",
    "relative_open",
    "metadata",
    "filesystem",
    "reparse",
    "path_type",
    "acl_unavailable",
    "acl_null",
    "alias_outside_root",
    "alias_duplicate",
    "alias_missing_primary",
    "alias_count",
    "identity_mismatch",
    "path_shape",
    "name_bound",
    "file_size",
    "bytes_bound",
    "time_bound",
    "enumeration",
    "changed",
    "read_failed",
    "handle_bound",
    "cleanup_failed",
    "internal",
}


def section(start: str, end: str) -> str:
    return TEXT.split(start, 1)[1].split(end, 1)[0]


def test_pins_are_literal_and_original_guard_bytes_are_unchanged():
    guard_pin = re.search(r"\$guardSha256 = '([0-9a-f]{64})'", TEXT).group(1)
    assert guard_pin == hashlib.sha256(GUARD.read_bytes()).hexdigest()
    assert guard_pin == "d7a38b5278802d9ba768d9987b4582a219d490923b0cc4da0c297d29a250b45d"
    collector_pin = re.search(r"\$collectorSha256 = '([0-9a-f]{64})'", TEXT).group(1)
    assert collector_pin == hashlib.sha256(COLLECTOR.read_bytes()).hexdigest()
    assert collector_pin != "0" * 64
    assert TEXT.count("$collectorSha256 =") == 1
    assert "$collectorSha256 = $env:" not in TEXT
    assert "Open-K5PinnedFile $collectorPath 65536 $collectorSha256" in TEXT
    assert "Open-K5PinnedFile $guardPath 131072 $guardSha256" in TEXT


def test_in_memory_invocation_requires_nonempty_explicit_bundle_parameter():
    declaration = TEXT.split("$ErrorActionPreference", 1)[0]
    assert "[CmdletBinding()]" in declaration
    assert "[Parameter(Mandatory = $true)]" in declaration
    assert "[ValidateNotNullOrEmpty()]" in declaration
    assert "[string]$BundleScripts" in declaration
    assert "[string]$BundleScripts =" not in declaration
    for forbidden in ("$PSScriptRoot", "$PSCommandPath", "$MyInvocation", "$PWD", "Get-Location"):
        assert forbidden not in TEXT


def test_arbitrary_bundle_root_or_nonexact_path_refuses_before_source_read():
    assert not re.search(r"(?im)^\s*\$BundleScripts\s*=", TEXT)
    assert (
        "$bundleRoot = Join-Path $temporary.FullName ('k5-git-link-observation-'"
        " + $context.run_id + '-' + $context.run_attempt)"
    ) in TEXT
    expected = TEXT.index("$expectedBundleScripts = Join-Path $bundleRoot 'scripts'")
    refusal = TEXT.index("$BundleScripts -cne $expectedBundleScripts) { throw 'path' }")
    canonical = TEXT.index("Assert-K5OrdinaryPath $BundleScripts $true")
    source_read = TEXT.index("$guardFile = Open-K5PinnedFile")
    assert expected < refusal < canonical < source_read
    assert "$expectedBundleScripts = $BundleScripts" not in TEXT
    assert "$bundleRoot = $BundleScripts" not in TEXT
    assert "$BundleScripts -ine" not in TEXT
    assert "$BundleScripts.StartsWith" not in TEXT
    assert "$BundleScripts -like" not in TEXT
    assert "$guardPath = Join-Path $BundleScripts 'assert-stage-one-physical-admission.ps1'" in TEXT
    assert "$collectorPath = Join-Path $BundleScripts 'observe_installed_git_links.cs'" in TEXT


def test_actions_context_and_workspace_structure_are_exact():
    body = section("$failure = 'context_binding'", "$failure = 'guard_binding'")
    for required in (
        "$env:OS -cne 'Windows_NT'",
        "[Environment]::MachineName -cne 'VLR-CYZ4PK3'",
        "$env:RUNNER_NAME -cne 'K5-Physical'",
        "$env:GITHUB_REPOSITORY -cne 'mkurtgerald/K5-Vision'",
        r"$env:GITHUB_SHA -cnotmatch '\A[0-9a-f]{40}\z'",
        "$env:K5_DIAGNOSTIC_SHA -cne $env:GITHUB_SHA",
        r"$env:GITHUB_RUN_ID -cnotmatch '\A[1-9][0-9]{0,19}\z'",
        r"$env:GITHUB_RUN_ATTEMPT -cnotmatch '\A[1-9][0-9]{0,19}\z'",
        "$runner.Name -cne 'K5-Vision'",
        "$workspace.Name -cne 'K5-Vision'",
        "$workspace.Parent.FullName -cne $runner.FullName",
        "$temporary.Name -cne '_temp'",
        "$temporary.Parent.FullName -cne $runner.Parent.FullName",
        "Assert-K5OrdinaryPath $env:RUNNER_TEMP $true",
    ):
        assert required in body


def test_source_paths_reject_aliases_noncanonical_paths_reparse_and_hardlinks():
    body = section("function Assert-K5OrdinaryPath", "function Open-K5PinnedFile")
    for required in (
        "$Path.Length -gt 1024",
        r"$Path -cnotmatch '\A[A-Z]:\\'",
        "$Path.Substring(2).Contains(':')",
        "[IO.Path]::GetFullPath($Path) -cne $Path",
        "$part -in @('.', '..')",
        "$part.EndsWith('.')",
        "$part.EndsWith(' ')",
        "$item.FullName -cne $Path",
        "$count -gt 32",
        "[IO.FileAttributes]::ReparsePoint",
        "[string]::IsNullOrEmpty($item.LinkType)",
        "$item.Directory",
        "$item.Parent",
    ):
        assert required in body


def test_pinned_source_reads_are_bounded_locked_exact_utf8_and_in_memory():
    body = section("function Open-K5PinnedFile", "function Assert-K5CompilerIdle")
    for required in (
        "[IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read",
        "$stream.Length -le 0 -or $stream.Length -gt $Limit",
        "$stream.ReadByte() -ne -1",
        "$stream.Length -ne $bytes.Length",
        "$sha256.ComputeHash($bytes)",
        "$actual -cne $Digest",
        "return @{ stream = $stream; bytes = $bytes }",
    ):
        assert required in body
    assert body.count("Assert-K5OrdinaryPath $Path $false") == 2
    assert "[Text.UTF8Encoding]::new($false, $true)" in TEXT
    assert "$collectorText = $strictUtf8.GetString($collectorFile.bytes)" in TEXT
    assert "$guard = [scriptblock]::Create($strictUtf8.GetString($guardFile.bytes))" in TEXT


def test_original_admission_surrounds_compilation_and_observation():
    main = TEXT.split("$guard = $null", 1)[1]
    ordered = (
        "& $guard > $null",
        "Assert-K5CompilerIdle",
        "Open-K5PinnedFile $collectorPath",
        "Assert-K5InboxCompiler",
        "New-K5CompilerDirectory $compilerTemp",
        "Add-K5PinnedCollector $collectorText $compilerTemp",
        "Assert-K5CompilerIdle",
        "[K5FixedGitObservation]::ObserveGuarded()",
        "Assert-K5DiagnosticRecord $record $context",
        "} finally {",
        "& $guard > $null",
        "Assert-K5CompilerIdle",
        "$postPassed = $true",
    )
    offset = 0
    for marker in ordered:
        offset = main.index(marker, offset) + len(marker)
    assert TEXT.count("& $guard > $null") == 2
    assert main.index("foreach ($file in @($collectorFile, $guardFile))") > offset
    assert main.index("if ($failed -or -not $postPassed") > offset
    assert TEXT.count("[K5FixedGitObservation]::ObserveGuarded()") == 1
    assert main.count("Assert-K5CompilerIdle") == 3


def test_final_artifact_admission_never_scans_inside_observation_process():
    main = TEXT.split("$guard = $null", 1)[1]
    assert "Assert-K5CompilerArtifacts" not in main
    # The unused checker may remain, but no function or top-level call can invoke
    # it before this PowerShell process exits. Directories remain inadmissible at
    # final admission; their transient presence establishes no provenance.
    assert TEXT.count("Assert-K5CompilerArtifacts") == 1
    assert "function Assert-K5CompilerArtifacts([string]$Path)" in TEXT
    assert TEXT.count("EnumerateFileSystemEntries") == 1
    assert "after this PowerShell process exits" in TEXT


def test_compiler_idle_gate_is_bounded_names_only_and_never_signals_processes():
    body = section("function Assert-K5CompilerIdle", "function Assert-K5InboxCompiler")
    assert "-ClassName Win32_Process -Property Name -OperationTimeoutSec 10" in body
    assert "$rows.Count -lt 1 -or $rows.Count -gt 32768" in body
    assert "$row.Name -isnot [string]" in body
    assert "$row.Name.Length -gt 260" in body
    assert "@('csc.exe', 'cvtres.exe', 'vbc.exe', 'VBCSCompiler.exe')" in body
    assert "throw 'compiler_occupied'" in body
    for forbidden in ("ProcessId", "ExecutablePath", "CommandLine", "Get-Process", "Kill("):
        assert forbidden not in body


def test_compilation_selects_existing_inbox_desktop_x64_without_new_attestation():
    body = section("function Assert-K5InboxCompiler", "function New-K5CompilerDirectory")
    for required in (
        "$PSVersionTable.PSEdition -cne 'Desktop'",
        "$PSVersionTable.PSVersion.Major -ne 5",
        "$PSVersionTable.PSVersion.Minor -ne 1",
        "[Environment]::Is64BitProcess",
        "[Environment]::Is64BitOperatingSystem",
        "[Runtime.InteropServices.RuntimeEnvironment]::GetRuntimeDirectory()",
        r"$runtime -cne 'C:\Windows\Microsoft.NET\Framework64\v4.0.30319'",
        "$compilerPath = Join-Path $runtime 'csc.exe'",
        "$compiler.FullName -cne $compilerPath",
        "[IO.FileAttributes]::ReparsePoint",
        "('K5FixedGitObservation' -as [type])",
        "throw 'type_reuse'",
    ):
        assert required in body
    for forbidden in ("LinkType", "Get-FileHash", "Open-K5PinnedFile", "Authenticode", "nlink"):
        assert forbidden not in body
    assert "Existing inbox .NET/compiler trust is an explicit platform assumption" in body


def test_system_core_dependency_is_fixed_inbox_path_checked_like_compiler():
    body = section("function Assert-K5InboxCompiler", "function New-K5CompilerDirectory")
    for required in (
        "$systemCorePath = Join-Path $runtime 'System.Core.dll'",
        "$systemCore = Get-Item -LiteralPath $systemCorePath -Force -ErrorAction Stop",
        "$systemCore.FullName -cne $systemCorePath -or $systemCore.PSIsContainer",
        "($systemCore.Attributes -band [IO.FileAttributes]::ReparsePoint)",
        "return $systemCorePath",
    ):
        assert required in body
    main = TEXT.split("$guard = $null", 1)[1]
    assert "$systemCorePath = Assert-K5InboxCompiler" in main
    assert "Add-K5PinnedCollector $collectorText $compilerTemp $systemCorePath" in main
    compile_body = section("function Add-K5PinnedCollector", "function Get-K5FailureProjection")
    assert "[string]$SystemCorePath" in compile_body
    assert "-ReferencedAssemblies $SystemCorePath" in compile_body
    assert TEXT.count("-ReferencedAssemblies") == 1
    assert "HashSet<string>" in COLLECTOR.read_text(encoding="utf-8")
    for forbidden in (
        "Assert-K5OrdinaryPath $systemCorePath",
        "-AssemblyName",
        "LoadWithPartialName",
        "Assembly]::Load",
        "Assembly]::LoadFrom",
        "Get-ChildItem",
        "GAC",
    ):
        assert forbidden not in TEXT


def test_compiler_temp_is_new_exact_private_directory_without_overwrite():
    assert "$compilerTemp = Join-Path $bundleRoot 'compiler-temp'" in TEXT
    body = section("function New-K5CompilerDirectory", "function Assert-K5CompilerArtifacts")
    for required in (
        "Assert-K5OrdinaryPath $parent $true",
        "[IO.File]::Exists($Path)",
        "[IO.Directory]::Exists($Path)",
        "Test-Path -LiteralPath $Path -ErrorAction Stop",
        "throw 'compiler_temp_exists'",
        "New-Item -Path $Path -ItemType Directory -ErrorAction Stop",
        "$created.FullName -cne $Path",
        "Assert-K5OrdinaryPath $Path $true",
    ):
        assert required in body
    assert "-Force" not in body
    assert "CreateDirectory(" not in body
    assert TEXT.count("New-Item") == 1


def test_compilation_receives_only_pinned_text_and_restores_temp_in_finally():
    body = section("function Add-K5PinnedCollector", "function Get-K5FailureProjection")
    assert "Assert-K5OrdinaryPath $CompilerTemp $true" in body
    for name in ("TEMP", "TMP"):
        assert f"GetEnvironmentVariable('{name}', 'Process')" in body
        assert f"SetEnvironmentVariable('{name}', $CompilerTemp, 'Process')" in body
    assert (
        "Add-Type -TypeDefinition $Source -Language CSharp -PassThru"
        " -ReferencedAssemblies $SystemCorePath -ErrorAction Stop"
    ) in body
    assert "-WarningAction SilentlyContinue -Verbose:$false -Debug:$false" in body
    tail = body.split("} finally {", 1)[1]
    assert "SetEnvironmentVariable('TEMP', $previousTemp, 'Process')" in tail
    assert "SetEnvironmentVariable('TMP', $previousTmp, 'Process')" in tail
    assert tail.count("catch { if ($null -eq $restoreError) { $restoreError = $_ } }") == 2
    assert "if ($null -ne $restoreError -and $null -eq $compileError)" in tail
    assert (
        "[Management.Automation.RuntimeException]::new('compiler_environment',"
        " $restoreError.Exception)"
    ) in tail
    assert "-Path" not in body
    assert "-OutputAssembly" not in body
    assert "'Machine'" not in TEXT


def test_compile_error_record_and_original_exception_survive_restore_failure():
    body = section("function Add-K5PinnedCollector", "function Get-K5FailureProjection")
    assert "$compileError = $null" in body
    assert "$restoreError = $null" in body
    assert re.search(r"} catch \{\s*\$compileError = \$_\s*throw\s*} finally \{", body)
    assert "throw $compileError" not in body
    assert "throw $_" not in body
    assert "if ($restoreFailed)" not in body
    tail = body.split("} finally {", 1)[1]
    restore_gate = tail.index("if ($null -ne $restoreError -and $null -eq $compileError)")
    assert tail.index("SetEnvironmentVariable('TEMP', $previousTemp, 'Process')") < restore_gate
    assert tail.index("SetEnvironmentVariable('TMP', $previousTmp, 'Process')") < restore_gate
    assert tail.count("throw ") == 1


def test_failure_projection_is_exact_closed_and_never_formats_error_objects():
    body = section("function Get-K5FailureProjection", "function Assert-K5RecordKeys")
    expected = {
        "context",
        "path",
        "size",
        "read",
        "hash",
        "binding",
        "compiler_inventory",
        "compiler_occupied",
        "runtime",
        "type_reuse",
        "compiler_temp_exists",
        "compiler_temp",
        "compiler_artifacts",
        "compiled_type",
        "compiler_environment",
        "record",
    }
    assert "[Management.Automation.ErrorRecord]$FailureError" in body
    known = body.split("$knownReasons = @(", 1)[1].split(")", 1)[0]
    assert set(re.findall(r"'([a-z_]+)'", known)) == expected
    assert "$reason = 'unknown'" in body
    assert "$compilerCode = 'unknown'" in body
    assert "$FailureError.Exception.Message -ceq $knownReason" in body
    assert "$reason = $knownReason" in body
    assert "$FailureError.TargetObject -is [System.CodeDom.Compiler.CompilerError]" in body
    assert "$number = $FailureError.TargetObject.ErrorNumber" in body
    assert r"$number -is [string] -and $number -cmatch '\ACS[0-9]{4}\z'" in body
    assert "$compilerCode = $number" in body
    assert "return @{ reason = $reason; compiler_code = $compilerCode }" in body
    for forbidden in (
        ".ToString(",
        "Out-String",
        "ErrorDetails",
        "InvocationInfo",
        "ScriptStackTrace",
        "FileName",
        "FullyQualifiedErrorId",
        "ErrorText",
        "Write-Host",
        "Write-Error",
        "ConvertTo-Json",
        "-match ",
        "-like ",
    ):
        assert forbidden not in body
    # Source-contract model only: known messages can select a literal; no text
    # fragment, arbitrary target, or loosely shaped compiler ID can escape.
    def project(message, number, typed):
        reason = next((item for item in expected if message == item), "unknown")
        code = "unknown"
        if typed and isinstance(number, str) and re.fullmatch(r"CS[0-9]{4}", number):
            code = number
        return reason, code

    for reason in expected:
        assert project(reason, None, False) == (reason, "unknown")
        assert project(reason.upper(), None, False) == ("unknown", "unknown")
        assert project(reason + "\nprivate-body", None, False) == ("unknown", "unknown")
    assert project(r"C:\private\token-CS0246.cs", "CS0246", True) == ("unknown", "CS0246")
    for number in ("CS0246", "CS0000", "CS9999"):
        assert project("unknown", number, False) == ("unknown", "unknown")
    for number in (
        None, 246, "cs0246", "CS246", "CS00246", "CS0246\n", "CS0246 private", "CS１２３４"
    ):
        assert project("unknown", number, True) == ("unknown", "unknown")


def test_first_error_and_phase_are_preserved_through_mandatory_post_checks():
    main = TEXT.split("$guard = $null", 1)[1]
    assert "$failureError = $null" in main
    first_catch = main.split("} catch {\n    # No exception text", 1)[1].split("} finally {", 1)[0]
    assert "$failureError = $_" in first_catch
    finalization = main.split("} finally {", 1)[1].split("if ($failed -or -not $postPassed", 1)[0]
    for phase in ("post_admission", "file_cleanup"):
        assert re.search(
            r"if \(\$null -eq \$failureError\) \{\s*\$failureError = \$_\s*"
            + re.escape(f"$failure = '{phase}'") + r"\s*}",
            finalization,
        )
    failure = main.split("if ($failed -or -not $postPassed", 1)[1]
    assert "$projection = Get-K5FailureProjection $failureError" in failure
    assert "reason = $projection.reason; compiler_code = $projection.compiler_code" in failure
    assert "code = $failure" in failure
    for forbidden in ("$failureError.Exception", "$failureError.TargetObject", "$_", "Out-String"):
        assert forbidden not in failure


def test_compiler_artifact_acceptance_is_bounded_shallow_and_preserves_files():
    body = section("function Assert-K5CompilerArtifacts", "function Add-K5PinnedCollector")
    for required in (
        "[IO.Directory]::EnumerateFileSystemEntries($Path).GetEnumerator()",
        "$entries.MoveNext()",
        "$count -gt 64",
        "Assert-K5OrdinaryPath $entry $false",
        "$bytes -gt 16777216",
        "$entries.Dispose()",
        "not a disk quota",
    ):
        assert required in body
    assert "-Recurse" not in TEXT
    assert "Remove-Item" not in TEXT
    assert ".Delete(" not in TEXT


def test_normal_or_failed_results_always_require_separate_final_admission():
    assert "short Actions step timeout" in TEXT
    assert "20-second current-step watchdog" in TEXT
    assert "SEPARATE always-run original" in TEXT
    assert "$sanitized['requires_separate_post_admission'] = $true" in TEXT
    assert "$sanitized['compiler_artifacts_retained'] = $true" in TEXT
    failure = TEXT.split("if ($failed -or -not $postPassed", 1)[1]
    assert "requires_separate_post_admission = $true" in failure
    assert "compiler_artifacts_retained = $true" in failure
    assert "storage_admission = $false" in failure
    assert "retry_authority = $false" in failure
    assert "exception_authority = $false" in failure
    assert "if ($failed -or -not $postPassed -or $null -eq $result)" in TEXT
    assert "if ($result.status -cne 'observed')" in failure
    assert failure.count("throw 'Installed Git link observation refused.") == 2


def wrapper_acceptance_model(errors=None, collector_status="observed", separate_post="success"):
    """Source-bound phase model, not PowerShell execution or physical evidence."""
    errors = errors or {}
    main = TEXT.split("$failure = 'pre_admission'", 1)[1]
    observation = main.split("} catch {\n    # No exception text", 1)[0]
    phases = ["pre_admission", *re.findall(r"\$failure = '([a-z_]+)'", observation)]
    assert phases == [
        "pre_admission", "compiler_idle_before", "collector_binding", "runtime_binding",
        "compiler_temp", "collector_compile", "compiler_idle_after", "collector_observation",
        "record_validation",
    ]
    first_error = None
    first_phase = None
    result = None
    seen = []
    for phase in phases:
        seen.append(phase)
        if phase in errors:
            first_error, first_phase = errors[phase], phase
            break
        if phase == "record_validation":
            result = collector_status
    # The original guard and compiler-idle finalization still run. Any first
    # error remains primary when host admission or handle cleanup also fails.
    for phase in ("post_admission", "file_cleanup"):
        seen.append(phase)
        if phase in errors and first_error is None:
            first_error, first_phase = errors[phase], phase
    wrapper_passed = first_error is None and result == "observed"
    return {
        "status": "observed" if wrapper_passed else "refused",
        "first_error": first_error,
        "first_phase": first_phase,
        "seen": seen,
        "requires_separate_post_admission": True,
        "accepted": wrapper_passed and separate_post == "success",
    }


@pytest.mark.parametrize(
    "separate_post", [None, "pending", "failure", "skipped", "cancelled", "timed_out", "success"]
)
def test_observed_model_is_provisional_until_independent_post_succeeds(separate_post):
    result = wrapper_acceptance_model(separate_post=separate_post)
    assert result["status"] == "observed"
    assert result["requires_separate_post_admission"] is True
    assert result["accepted"] is (separate_post == "success")


@pytest.mark.parametrize(
    "phase",
    ["pre_admission", "compiler_idle_before", "collector_compile", "compiler_idle_after",
     "collector_observation", "record_validation", "post_admission", "file_cleanup"],
)
def test_model_never_accepts_failed_compile_admission_observation_or_cleanup(phase):
    error_record = object()
    result = wrapper_acceptance_model({phase: error_record})
    assert result["first_error"] is error_record
    assert result["first_phase"] == phase
    assert result["status"] == "refused"
    assert result["accepted"] is False
    assert result["seen"][-2:] == ["post_admission", "file_cleanup"]
    if phase in ("pre_admission", "compiler_idle_before", "collector_compile", "compiler_idle_after"):
        assert "collector_observation" not in result["seen"]


@pytest.mark.parametrize("phase", ["pre_admission", "collector_compile", "post_admission"])
def test_model_preserves_first_error_record_when_finalization_also_fails(phase):
    first_error = object()
    errors = {"post_admission": object(), "file_cleanup": object(), phase: first_error}
    result = wrapper_acceptance_model(errors)
    assert result["first_error"] is first_error
    assert result["first_phase"] == phase
    assert result["accepted"] is False


def test_model_cannot_upgrade_collector_refusal_with_successful_final_post():
    result = wrapper_acceptance_model(collector_status="refused", separate_post="success")
    assert result["status"] == "refused"
    assert result["accepted"] is False


def test_native_record_dictionary_schema_flags_context_and_output_are_closed():
    body = section("function Assert-K5DiagnosticRecord", "$guard = $null")
    for required in (
        "[Collections.Generic.Dictionary[string,object]]",
        "$Record['status'] -cnotin @('observed', 'refused')",
        "Assert-K5RecordKeys $Record $keys",
        "$Record['schema_version'] -cne 'fixed-git-hardlink-observation-v2'",
        "$Record['scope'] -cne 'fixed-git-readonly-metadata'",
        "$Record['target'] -cne 'cmd/git.exe'",
        "@('exception_authority', 'storage_admission', 'retry_authority')",
        "$Record[$key] -isnot [bool] -or $Record[$key] -ne $false",
        "$Record['closure_complete'] -isnot [bool]",
        "$sanitized['source_sha'] = $Context.source_sha",
        "$sanitized['run_id'] = $Context.run_id",
        "$sanitized['run_attempt'] = $Context.run_attempt",
        "ConvertTo-Json -InputObject $sanitized -Compress -Depth 5",
        "$strictUtf8.GetByteCount($text) -gt 65536",
    ):
        assert required in body
    keys = section("function Assert-K5RecordKeys", "function Assert-K5Integer")
    assert "$Record.Count -ne $Expected.Count" in keys
    assert "$key -cnotin $Expected" in keys
    assert TEXT.count("Write-Host") == 2
    assert "Write-Error" not in TEXT
    assert "$_" not in TEXT.split("if ($failed -or -not $postPassed", 1)[1]


def test_refusals_are_allowlisted_and_bound_below_watchdog_budget():
    body = section("function Assert-K5DiagnosticRecord", "$guard = $null")
    codes = section("$codes = @(", ")\n        if ($Record['closure_complete']")
    assert set(re.findall(r"'([a-z_]+)'", codes)) == REFUSALS
    csharp = COLLECTOR.read_text(encoding="utf-8")
    assert all(f'"{code}"' in csharp for code in REFUSALS)
    assert "$Record['closure_complete'] -ne $false" in body
    assert "$Record['code'] -cnotin $codes" in body
    assert "Assert-K5Integer $Record['bytes_read'] 0 134217728" in body
    assert "Assert-K5Integer $Record['elapsed_ms'] 0 19999" in body
    assert "$Record['closure_complete'] -ne $true -or $Record['code'] -cne 'none'" in body


def test_observed_aliases_are_bounded_consistent_and_never_acl_admission():
    body = section("function Assert-K5DiagnosticRecord", "$guard = $null")
    for required in (
        "Assert-K5Integer $Record['link_count'] 1 16",
        "Assert-K5Integer $Record['bytes_read'] 1 134217728",
        "Assert-K5Integer $Record['elapsed_ms'] 0 8000",
        "$aliases -isnot [Collections.IList]",
        "$aliases.Count -ne $Record['link_count']",
        "$alias['relative_name'].Length -gt 1024",
        r"'\A[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\z'",
        r"$alias['ntfs_volume_serial'] -cnotmatch '\A[0-9a-f]{8}\z'",
        r"$alias['ntfs_file_id'] -cnotmatch '\A[0-9a-f]{16}\z'",
        "$alias['sha256'] -cne $Record['primary_sha256']",
        "$seen.ContainsKey($alias['relative_name'])",
        "Assert-K5Integer $alias['size_bytes'] 1 33554432",
        "$alias['link_count'] -ne $Record['link_count']",
        "@('ntfs_volume_serial', 'ntfs_file_id', 'acl_sha256', 'size_bytes')",
        "$seen.ContainsKey('cmd/git.exe')",
        "$Record['bytes_read'] -ne ($first['size_bytes'] * ($Record['link_count'] + 2))",
        "A readable owner/DACL fingerprint is evidence only, never ACL admission",
    ):
        assert required in body


@pytest.mark.parametrize(
    "forbidden",
    [
        "python",
        "Python",
        "base64",
        "ProcessStartInfo",
        "Process]::new",
        "Get-Command",
        "Start-Process",
        "Get-Process",
        "Stop-Process",
        "GetProcessById",
        "taskkill",
        ".Kill(",
        "Environment]::Exit",
        "Invoke-Expression",
        "Invoke-WebRequest",
        "Invoke-RestMethod",
        "Set-Acl",
        "icacls",
        "takeown",
        "Set-Item",
        "Remove-Item",
        "Out-File",
        "WriteAll",
        "GITHUB_OUTPUT",
        "GITHUB_PATH",
        "GITHUB_ENV",
        "$env:PATH =",
        "$git =",
        "git -C",
        "actions/checkout",
        "Start-Job",
        "-Verb RunAs",
        "ReadToEnd",
        "Invoke-K5OwnedCollector",
    ],
)
def test_no_removed_runtime_transport_or_unowned_mutating_fallback(forbidden):
    assert forbidden not in TEXT


def test_source_only_snapshot_digest_is_reproducible():
    assert WRAPPER.read_bytes().decode("utf-8") == TEXT
    print("WRAPPER_SHA256=" + hashlib.sha256(WRAPPER.read_bytes()).hexdigest())
    print("WRAPPER_TEST_SHA256=" + hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
