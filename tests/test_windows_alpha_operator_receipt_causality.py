"""Fail-closed Windows operator receipt causality regression; no camera media."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

START = Path(__file__).resolve().parents[1] / "scripts" / "windows-alpha" / "Start-K5VisionAlpha.ps1"


def test_windows_receipt_submissions_cannot_exceed_delivered_frames() -> None:
    text = START.read_text(encoding="utf-8")
    gate = text.split("function Test-K5AlphaOperatorReceipt", 1)[1].split("\n$python =", 1)[0]
    assert "$Receipt.analytics_provider_completions -gt $Receipt.analytics_provider_submissions" in gate
    assert "$Receipt.analytics_provider_submissions -gt $Receipt.delivered_frames" in gate


@pytest.mark.skipif(sys.platform != "win32", reason="Requires Windows PowerShell 5.1")
def test_windows_receipt_rejects_impossible_submission_count() -> None:
    script = r"""
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile('__START__', [ref]$tokens, [ref]$errors)
if ($errors.Count -ne 0) { throw 'Launcher parser refused script.' }
$function = $ast.Find({ param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
    $node.Name -eq 'Test-K5AlphaOperatorReceipt'
}, $true)
if ($null -eq $function) { throw 'Receipt guard is missing.' }
. ([scriptblock]::Create($function.Extent.Text))
$receipt = [pscustomobject]@{
    completed = $true
    delivered_frames = [long]225
    presentations = [long]225
    analytics_enabled = $true
    analytics_provider_submissions = [long]225
    analytics_provider_completions = [long]1
    analytics_rendered_boxes = [long]1
    analytics_failures = [long]0
}
if (-not (Test-K5AlphaOperatorReceipt -Receipt $receipt -AnalyticsRequired $true)) {
    throw 'Valid equal-bound receipt refused.'
}
$receipt.analytics_provider_submissions = [long]226
if (Test-K5AlphaOperatorReceipt -Receipt $receipt -AnalyticsRequired $true) {
    throw 'Impossible analytics submissions admitted.'
}
"""
    script = script.replace("__START__", str(START).replace("'", "''"))
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        check=False, capture_output=True, text=True, timeout=45,
    )
    assert result.returncode == 0, result.stdout + result.stderr
