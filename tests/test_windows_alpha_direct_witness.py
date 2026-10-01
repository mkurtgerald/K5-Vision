"""Regression checks for the camera-free installed Windows alpha witness boundary."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ALPHA = ROOT / "scripts" / "windows-alpha"
WITNESS = ALPHA / "Invoke-K5VisionAlphaWitness.ps1"
WORKFLOW = ROOT / ".github" / "workflows" / "stage-one-operator-physical.yml"


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
