"""Alpha analytics admission and receipt guards, without native media allocation."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ALPHA = ROOT / "scripts" / "windows-alpha"
START = ALPHA / "Start-K5VisionAlpha.ps1"
TEST = ALPHA / "Test-K5VisionAlpha.ps1"


def test_windows_smoke_selects_analytics_and_preserves_lifetime_coverage() -> None:
    workflow = (ROOT / ".github" / "workflows" / "windows-alpha-script-smoke.yml").read_text()
    for path in (
        "tests/test_windows_alpha_analytics.py",
        "tests/test_app_resource_lifetime.py",
        "tests/test_cli.py",
    ):
        assert workflow.count(f'      - "{path}"') == 2
    selection = next(line for line in workflow.splitlines() if line.strip().startswith("pytest "))
    assert "tests/test_windows_alpha_analytics.py" in selection
    assert "tests/test_app_resource_lifetime.py" in selection
    assert "tests/test_cli.py" in selection
    for path in (
        "tests/test_one_file_windows_bootstrap.py",
        "tests/test_windows_alpha_bootstrap.py",
        "tests/test_public_test_operator_runtime.py",
        "tests/test_windows_alpha_direct_witness.py",
    ):
        assert path in selection


def test_analytics_admission_precedes_alpha_state_and_media() -> None:
    text = START.read_text(encoding="utf-8")
    admission = "$analyticsRequired = Invoke-K5AnalyticsPreflight -Python $python"
    assert admission in text
    assert text.index(admission) < text.index("$sessionRoot =")
    assert text.index(admission) < text.index("$synthetic = Start-K5SyntheticSource")
    assert text.index(admission) < text.index("$env:K5_DEVICE_DB_PATH =")
    assert "[switch]$AnalyticsPreflightOnly" in text
    assert text.index("if ($AnalyticsPreflightOnly)") < text.index("$sessionRoot =")


def test_test_script_reuses_installed_launcher_admission_before_native_probe() -> None:
    text = TEST.read_text(encoding="utf-8")
    gate = "& $launcher -AnalyticsPreflightOnly"
    assert gate in text
    assert text.index(gate) < text.index("& $gstLaunch --version")
    assert "& $python -I -B -m k5vision.cli --version" in text
    assert "& $python -I -B -m k5vision.cli --help" in text


def test_preflight_is_bounded_memory_only_and_installed_source_isolated() -> None:
    text = START.read_text(encoding="utf-8")
    helper = text.split("function Invoke-K5AnalyticsPreflight", 1)[1].split(
        "function Test-K5AlphaOperatorReceipt", 1
    )[0]
    assert '"-I -B -m k5vision.cli analytics-preflight"' in helper
    assert "$info.WorkingDirectory = Split-Path -Parent $Python" in helper
    assert "$maximumOutputBytes = 4096" in helper
    assert "[int]$TimeoutSeconds = 30" in helper
    assert "ReadAsync" in helper
    assert "ReadToEnd" not in helper
    assert "WriteAll" not in helper and "New-Item" not in helper
    assert "$child.Kill()" in helper and "$child.WaitForExit(5000)" in helper
    assert "Get-CimInstance" not in helper and "Get-Process" not in helper
    assert '$arguments = @("-I","-B") + $arguments' in text
    assert "-WorkingDirectory (Split-Path -Parent $python)" in text


def test_preflight_admits_only_canonical_typed_success_records() -> None:
    text = START.read_text(encoding="utf-8")
    admitted = re.findall(r"\$json -ceq '([^']+)'", text)
    assert len(admitted) == 2
    expected = [
        {"schema_version": "1", "analytics_enabled": True, "status": "ready"},
        {"schema_version": "1", "analytics_enabled": False, "status": "disabled"},
    ]
    assert admitted == [json.dumps(value, separators=(",", ":")) for value in expected]
    for value in admitted:
        assert f"[{value}]" not in admitted
        assert value.replace('"schema_version":"1"', '"schema_version":1') not in admitted
        assert value[:-1] + ',"status":"disabled"}' not in admitted
        assert value[:-1] + ',"' + chr(92) + 'u0073tatus":"disabled"}' not in admitted


def test_selected_analytics_gate_precedes_any_operator_pass() -> None:
    text = START.read_text(encoding="utf-8")
    gate = "Test-K5AlphaOperatorReceipt -Receipt $receipt -AnalyticsRequired $analyticsRequired"
    assert gate in text
    assert text.index(gate) < text.index('Write-Host ("K5 operator PASS:')
    assert '"K5 analytics PASS: submissions={0}, completions={1}, failures=0"' in text
    helper = text.split("function Test-K5AlphaOperatorReceipt", 1)[1].split("\n$python =", 1)[0]
    for name in (
        "analytics_enabled",
        "analytics_provider_submissions",
        "analytics_provider_completions",
        "analytics_failures",
    ):
        assert name in helper
    assert "analytics_overlays_rendered" not in helper
    assert "person" not in helper


def _quote(path: Path) -> str:
    return str(path).replace("'", "''")


def _load_functions() -> str:
    return r"""
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    '__START__', [ref]$tokens, [ref]$parseErrors
)
if ($parseErrors.Count -ne 0) { throw 'Launcher parse failed.' }
$null = [System.Management.Automation.Language.Parser]::ParseFile(
    '__TEST__', [ref]$tokens, [ref]$parseErrors
)
if ($parseErrors.Count -ne 0) { throw 'Test script parse failed.' }
foreach ($name in @('Invoke-K5AnalyticsPreflight', 'Test-K5AlphaOperatorReceipt')) {
    $function = $ast.Find({ param($node)
        $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -eq $name
    }, $true)
    if ($null -eq $function) { throw 'Analytics guard function missing.' }
    . ([scriptblock]::Create($function.Extent.Text))
}
""".replace("__START__", _quote(START)).replace("__TEST__", _quote(TEST))


def _powershell(script: str, *, timeout: int = 45) -> None:
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(sys.platform != "win32", reason="Requires Windows PowerShell 5.1")
def test_windows_receipts_are_typed_and_fail_closed_without_requiring_boxes() -> None:
    video = {"completed": True, "delivered_frames": 225, "presentations": 225}
    enabled = {
        **video,
        "analytics_enabled": True,
        "analytics_provider_submissions": 2,
        "analytics_provider_completions": 1,
        "analytics_failures": 0,
        "analytics_overlays_rendered": 0,
    }
    cases = [
        (None, True, False),
        (video, False, True),
        (video, True, False),
        (enabled, True, True),
    ]
    for field in ("completed", "delivered_frames", "presentations"):
        for value in (None, False, "1", 1.5, -1, 0):
            cases.append(({**enabled, field: value}, True, False))
    for field in (
        "analytics_enabled",
        "analytics_provider_submissions",
        "analytics_provider_completions",
        "analytics_failures",
    ):
        missing = dict(enabled)
        del missing[field]
        cases.append((missing, True, False))
        for value in (None, "0", "false", -1, 0.5, [], {}):
            cases.append(({**enabled, field: value}, True, False))
    for field in ("delivered_frames", "presentations"):
        for value in (True, 1.0):
            cases.append(({**enabled, field: value}, True, False))
    for field in ("analytics_provider_submissions", "analytics_provider_completions"):
        for value in (False, True, 0):
            cases.append(({**enabled, field: value}, True, False))
    for value in (False, "true", 1):
        cases.append(({**enabled, "analytics_enabled": value}, True, False))
    for value in (True, False, 1):
        cases.append(({**enabled, "analytics_failures": value}, True, False))
    cases.append(({**enabled, "analytics_provider_completions": 3}, True, False))
    data = json.dumps(cases, separators=(",", ":"))
    script = (
        _load_functions()
        + f"\n$cases = ConvertFrom-Json '{data}'\n"
        + r"""
foreach ($case in $cases) {
    $actual = Test-K5AlphaOperatorReceipt -Receipt $case[0] -AnalyticsRequired $case[1]
    if ($actual -isnot [bool] -or $actual -ne $case[2]) {
        throw 'Receipt guard accepted invalid evidence or rejected valid evidence.'
    }
}
"""
    )
    _powershell(script)


@pytest.mark.skipif(sys.platform != "win32", reason="Requires Windows PowerShell process API")
def test_windows_preflight_process_bounds_schema_and_environment(tmp_path: Path) -> None:
    install = tmp_path / "installed runtime"
    venv = install / ".venv"
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(venv)],
        check=True,
        timeout=60,
        capture_output=True,
    )
    (install / "Start-K5VisionAlpha.ps1").write_text(START.read_text(), encoding="utf-8")
    temporary = tmp_path / "session state"
    temporary.mkdir()
    package = venv / "Lib" / "site-packages" / "k5vision"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "cli.py").write_text(
        """import json, os, pathlib, sys, time
assert sys.argv[1:] == ["analytics-preflight"]
assert sys.flags.isolated and sys.flags.dont_write_bytecode
assert pathlib.Path.cwd() == pathlib.Path(sys.executable).parent
assert "PYTHONPATH" not in os.environ and "PYTHONHOME" not in os.environ
assert "K5_STAGE03_SOURCE" not in os.environ and "K5_STAGE03_CAM_CRED" not in os.environ
assert "K5_CONTROL_PLANE_READ_TOKEN" not in os.environ and "CAM_CRED" not in os.environ
assert "UNRELATED_PRIVATE_TOKEN" not in os.environ
mode = os.environ.get("K5_ANALYTICS_CONFIG", "disabled")
receipt = {"schema_version": "1", "analytics_enabled": False, "status": "disabled"}
if mode == "ready": receipt.update(analytics_enabled=True, status="ready")
if mode == "refused":
    receipt["status"] = "refused"
    print(json.dumps(receipt, separators=(",", ":"))); sys.exit(1)
if mode == "timeout":
    pathlib.Path(sys.executable).with_name("owned-child.pid").write_text(str(os.getpid()))
    time.sleep(30)
if mode == "empty": sys.exit(0)
if mode == "invalid-utf8": sys.stdout.buffer.write(bytes([255])); sys.exit(0)
if mode == "flood": print("private-value" * 100000); sys.exit(0)
if mode == "stderr": print("private-value" * 100000, file=sys.stderr); sys.exit(0)
if mode == "noise": print("private-value")
if mode == "wrong-bool": receipt["analytics_enabled"] = "false"
if mode == "wrong-version": receipt["schema_version"] = 1
if mode == "extra": receipt["path"] = "private-value"
if mode == "contradiction": receipt["analytics_enabled"] = True
if mode == "array": print(json.dumps([receipt])); sys.exit(0)
if mode == "nonzero": print(json.dumps(receipt, separators=(",", ":"))); sys.exit(2)
if mode == "escaped-duplicate":
    print('{"schema_version":"1","analytics_enabled":false,"status":"ready","'
          + chr(92) + 'u0073tatus":"disabled"}')
    sys.exit(0)
if mode == "duplicate":
    print('{"schema_version":"1","analytics_enabled":false,"status":"ready","status":"disabled"}')
    sys.exit(0)
print(json.dumps(receipt, separators=(",", ":")))
""",
        encoding="utf-8",
    )
    python = _quote(venv / "Scripts" / "python.exe")
    pid_file = _quote(venv / "Scripts" / "owned-child.pid")
    launcher = _quote(install / "Start-K5VisionAlpha.ps1")
    script = (
        _load_functions()
        + rf"""
$python = '{python}'
$env:PYTHONPATH = 'private-value'
$env:PYTHONHOME = 'private-value'
$env:K5_STAGE03_SOURCE = 'private-value'
$env:K5_STAGE03_CAM_CRED = 'private-value'
$env:K5_CONTROL_PLANE_READ_TOKEN = 'private-value'
$env:CAM_CRED = 'private-value'
$env:UNRELATED_PRIVATE_TOKEN = 'private-value' 
$env:TEMP = '{_quote(temporary)}'
$env:K5_DEVICE_DB_PATH = 'untouched-database'
$expectedFailure = 'K5 analytics preflight failed. No alpha session was started.'
$unrelated = [Diagnostics.Process]::Start($python, '-I -B -c "import time;time.sleep(60)"')
$unrelatedHandle = $unrelated.Handle
try {{
foreach ($mode in @('disabled', 'ready', 'refused', 'noise', 'wrong-bool', 'wrong-version',
                   'extra', 'contradiction', 'array', 'nonzero', 'duplicate', 'escaped-duplicate',
                   'flood', 'stderr', 'empty', 'invalid-utf8', 'timeout')) {{
    $env:K5_ANALYTICS_CONFIG = $mode
    $watch = [Diagnostics.Stopwatch]::StartNew()
    try {{
        $result = Invoke-K5AnalyticsPreflight -Python $python -TimeoutSeconds 2
        if ($mode -notin @('disabled', 'ready')) {{ throw 'invalid_preflight_accepted' }}
        if ($result -isnot [bool] -or $result -ne ($mode -eq 'ready')) {{
            throw 'invalid_preflight_result'
        }}
    }} catch {{
        if ($mode -in @('disabled', 'ready')) {{ throw }}
        if ($_.Exception.Message -ne $expectedFailure) {{
            throw 'Unsanitized preflight failure.'
        }}
    }}
    if ($watch.Elapsed.TotalSeconds -gt 10) {{ throw 'Unbounded preflight.' }}
    foreach ($name in @('PYTHONPATH', 'PYTHONHOME', 'K5_STAGE03_SOURCE', 'K5_STAGE03_CAM_CRED',
                       'K5_CONTROL_PLANE_READ_TOKEN', 'CAM_CRED', 'UNRELATED_PRIVATE_TOKEN')) {{
        if ([Environment]::GetEnvironmentVariable($name, 'Process') -ne 'private-value') {{
            throw 'Parent environment changed.'
        }}
    }}
    if ($env:K5_ANALYTICS_CONFIG -ne $mode) {{ throw 'Explicit selection changed.' }}
    # Execute the installed launcher's real top-level path, with no native media.
    # Refusal must occur even without GStreamer records and before session state.
    if ($mode -ne 'timeout') {{
        try {{
            if ($mode -in @('disabled', 'ready')) {{
                & '{launcher}' -AnalyticsPreflightOnly
            }} else {{
                & '{launcher}'
                throw 'invalid_launcher_accepted'
            }}
        }} catch {{
            if ($mode -in @('disabled', 'ready') -or
                $_.Exception.Message -ne $expectedFailure) {{ throw }}
        }}
        if (@(Get-ChildItem -LiteralPath $env:TEMP -Force).Count -ne 0) {{
            throw 'Preflight created session state.'
        }}
        if ($env:K5_DEVICE_DB_PATH -ne 'untouched-database') {{
            throw 'Refusal changed database state.'
        }}
    }}
}}
try {{
    $null = Invoke-K5AnalyticsPreflight -Python ($python + '.unavailable') -TimeoutSeconds 2
    throw 'missing_cli_accepted'
}} catch {{
    if ($_.Exception.Message -ne $expectedFailure) {{ throw }}
}}
$ownedId = [int](Get-Content -LiteralPath '{pid_file}' -Raw)
if (Get-Process -Id $ownedId -ErrorAction SilentlyContinue) {{ throw 'Timed out child survived.' }}
if (@(Get-ChildItem -LiteralPath (Split-Path -Parent $python) -Filter '*.log').Count -ne 0) {{
    throw 'Preflight retained output.'
}}
if ($unrelated.HasExited) {{ throw 'Unrelated process was terminated.' }}
}} finally {{
    if (-not $unrelated.HasExited) {{ $unrelated.Kill() }}
    if (-not $unrelated.WaitForExit(5000)) {{ throw 'Test process cleanup failed.' }}
    $unrelated.Dispose()
}}
"""
    )
    _powershell(script, timeout=60)
    assert not list(package.glob("__pycache__"))
