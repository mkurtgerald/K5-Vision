"""Regression checks for the camera-free installed Windows alpha witness boundary."""

from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ALPHA = ROOT / "scripts" / "windows-alpha"
WITNESS = ALPHA / "Invoke-K5VisionAlphaWitness.ps1"
WORKFLOW = ROOT / ".github" / "workflows" / "stage-one-operator-physical.yml"


def test_baseline_launcher_identity_is_pinned_independently_of_candidate_source() -> None:
    text = WITNESS.read_text()
    assert '$reviewedBaselineRevision = "d531d50d479f46af6ceed324a7cc379745becb61"' in text
    assert "$revision -cne $reviewedBaselineRevision" in text
    assert "$expectedLauncherSize = 20863" in text
    assert '$expectedLauncherBlob = "fe1ca98340a6967de0fa92a86dd361efa6dc6805"' in text
    assert (
        '$expectedLauncherHash = "e63fa030ec254bd4b8fa83cf90abd08fdfe351bf6fb4c40a215b1ad7bd60dd55"'
        in text
    )
    assert "$expectedLauncherHash = Get-K5CanonicalHash" not in text


def test_baseline_validates_raw_launcher_bytes_before_install_and_after_copy() -> None:
    text = WITNESS.read_text()
    validation = (
        "Assert-K5PinnedLauncherBytes -Path $payloadLauncher -ExpectedSize $expectedLauncherSize "
        "-ExpectedBlob $expectedLauncherBlob -ExpectedSha256 $expectedLauncherHash"
    )
    assert validation in text
    assert text.index(validation) < text.index("$null = Invoke-K5Bounded $hostExe")
    installed = (
        "Assert-K5PinnedLauncherBytes -Path $installedLauncher -ExpectedSize $expectedLauncherSize "
        "-ExpectedBlob $expectedLauncherBlob -ExpectedSha256 $expectedLauncherHash"
    )
    assert installed in text
    assert text.index(installed) < text.index("foreach ($attempt in 1..2)")
    helper = text.split("function Assert-K5PinnedLauncherBytes", 1)[1].split(
        "function Invoke-K5Bounded", 1
    )[0]
    assert "[IO.File]::Open($Path" in helper
    assert "[IO.FileShare]::Read" in helper
    assert "$stream.Length -ne $ExpectedSize" in helper
    assert "$stream.Read($bytes, $offset, $ExpectedSize - $offset)" in helper
    assert "[Security.Cryptography.SHA256]::Create()" in helper
    assert "[Security.Cryptography.SHA1]::Create()" in helper
    assert '$header = [Text.Encoding]::ASCII.GetBytes("blob " + $ExpectedSize + [char]0)' in helper
    assert 'Replace("`r`n"' not in helper


@pytest.mark.skipif(sys.platform != "win32", reason="Requires Windows PowerShell")
def test_windows_baseline_byte_validator_rejects_crlf_and_tampering(tmp_path: Path) -> None:
    payload = b"reviewed launcher\n"
    path = tmp_path / "baseline.ps1"
    path.write_bytes(payload)
    sha256 = hashlib.sha256(payload).hexdigest()
    blob = hashlib.sha1(b"blob " + str(len(payload)).encode() + b"\0" + payload).hexdigest()
    script = r"""
$ErrorActionPreference = 'Stop'
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    '__WITNESS__', [ref]$tokens, [ref]$errors)
if ($errors.Count -ne 0) { throw 'parse' }
$function = $ast.Find({ param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
    $node.Name -eq 'Assert-K5PinnedLauncherBytes'
}, $true)
if ($null -eq $function) { throw 'missing helper' }
. ([scriptblock]::Create($function.Extent.Text))
$path = '__PAYLOAD__'
$arguments = @{
    Path=$path; ExpectedSize=__SIZE__; ExpectedBlob='__BLOB__'; ExpectedSha256='__SHA__'
}
Assert-K5PinnedLauncherBytes @arguments
foreach ($text in @("reviewed launcher`r`n", "changed! launcher`n")) {
    $bytes = [Text.Encoding]::ASCII.GetBytes($text)
    [IO.File]::WriteAllBytes($path, $bytes)
    try { Assert-K5PinnedLauncherBytes @arguments; throw 'tamper accepted' }
    catch { if ($_.Exception.Message -ne 'launcher_mismatch') { throw } }
}
[IO.File]::WriteAllBytes($path, [Text.Encoding]::ASCII.GetBytes("reviewed launcher`n"))
$arguments.ExpectedBlob = '0000000000000000000000000000000000000000'
try { Assert-K5PinnedLauncherBytes @arguments; throw 'blob mismatch accepted' }
catch { if ($_.Exception.Message -ne 'launcher_mismatch') { throw } }
"""
    for key, value in {
        "__WITNESS__": str(WITNESS).replace("'", "''"),
        "__PAYLOAD__": str(path).replace("'", "''"),
        "__SIZE__": str(len(payload)),
        "__BLOB__": blob,
        "__SHA__": sha256,
    }.items():
        script = script.replace(key, value)
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_witness_reuses_existing_physical_job_and_source_free_artifact() -> None:
    workflow = WORKFLOW.read_text()
    assert workflow.count("runs-on:") == 1
    assert "runs-on: [self-hosted, Windows, X64, k5-physical, camera-lab]" in workflow
    assert "Invoke-K5VisionAlphaWitness.ps1 -Output $env:K5_ALPHA_DIRECT_RTSP_OUTPUT" in workflow
    assert workflow.count("$env:K5_ALPHA_DIRECT_RTSP_OUTPUT") == 2
    assert "artifacts/alpha-direct-rtsp-physical.json" in workflow
    assert "timeout-minutes: 10" in workflow
    assert "workflow_dispatch:\n\n" in workflow


def test_witness_installs_exact_payload_and_checks_installed_decoder() -> None:
    text = WITNESS.read_text()
    assert '"Install-K5VisionAlpha.ps1"' in text
    assert '"-K5Revision", $revision, "-SkipDesktopShortcut"' in text
    assert "https://github.com/mkurtgerald/K5-Vision/archive/$revision.zip" in text
    assert "-UseBasicParsing -TimeoutSec 30" in text
    assert 'Join-Path $payloadRoot "scripts\\windows-alpha\\Install-K5VisionAlpha.ps1"' in text
    assert "$installedLauncherHash -ne $expectedLauncherHash" in text
    assert '"k5-revision.txt"' in text
    assert 'Join-Path $installRoot ".venv\\Scripts\\python.exe"' in text
    assert "import k5vision.media.gstreamer_direct_frame_delivery as m" in text
    assert "if ($actualHash -ne $expectedHash)" in text
    assert 'Invoke-K5Bounded $inspect @("d3d11h264dec") 15' in text
    assert "decoder_version_mismatch" in text


def test_witness_restores_process_environment_without_job_exports() -> None:
    text = WITNESS.read_text()
    for name in ("RUNNER_TOOL_CACHE", "GITHUB_ENV", "GITHUB_PATH", "RUNNER_TEMP", "TEMP", "TMP"):
        assert f'"{name}"' in text
    assert '$preserved[$name] = [Environment]::GetEnvironmentVariable($name, "Process")' in text
    assert '[Environment]::SetEnvironmentVariable($name, $preserved[$name], "Process")' in text
    assert '"K5RunnerTools\\k5-gstreamer\\1.28.7\\msvc_x86_64\\bin"' in text
    assert "Add-Content" not in text


def test_witness_keeps_generated_launcher_and_reentry_cleanup_bounds() -> None:
    text = WITNESS.read_text()
    assert "foreach ($attempt in 1..2)" in text
    assert 'Join-Path $installRoot "Run-K5VisionAlpha.ps1"' in text
    assert '"-Port", $port, "-ExitAfterPublicTest") 90' in text
    assert '"-PublicRtspSource"' not in text
    assert "[System.Net.IPAddress]::Loopback, 0" in text
    assert "Exiting after one bounded alpha acceptance run" in text
    assert "session_cleanup_incomplete" in text
    assert "Remove-Item -LiteralPath $workRoot -Recurse -Force" in text
    assert "if (-not $completed) { throw" in text
    shipped = (ALPHA / "Start-K5VisionAlpha.ps1").read_text()
    assert "Local synthetic RTSP port 8554 is already in use." in shipped
    assert "Remove-Item Env:K5_STAGE_ONE_RECORDING_ROOT" in shipped
    assert "if ($ExitAfterPublicTest)" in shipped


def test_witness_only_terminates_its_own_bounded_child_tree() -> None:
    text = WITNESS.read_text()
    assert "$child = Start-Process" in text
    assert text.index("$childHandle = $child.Handle") < text.index("$child.WaitForExit(")
    assert "$child.WaitForExit()" not in text
    assert "$child.WaitForExit($Seconds * 1000)" in text
    assert "if (-not $child.HasExited)" in text
    assert "taskkill.exe /PID $child.Id /T /F" in text
    assert "$child.WaitForExit(5000)" in text
    assert "Stop-Process" not in text
    assert "Get-CimInstance" not in text
    assert "taskkill.exe /IM" not in text


@pytest.mark.parametrize(
    ("log", "accepted"),
    [
        ("K5 operator PASS: frames=225, presentations=225\r\n", True),
        ("K5 Vision Alpha health check PASS.\n", False),
        ("K5 operator PASS: frames=0, presentations=0\n", False),
        ("K5 operator PASS: frames=225, presentations=0\n", False),
        ("K5 operator PASS: frames=224, presentations=225\n", False),
        ("K5 operator PASS: frames=225, presentations=225\n" * 2, False),
    ],
)
def test_witness_receipt_expression_rejects_health_only_or_zero_frames(
    log: str, accepted: bool
) -> None:
    text = WITNESS.read_text()
    pattern = re.search(r"\$receipts = \[regex\]::Matches\(\$text, '([^']+)'\)", text)
    assert pattern is not None
    matches = re.findall(pattern.group(1), log)
    result = len(matches) == 1 and int(matches[0][0]) == 225 and int(matches[0][1]) >= 225
    assert result is accepted
    assert "$receipts.Count -ne 1" in text
    assert "$frames -ne 225 -or $presentations -lt 225" in text


def test_witness_retains_only_sanitized_evidence() -> None:
    text = WITNESS.read_text()
    evidence = text.split("$evidence = @{", 1)[1].split("\n    }", 1)[0]
    assert 'scope = "installed_local_synthetic_direct_rtsp"' in evidence
    assert "runs = $runs; cleanup_complete = $cleaned" in evidence
    for variable in ("$text", "$inventory", "$workRoot", "$installRoot", "$preserved", "$stdout"):
        assert variable not in evidence
    assert 'else { "unexpected" }' in text
    assert "Remove-Item -LiteralPath $stdout, $stderr -Force" in text
    assert "rtsp://" not in text


@pytest.mark.skipif(sys.platform != "win32", reason="Requires the Windows PowerShell process API")
def test_windows_bounded_child_success_nonzero_timeout(tmp_path: Path) -> None:
    # Extract only the production helper AST; do not install K5, start media, or
    # execute the witness's top-level physical workflow in this script-smoke test.
    witness = str(WITNESS).replace("'", "''")
    temporary = str(tmp_path).replace("'", "''")
    script = r"""
$ErrorActionPreference = "Stop"
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    '__WITNESS__', [ref]$tokens, [ref]$parseErrors
)
if ($parseErrors.Count -ne 0) { throw "Witness parse failed." }
$function = $ast.Find({ param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
    $node.Name -eq 'Invoke-K5Bounded'
}, $true)
if ($null -eq $function) { throw "Bounded helper is missing." }
. ([scriptblock]::Create($function.Extent.Text))
$workRoot = '__TEMPORARY__'
$hostExe = (Get-Process -Id $PID).Path
$successArgs = @('-NoProfile', '-NonInteractive', '-Command',
                 '"Write-Output native-success; exit 0"')
$ok = Invoke-K5Bounded $hostExe $successArgs 10
if ($ok.Trim() -ne 'native-success') { throw "Successful child was not captured." }
try {
    $null = Invoke-K5Bounded $hostExe @('-NoProfile', '-NonInteractive', '-Command', '"exit 7"') 10
    throw 'nonzero_accepted'
} catch {
    if ($_.Exception.Message -ne 'child_failed') { throw }
}
$watch = [Diagnostics.Stopwatch]::StartNew()
try {
    $timeoutArgs = @('-NoProfile', '-NonInteractive', '-Command', '"Start-Sleep -Seconds 30"')
    $null = Invoke-K5Bounded $hostExe $timeoutArgs 1
    throw 'timeout_accepted'
} catch {
    if ($_.Exception.Message -ne 'bounded_child_timeout') { throw }
}
if ($watch.Elapsed.TotalSeconds -gt 10) { throw "Child timeout was not bounded." }
if (@(Get-ChildItem -LiteralPath $workRoot -Filter 'child.*.log').Count -ne 0) {
    throw "Child output cleanup failed."
}
""".replace("__WITNESS__", witness).replace("__TEMPORARY__", temporary)
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
