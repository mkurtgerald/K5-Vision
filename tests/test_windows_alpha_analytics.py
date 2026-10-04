"""Alpha analytics admission and receipt guards, without native media allocation."""

from __future__ import annotations

import hashlib
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


VERSION_GUARD_PREFIX = b"K5_MEDIA_MTX_VERSION_GUARD="
VERSION_GUARD_OUTCOMES = {"guard_refused", "wrongly_accepted", "method_binding", "unexpected"}
VERSION_GUARD_SCRIPT = r"""
param([string]$Start, [string]$Python)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$outcome = 'unexpected'
$script:pinPreserved = $false
$guardSelected = $false
$script:wrongProbeConfirmed = $false
try {
    $tokens = $null
    $parseErrors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile(
        $Start, [ref]$tokens, [ref]$parseErrors
    )
    if ($parseErrors.Count -ne 0) { throw 'fixture_invalid' }
    $pin = @($ast.EndBlock.Statements | Where-Object {
        $_ -is [System.Management.Automation.Language.AssignmentStatementAst] -and
        $_.Left -is [System.Management.Automation.Language.VariableExpressionAst] -and
        $_.Left.VariablePath.UserPath -ceq 'MediaMtxVersion'
    })
    if ($pin.Count -ne 1 -or $pin[0].Right.Extent.Text -cne '"1.21.1"') {
        throw 'fixture_invalid'
    }
    $functions = @($ast.FindAll({ param($node)
        $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -ceq 'Start-K5SyntheticSource'
    }, $true))
    if ($functions.Count -ne 1) { throw 'fixture_invalid' }
    $statements = @($functions[0].Body.EndBlock.Statements)
    $assignments = @($statements | Where-Object {
        $_ -is [System.Management.Automation.Language.AssignmentStatementAst] -and
        $_.Right.Extent.Text -ceq '@(& $mediaMtx --version 2>&1)'
    })
    if ($assignments.Count -ne 1) { throw 'fixture_invalid' }
    $assignment = $assignments[0]
    $index = [Array]::IndexOf($statements, $assignment)
    if ($index -lt 0 -or $index + 1 -ge $statements.Count) { throw 'fixture_invalid' }
    $guard = $statements[$index + 1]
    if ($guard -isnot [System.Management.Automation.Language.IfStatementAst] -or
        -not $guard.Extent.Text.Contains('Pinned MediaMTX executable failed its version probe.')) {
        throw 'fixture_invalid'
    }
    if ($assignment.Left -isnot [System.Management.Automation.Language.VariableExpressionAst]) {
        throw 'fixture_invalid'
    }
    $outputName = $assignment.Left.VariablePath.UserPath
    if ($outputName -cnotmatch '^[A-Za-z_][A-Za-z0-9_]{0,63}$') { throw 'fixture_invalid' }
    if ($assignment.Extent.Text.Length -gt 256 -or $guard.Extent.Text.Length -gt 2048) {
        throw 'fixture_invalid'
    }
    # Execute only the candidate's exact scalar pin and exact two guard statements.
    # Python --version is the sole child; no synthetic helper or Start top level runs.
    . ([scriptblock]::Create($pin[0].Extent.Text))
    $mediaMtx = $Python
    $code = @(
        'function Invoke-K5VersionGuardOnly {',
        $assignment.Extent.Text,
        '$script:pinPreserved = ($MediaMtxVersion -is [string] -and',
        '    $MediaMtxVersion -ceq ''1.21.1'')',
        ('$testOutput = @($' + $outputName + ')'),
        '$script:wrongProbeConfirmed = ($LASTEXITCODE -eq 0 -and',
        '    $testOutput.Count -eq 1 -and',
        '    ([string]$testOutput[0] -cmatch ''^Python [0-9]+\.[0-9]+\.[0-9]+[a-z0-9.+-]*$''))',
        $guard.Extent.Text,
        '}'
    ) -join "`n"
    . ([scriptblock]::Create($code))
    $guardSelected = $true
    try {
        Invoke-K5VersionGuardOnly *> $null
        $outcome = 'wrongly_accepted'
    } catch {
        if ($_.Exception.Message -ceq 'Pinned MediaMTX executable failed its version probe.') {
            $outcome = 'guard_refused'
        } elseif ($_.Exception -is [System.Management.Automation.MethodException] -or
                  $_.Exception -is [System.Management.Automation.MethodInvocationException]) {
            $outcome = 'method_binding'
        } else {
            $outcome = 'unexpected'
        }
    }
} catch {
    $outcome = 'unexpected'
}
$record = [ordered]@{
    schema_version = 'mediamtx-version-guard-v1'
    outcome = $outcome
    guard_selected = $guardSelected
    wrong_probe_confirmed = $script:wrongProbeConfirmed
    pin_preserved = $script:pinPreserved
}
Write-Output ('K5_MEDIA_MTX_VERSION_GUARD=' + ($record | ConvertTo-Json -Compress))
exit 0
"""


def _version_guard_record(stdout: bytes, stderr: bytes, returncode: int) -> dict:
    """Accept a single bounded fixed record, never a raw PowerShell error."""
    if type(returncode) is not int or returncode != 0 or stderr or len(stdout) > 1024:
        raise ValueError("Invalid version guard evidence")
    lines = stdout.splitlines()
    if len(lines) != 1 or not lines[0].startswith(VERSION_GUARD_PREFIX):
        raise ValueError("Invalid version guard evidence")
    pairs = json.loads(lines[0][len(VERSION_GUARD_PREFIX) :], object_pairs_hook=list)
    if type(pairs) is not list or any(type(pair) is not tuple or len(pair) != 2 for pair in pairs):
        raise ValueError("Invalid version guard evidence")
    value = dict(pairs)
    if len(value) != len(pairs) or value.keys() != {
        "schema_version",
        "outcome",
        "guard_selected",
        "wrong_probe_confirmed",
        "pin_preserved",
    }:
        raise ValueError("Invalid version guard evidence")
    if value["schema_version"] != "mediamtx-version-guard-v1":
        raise ValueError("Invalid version guard evidence")
    if type(value["outcome"]) is not str or value["outcome"] not in VERSION_GUARD_OUTCOMES:
        raise ValueError("Invalid version guard evidence")
    if any(
        type(value[key]) is not bool
        for key in ("guard_selected", "wrong_probe_confirmed", "pin_preserved")
    ):
        raise ValueError("Invalid version guard evidence")
    return value


@pytest.mark.skipif(sys.platform != "win32", reason="Requires Windows PowerShell version binding")
def test_windows_synthetic_version_guard_rejects_wrong_executable(tmp_path: Path) -> None:
    record = None
    try:
        python = Path(sys._base_executable).resolve(strict=True)
        if python.name.lower() != "python.exe" or not python.is_file():
            raise ValueError("Invalid fixture runtime")
        script = tmp_path / "version-guard.ps1"
        script.write_text(VERSION_GUARD_SCRIPT, encoding="ascii", newline="\n")
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-File",
                str(script),
                "-Start",
                str(START),
                "-Python",
                str(python),
            ],
            capture_output=True,
            timeout=20,
            check=False,
        )
        record = _version_guard_record(result.stdout, result.stderr, result.returncode)
    except Exception:
        pass  # No exception, argument, source text or child output reaches pytest.
    if record is None:
        pytest.fail("MediaMTX version guard fixture failed", pytrace=False)
    print(
        VERSION_GUARD_PREFIX.decode("ascii")
        + json.dumps(record, separators=(",", ":"), sort_keys=True)
    )
    if record != {
        "schema_version": "mediamtx-version-guard-v1",
        "outcome": "guard_refused",
        "guard_selected": True,
        "wrong_probe_confirmed": True,
        "pin_preserved": True,
    }:
        pytest.fail("MediaMTX version guard did not reject the wrong executable", pytrace=False)


def test_version_guard_probe_is_hosted_only_and_keeps_existing_smoke_selection() -> None:
    from fnmatch import fnmatchcase

    native = (ROOT / ".github/workflows/installed-analytics-candidate.yml").read_text()
    push = native.split("  push:\n", 1)[1].split("\n\n", 1)[0]
    patterns = [
        line.strip()[2:].strip("\"'")
        for line in push.split("    paths:\n", 1)[1].splitlines()
        if line.strip().startswith("- ")
    ]
    assert patterns and all(
        not fnmatchcase("tests/test_windows_alpha_analytics.py", pattern) for pattern in patterns
    )
    test_windows_smoke_selects_analytics_and_preserves_lifetime_coverage()


def test_version_guard_record_retains_only_fixed_evidence() -> None:
    value = {
        "schema_version": "mediamtx-version-guard-v1",
        "outcome": "wrongly_accepted",
        "guard_selected": True,
        "wrong_probe_confirmed": True,
        "pin_preserved": True,
    }
    raw = VERSION_GUARD_PREFIX + json.dumps(value).encode() + b"\r\n"
    assert _version_guard_record(raw, b"", 0) == value
    for change in (
        {"path": "private"},
        {"outcome": "private exception"},
        {"outcome": []},
        {"wrong_probe_confirmed": 1},
        {"schema_version": "unknown"},
    ):
        bad = {**value, **change}
        with pytest.raises(ValueError):
            _version_guard_record(VERSION_GUARD_PREFIX + json.dumps(bad).encode(), b"", 0)
    for output, error, code in (
        (raw * 2, b"", 0),
        (raw, b"private stderr", 0),
        (raw, b"", 1),
        (b"x" * 1025, b"", 0),
        (VERSION_GUARD_PREFIX + b"[]", b"", 0),
    ):
        with pytest.raises(ValueError):
            _version_guard_record(output, error, code)


def test_version_guard_fixture_selects_only_source_pin_assignment_and_guard() -> None:
    assert "Set-StrictMode -Version Latest" in VERSION_GUARD_SCRIPT
    assert "'@(& $mediaMtx --version 2>&1)'" in VERSION_GUARD_SCRIPT
    assert "$guard = $statements[$index + 1]" in VERSION_GUARD_SCRIPT
    assert "$pin.Count -ne 1" in VERSION_GUARD_SCRIPT
    assert "$assignments.Count -ne 1" in VERSION_GUARD_SCRIPT
    assert "$functions.Count -ne 1" in VERSION_GUARD_SCRIPT
    assert "$guard.Extent.Text" in VERSION_GUARD_SCRIPT
    assert "$assignment.Extent.Text" in VERSION_GUARD_SCRIPT
    body = VERSION_GUARD_SCRIPT.split("$code = @(", 1)[1].split(") -join", 1)[0]
    assert body.index("$assignment.Extent.Text") < body.index("$script:pinPreserved")
    assert body.index("$script:pinPreserved") < body.index("$guard.Extent.Text")
    assert "$MediaMtxVersion -ceq ''1.21.1''" in body
    assert "Invoke-K5VersionGuardOnly *> $null" in VERSION_GUARD_SCRIPT
    assert "& $Start" not in VERSION_GUARD_SCRIPT
    assert "Start-Process" not in VERSION_GUARD_SCRIPT
    assert "--validate-conf" not in VERSION_GUARD_SCRIPT
    assert "Test-K5TcpListener" not in VERSION_GUARD_SCRIPT
    assert "Invoke-WebRequest" not in VERSION_GUARD_SCRIPT


def test_version_guard_record_rejects_duplicate_fields_and_nested_values() -> None:
    for suffix in (
        b'{"schema_version":"mediamtx-version-guard-v1","outcome":"guard_refused",'
        b'"outcome":"wrongly_accepted","guard_selected":true,"wrong_probe_confirmed":true,'
        b'"pin_preserved":true}',
        b'{"schema_version":"mediamtx-version-guard-v1","outcome":"guard_refused",'
        b'"guard_selected":{},"wrong_probe_confirmed":true,"pin_preserved":true}',
    ):
        with pytest.raises(ValueError):
            _version_guard_record(VERSION_GUARD_PREFIX + suffix, b"", 0)


def test_media_mtx_version_guard_has_distinct_output_and_exact_official_token() -> None:
    source = START.read_text()
    assert "$mediaMtxVersionOutput = @(& $mediaMtx --version 2>&1)" in source
    guard = source.split("$mediaMtxVersionOutput =", 1)[1].split("$validation =", 1)[0]
    assert "$LASTEXITCODE -ne 0" in guard
    assert "$mediaMtxVersionOutput.Count -ne 1" in guard
    assert "$mediaMtxVersionOutput[0] -isnot [string]" in guard
    assert '$mediaMtxVersionOutput[0] -cne ("v" + $MediaMtxVersion)' in guard
    assert ".Contains(" not in guard and "-join" not in guard


def test_synthetic_cleanup_preserves_typed_primary_error_in_memory_only() -> None:
    source = START.read_text()
    assert '$cleanupFailure.Data["K5.StartupErrorRecord"] = $startupFailure' in source
    assert "throw $cleanupFailure" in source
    assert "throw $startupFailure" in source
    assert source.count("K5.StartupErrorRecord") == 1


@pytest.mark.skipif(sys.platform != "win32", reason="Requires Windows PowerShell version predicate")
def test_windows_version_guard_exact_official_token_matrix(tmp_path: Path) -> None:
    # Reuse the independently pinned, exact AST selection from the baseline.
    selection = VERSION_GUARD_SCRIPT.split("    $mediaMtx = $Python", 1)[0]
    cases = [
        (["v1.21.1"], 0, True),
        (["1.21.1"], 0, False),
        (["v1.21.10"], 0, False),
        (["prefix v1.21.1"], 0, False),
        (["v1.21.1 suffix"], 0, False),
        (["v1.21.1 "], 0, False),
        ([], 0, False),
        ([""], 0, False),
        (["v1.21.1", "v1.21.1"], 0, False),
        (["v1.21.1\nextra"], 0, False),
        (["v1.21.1"], 1, False),
        ([123], 0, False),
    ]
    data = json.dumps(
        [dict(lines=lines, exit=exit_code, accept=accept) for lines, exit_code, accept in cases]
    )
    script = (
        selection
        + r"""
    $code = @(
        'function Invoke-K5ControlledVersionGuard { param($case)',
        ('Set-Variable -Name ' + $outputName + ' -Value @($case.lines)'),
        '$LASTEXITCODE = $case.exit',
        $guard.Extent.Text,
        '}'
    ) -join "`n"
    . ([scriptblock]::Create($code))
    $cases = ConvertFrom-Json '__CASES__'
    foreach ($case in $cases) {
        $accepted = $true
        try { Invoke-K5ControlledVersionGuard $case *> $null }
        catch {
            if ($_.Exception.Message -cne 'Pinned MediaMTX executable failed its version probe.') {
                throw 'fixture_invalid'
            }
            $accepted = $false
        }
        if ($accepted -ne $case.accept -or $MediaMtxVersion -cne '1.21.1') {
            throw 'fixture_invalid'
        }
    }
    Write-Output 'K5_MEDIA_MTX_VERSION_MATRIX=passed'
    exit 0
} catch { exit 1 }
""".replace("__CASES__", data.replace("'", "''"))
    )
    valid = False
    try:
        target = tmp_path / "version-matrix.ps1"
        target.write_text(script, encoding="ascii", newline="\n")
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-File",
                str(target),
                "-Start",
                str(START),
            ],
            capture_output=True,
            timeout=20,
            check=False,
        )
        valid = (
            result.returncode == 0
            and not result.stderr
            and result.stdout.strip() == b"K5_MEDIA_MTX_VERSION_MATRIX=passed"
        )
    except Exception:
        pass
    if not valid:
        pytest.fail("MediaMTX exact version predicate fixture failed", pytrace=False)


def _startup_witness():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "startup_diagnostic_fixture", ROOT / "scripts/installed_alpha_launcher_witness.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.skipif(sys.platform != "win32", reason="Requires Windows PowerShell ErrorRecord")
def test_windows_synthetic_primary_and_cleanup_errors_stay_typed_and_source_free(tmp_path: Path):
    # No media/port/child seams execute. The exact candidate function still owns
    # its genuine startup catch and cleanup logic; fake objects fail both paths.
    module = _startup_witness()
    fixture = r"""
param([int]$Port, [switch]$ExitAfterPublicTest)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$tokens = $null
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    '__SOURCE__', [ref]$tokens, [ref]$parseErrors
)
$functions = @($ast.FindAll({ param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
    $node.Name -ceq 'Start-K5SyntheticSource'
}, $true))
if ($parseErrors.Count -ne 0 -or $functions.Count -ne 1) { throw 'fixture_invalid' }
. ([scriptblock]::Create($functions[0].Extent.Text))
$sessionRoot = $PSScriptRoot
$MediaMtxVersion = '1.21.1'
$gstLaunch = 'fixture_publisher'
$script:server = $null
function Test-K5GStreamerElement { return $true }
function Get-K5MediaMtx { return 'Test-K5FixtureMediaMtx' }
function Test-K5FixtureMediaMtx {
    $global:LASTEXITCODE = 0
    if ($args[0] -ceq '--version') { return 'v1.21.1' }
}
function Test-K5TcpListener { return $null -ne $script:server }
function Start-Sleep { }
function Start-Process {
    param($FilePath, $ArgumentList, [switch]$PassThru, [switch]$NoNewWindow,
          $WindowStyle, $RedirectStandardOutput, $RedirectStandardError)
    if ($FilePath -ceq 'fixture_publisher') { throw 'PRIVATE_STARTUP_SENTINEL' }
    $script:server = [pscustomobject]@{ HasExited = $false; Handle = [IntPtr]1 }
    $script:server | Add-Member ScriptMethod Kill { throw 'PRIVATE_CLEANUP_SENTINEL' }
    $script:server | Add-Member ScriptMethod WaitForExit { param($milliseconds) return $false }
    return $script:server
}
try { $null = Start-K5SyntheticSource; throw 'fixture_invalid' }
catch {
    $message = 'Local synthetic RTSP startup failed and owned process cleanup was incomplete.'
    $linked = $_.Exception.Data['K5.StartupErrorRecord']
    if ($_.Exception.Message -cne $message -or
        $linked -isnot [System.Management.Automation.ErrorRecord] -or
        $linked.Exception.Message -cne 'PRIVATE_STARTUP_SENTINEL') {
        throw 'fixture_invalid'
    }
    throw
}
""".replace("__SOURCE__", _quote(START))
    records = None
    try:
        target = tmp_path / "start-fixture.ps1"
        target.write_text(fixture, encoding="ascii", newline="\n")
        envelope = tmp_path / "envelope.ps1"
        envelope.write_text(
            module.bind_start_envelope(hashlib.sha256(fixture.encode("ascii")).hexdigest()),
            encoding="ascii",
            newline="\n",
        )
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-File",
                str(envelope),
                "-Start",
                str(target),
                "-Port",
                "8011",
            ],
            capture_output=True,
            timeout=20,
            check=False,
        )
        if (
            result.returncode != 24
            or result.stderr
            or len(result.stdout) > 4096
            or b"PRIVATE_" in result.stdout
        ):
            raise ValueError("Invalid fixture result")
        records = [
            module.parse_start_error(line[len(module.START_ERROR_PREFIX) :])
            for line in result.stdout.splitlines()
            if line.startswith(module.START_ERROR_PREFIX)
        ]
        if (
            len(records) != 2
            or [r["phase"] for r in records] != ["primary", "cleanup"]
            or records[0]["failure"] != "unknown"
            or records[1]["failure"] != "synthetic_cleanup"
        ):
            records = None
    except Exception:
        records = None
    if records is None:
        pytest.fail("Typed startup and cleanup projection fixture failed", pytrace=False)
    for record in records:
        print(module.START_ERROR_PREFIX.decode() + json.dumps(record, separators=(",", ":")))


@pytest.mark.skipif(sys.platform != "win32", reason="Requires Windows PowerShell error projection")
def test_windows_start_error_projection_bounds_foreign_and_malformed_metadata(tmp_path: Path):
    module = _startup_witness()
    valid = False
    try:
        # Run the exact envelope, with a tiny owned Start fixture and no media.
        # A real foreign-origin ErrorRecord is linked in memory; malformed Data
        # must be ignored, and both invocations must still fail with exit 24.
        foreign = tmp_path / "foreign.ps1"
        foreign.write_text("throw 'PRIVATE_FOREIGN_SENTINEL'\n", encoding="ascii")
        envelope = tmp_path / "envelope.ps1"
        envelope.write_text(module.ENVELOPE, encoding="ascii", newline="\n")
        for linked in ("$saved", "'PRIVATE_MALFORMED_SENTINEL'"):
            target = tmp_path / "start.ps1"
            target.write_text(
                "param([int]$Port, [switch]$ExitAfterPublicTest)\n"
                f"try {{ & '{_quote(foreign)}' }} catch {{ $saved = $_ }}\n"
                "$fatal = [InvalidOperationException]::new("
                "'Local synthetic RTSP startup failed and owned process cleanup was incomplete.')\n"
                f"$fatal.Data['K5.StartupErrorRecord'] = {linked}\n"
                "throw $fatal\n",
                encoding="ascii",
                newline="\n",
            )
            result = subprocess.run(
                [
                    "powershell.exe",
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-File",
                    str(envelope),
                    "-Start",
                    str(target),
                    "-Port",
                    "8011",
                ],
                capture_output=True,
                timeout=20,
                check=False,
            )
            if result.returncode != 24 or result.stderr or b"PRIVATE_" in result.stdout:
                raise ValueError("Invalid projection")
            if len(result.stdout) > 4096:
                raise ValueError("Invalid projection")
            records = [
                module.parse_start_error(line[len(module.START_ERROR_PREFIX) :])
                for line in result.stdout.splitlines()
                if line.startswith(module.START_ERROR_PREFIX)
            ]
            if linked == "$saved":
                if (
                    len(records) != 2
                    or records[0]["origin"] != "unknown"
                    or records[0]["source_line"] is not None
                ):
                    raise ValueError("Invalid projection")
            elif len(records) != 1 or records[0]["phase"] != "primary":
                raise ValueError("Invalid projection")
        valid = True
    except Exception:
        pass
    if not valid:
        pytest.fail("Bounded Start error metadata fixture failed", pytrace=False)


@pytest.mark.skipif(sys.platform != "win32", reason="Requires Windows PowerShell source origin")
def test_windows_start_error_projection_checks_real_file_line_and_operation(tmp_path: Path):
    module = _startup_witness()
    valid = False
    try:
        # This fixture is explicitly modified test source. Stop immediately after
        # StrictMode, before any admission, native work, session or child creation.
        source = START.read_text()
        stop = 'throw "Pinned MediaMTX executable failed its version probe."'
        marker = "Set-StrictMode -Version Latest\n"
        if source.count(marker) != 1:
            raise ValueError("Invalid fixture source")
        fixture = source.replace(marker, marker + stop + "\n", 1)
        expected_line = fixture.splitlines().index(stop) + 1
        target = tmp_path / "source-fixture.ps1"
        envelope = tmp_path / "envelope.ps1"
        admitted = hashlib.sha256(fixture.encode("ascii")).hexdigest()
        cases = (
            ("", module.bind_start_envelope(admitted), "start"),
            ("", module.ENVELOPE, "unknown"),
            ("", module.bind_start_envelope("a" * 64), "unknown"),
            ("\n# replaced bytes", module.bind_start_envelope(admitted), "unknown"),
            ("\n#" + "x" * 65536, module.bind_start_envelope(admitted), "unknown"),
        )
        for extra, bound_envelope, expected_origin in cases:
            envelope.write_text(bound_envelope, encoding="ascii", newline="\n")
            target.write_text(fixture + extra, encoding="ascii", newline="\n")
            result = subprocess.run(
                [
                    "powershell.exe",
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-File",
                    str(envelope),
                    "-Start",
                    str(target),
                    "-Port",
                    "8011",
                ],
                capture_output=True,
                timeout=20,
                check=False,
            )
            if result.returncode != 24 or result.stderr or len(result.stdout) > 2048:
                raise ValueError("Invalid fixture result")
            records = [
                module.parse_start_error(line[len(module.START_ERROR_PREFIX) :])
                for line in result.stdout.splitlines()
                if line.startswith(module.START_ERROR_PREFIX)
            ]
            if len(records) != 1:
                raise ValueError("Invalid fixture result")
            record = records[0]
            if record["origin"] != expected_origin or record["failure"] != "mediamtx_version":
                raise ValueError("Invalid fixture projection")
            if expected_origin == "start":
                if (
                    record["source_line"] != expected_line
                    or record["operation"] != "launcher_setup"
                ):
                    raise ValueError("Invalid fixture projection")
            elif record["source_line"] is not None or record["operation"] != "unknown":
                raise ValueError("Invalid fixture projection")
        valid = True
    except Exception:
        pass
    if not valid:
        pytest.fail("Checked Start source line projection fixture failed", pytrace=False)


ERROR_SHAPE_PREFIX = b"K5_ALPHA_ERROR_SHAPE="
ERROR_SHAPE_BOOL_FIELDS = {
    "present",
    "invocation_present",
    "script_name_present",
    "script_name_matches",
    "command_path_present",
    "command_path_matches",
    "line_in_start",
    "runtime_exception",
    "parent_exception",
    "action_stop",
    "method_exception",
    "native_remote",
    "nested_record",
    "inner_exception",
}
ERROR_SHAPE_IDS = {
    "absent",
    "other",
    "known_throw",
    "variable_undefined",
    "property_missing",
    "null_method",
    "method_missing",
    "native_stderr",
    "path_missing",
    "command_missing",
    "parameter_binding",
    "element_missing",
}
ERROR_BOUNDARY_CASES = {
    "direct_known": 'throw "Pinned MediaMTX executable failed its version probe."',
    "nested_known": (
        "function Invoke-K5FixtureFault { "
        'throw "Pinned MediaMTX executable failed its version probe." }\nInvoke-K5FixtureFault'
    ),
    "nested_rethrow": (
        "function Invoke-K5FixtureFault { try { "
        'throw "Pinned MediaMTX executable failed its version probe." } '
        "catch { throw } }\nInvoke-K5FixtureFault"
    ),
    "missing_variable": (
        "function Invoke-K5FixtureFault { $null = $K5FixtureUndefinedVariable }\n"
        "Invoke-K5FixtureFault"
    ),
    "missing_property": (
        "function Invoke-K5FixtureFault { "
        "$null = ([pscustomobject]@{}).K5FixtureAbsentProperty }\nInvoke-K5FixtureFault"
    ),
    "null_method": (
        "function Invoke-K5FixtureFault { $value = $null; $null = $value.ToString() }\n"
        "Invoke-K5FixtureFault"
    ),
    "missing_owned_file": (
        "function Invoke-K5FixtureFault { "
        'Get-Content -LiteralPath (Join-Path $PSScriptRoot "fixture-absent") }\n'
        "Invoke-K5FixtureFault"
    ),
    "native_stderr": (
        "$null = @(& $python -I -B -c "
        "\"import sys;sys.stderr.write('PRIVATE_NATIVE_SENTINEL')\" 2>&1)"
    ),
    "native_all_streams": (
        "function Invoke-K5FixtureElement([string]$Name) {\n"
        "    & $gstInspect $Name *> $null\n"
        "    return $LASTEXITCODE -eq 0\n"
        "}\n"
        "$gstInspect = $python\n"
        'if (-not (Invoke-K5FixtureElement (Join-Path $PSScriptRoot "fixture-stderr.py"))) {\n'
        '    throw "Reviewed GStreamer runtime is missing required synthetic test element: '
        'videotestsrc"\n'
        "}"
    ),
    "foreign_known": '& (Join-Path $PSScriptRoot "foreign.ps1")',
    "foreign_runtime": '& (Join-Path $PSScriptRoot "foreign-runtime.ps1")',
}
ERROR_BOUNDARY_EXPECTED_IDS = {
    "direct_known": "known_throw",
    "nested_known": "known_throw",
    "nested_rethrow": "known_throw",
    "missing_variable": "variable_undefined",
    "missing_property": "property_missing",
    "null_method": "null_method",
    "missing_owned_file": "path_missing",
    "native_stderr": "native_stderr",
    "native_all_streams": "element_missing",
    "foreign_known": "known_throw",
    "foreign_runtime": "variable_undefined",
}

# Test-only, observational insertion after the original catch saves $fatal. All
# original envelope bytes remain in order and the production projection runs next.
# Inspect only typed ErrorRecord / Exception edges, never stringify a graph node.
ERROR_SHAPE_OBSERVER = r"""
    function Write-K5FixtureErrorShape {
        param([System.Management.Automation.ErrorRecord]$Root)
        $shape = [ordered]@{
            schema_version = 'alpha-error-boundary-shape-v1'
            observer_valid = $false
            records = 0
            nodes = 0
            cycle = $false
            truncated = $false
            source_read_ok = $false
            source_anchors_ok = $false
        }
        $fields = @('present','invocation_present','script_name_present','script_name_matches',
                    'command_path_present','command_path_matches','line_in_start',
                    'runtime_exception','parent_exception','action_stop','method_exception',
                    'native_remote','nested_record','inner_exception')
        for ($slot = 0; $slot -lt 4; $slot++) {
            foreach ($field in $fields) { $shape["record_${slot}_${field}"] = $false }
            $shape["record_${slot}_error_id"] = 'absent'
        }
        try {
            $startPath = [IO.Path]::GetFullPath($Start)
            $sourceLineCount = 0
            try {
                $stream = [IO.File]::OpenRead($startPath)
                try {
                    if ($stream.Length -gt 65536) { throw 'fixture_source_invalid' }
                    $bytes = New-Object byte[] 65537
                    $count = 0
                    while ($count -lt $bytes.Length) {
                        $read = $stream.Read($bytes, $count, $bytes.Length - $count)
                        if ($read -eq 0) { break }
                        $count += $read
                    }
                    if ($count -gt 65536) { throw 'fixture_source_invalid' }
                } finally { $stream.Dispose() }
                $utf8 = New-Object System.Text.UTF8Encoding($false, $true)
                $lines = $utf8.GetString($bytes, 0, $count).Replace("`r`n", "`n").Split("`n")
                if ($lines.Count -gt 4096) { throw 'fixture_source_invalid' }
                $shape.source_read_ok = $true
                $sourceLineCount = $lines.Count
                $anchors = ConvertFrom-Json '__ANCHOR_JSON__'
                $previous = 0
                foreach ($anchor in $anchors) {
                    $positions = @(for ($line = 0; $line -lt $lines.Count; $line++) {
                        if ($lines[$line].Trim() -ceq $anchor[0]) { $line + 1 }
                    })
                    if ($positions.Count -ne 1 -or $positions[0] -le $previous) {
                        throw 'fixture_source_invalid'
                    }
                    $previous = $positions[0]
                }
                $shape.source_anchors_ok = $true
            } catch { $shape.source_anchors_ok = $false }
            $queue = [Collections.Generic.Queue[object]]::new()
            $seen = [Collections.Generic.List[object]]::new()
            $queue.Enqueue([pscustomobject]@{ Value = $Root; Depth = 0 })
            while ($queue.Count -gt 0 -and $shape.nodes -lt 8) {
                $entry = $queue.Dequeue()
                $node = $entry.Value
                $revisited = $false
                foreach ($prior in $seen) {
                    if ([object]::ReferenceEquals($node, $prior)) { $revisited = $true; break }
                }
                if ($revisited) { $shape.cycle = $true; continue }
                if ($entry.Depth -gt 4) { $shape.truncated = $true; continue }
                $seen.Add($node)
                $shape.nodes++
                if ($node -is [System.Management.Automation.ErrorRecord]) {
                    if ($shape.records -ge 4) { $shape.truncated = $true; continue }
                    $slot = $shape.records
                    $shape.records++
                    $shape["record_${slot}_present"] = $true
                    $shape["record_${slot}_error_id"] = 'other'
                    $id = $node.FullyQualifiedErrorId
                    if ($null -ne $id -and $id.Length -le 512) {
                        $token = $id.Split(',')[0]
                        $elementId = 'Reviewed GStreamer runtime is missing required '
                        $elementId += 'synthetic test element: videotestsrc'
                        switch -CaseSensitive ($token) {
                            'Pinned MediaMTX executable failed its version probe.' {
                                $shape["record_${slot}_error_id"] = 'known_throw'
                            }
                            { $_ -ceq $elementId } {
                                $shape["record_${slot}_error_id"] = 'element_missing'
                            }
                            'VariableIsUndefined' {
                                $shape["record_${slot}_error_id"] = 'variable_undefined'
                            }
                            'PropertyNotFoundStrict' {
                                $shape["record_${slot}_error_id"] = 'property_missing'
                            }
                            'InvokeMethodOnNull' {
                                $shape["record_${slot}_error_id"] = 'null_method'
                            }
                            'MethodNotFound' {
                                $shape["record_${slot}_error_id"] = 'method_missing'
                            }
                            'NativeCommandError' {
                                $shape["record_${slot}_error_id"] = 'native_stderr'
                            }
                            'NativeCommandErrorMessage' {
                                $shape["record_${slot}_error_id"] = 'native_stderr'
                            }
                            'PathNotFound' {
                                $shape["record_${slot}_error_id"] = 'path_missing'
                            }
                            'CommandNotFoundException' {
                                $shape["record_${slot}_error_id"] = 'command_missing'
                            }
                            'NamedParameterNotFound' {
                                $shape["record_${slot}_error_id"] = 'parameter_binding'
                            }
                        }
                    }
                    $info = $node.InvocationInfo
                    $shape["record_${slot}_invocation_present"] = $null -ne $info
                    if ($null -ne $info) {
                        $name = $info.ScriptName
                        $shape["record_${slot}_script_name_present"] =
                            -not [string]::IsNullOrEmpty($name)
                        if (-not [string]::IsNullOrEmpty($name)) {
                            $shape["record_${slot}_script_name_matches"] = [string]::Equals(
                                [IO.Path]::GetFullPath($name), $startPath,
                                [StringComparison]::OrdinalIgnoreCase)
                        }
                        $property = $info.PSObject.Properties['PSCommandPath']
                        if ($null -ne $property) {
                            $path = $property.Value
                            $shape["record_${slot}_command_path_present"] =
                                -not [string]::IsNullOrEmpty($path)
                            if (-not [string]::IsNullOrEmpty($path)) {
                                $shape["record_${slot}_command_path_matches"] = [string]::Equals(
                                    [IO.Path]::GetFullPath($path), $startPath,
                                    [StringComparison]::OrdinalIgnoreCase)
                            }
                        }
                        $shape["record_${slot}_line_in_start"] =
                            $shape["record_${slot}_script_name_matches"] -and
                            $info.ScriptLineNumber -ge 1 -and
                            $info.ScriptLineNumber -le $sourceLineCount
                    }
                    $exception = $node.Exception
                    $shape["record_${slot}_runtime_exception"] =
                        $exception -is [System.Management.Automation.RuntimeException]
                    $shape["record_${slot}_parent_exception"] =
                        $exception -is
                            [System.Management.Automation.ParentContainsErrorRecordException]
                    $shape["record_${slot}_action_stop"] =
                        $exception -is [System.Management.Automation.ActionPreferenceStopException]
                    $shape["record_${slot}_method_exception"] =
                        $exception -is [System.Management.Automation.MethodException] -or
                        $exception -is [System.Management.Automation.MethodInvocationException]
                    $shape["record_${slot}_native_remote"] =
                        $exception -is [System.Management.Automation.RemoteException]
                    $shape["record_${slot}_inner_exception"] = $null -ne $exception.InnerException
                    if ($exception -is [System.Management.Automation.RuntimeException]) {
                        $shape["record_${slot}_nested_record"] =
                            $exception.ErrorRecord -is [System.Management.Automation.ErrorRecord]
                    }
                    $queue.Enqueue([pscustomobject]@{
                        Value = $exception; Depth = $entry.Depth + 1
                    })
                } elseif ($node -is [Exception]) {
                    if ($node -is [System.Management.Automation.RuntimeException] -and
                        $node.ErrorRecord -is [System.Management.Automation.ErrorRecord]) {
                        $queue.Enqueue([pscustomobject]@{
                            Value = $node.ErrorRecord; Depth = $entry.Depth + 1
                        })
                    }
                    if ($null -ne $node.InnerException) {
                        $queue.Enqueue([pscustomobject]@{
                            Value = $node.InnerException; Depth = $entry.Depth + 1
                        })
                    }
                } else { throw 'fixture_invalid' }
            }
            if ($queue.Count -gt 0) { $shape.truncated = $true }
            $shape.observer_valid = $true
        } catch { $shape.observer_valid = $false }
        Write-Output ('K5_ALPHA_ERROR_SHAPE=' + ($shape | ConvertTo-Json -Compress))
    }
    Write-K5FixtureErrorShape -Root $fatal
"""


def _error_shape_observer(anchors) -> str:
    return ERROR_SHAPE_OBSERVER.replace("__ANCHOR_JSON__", json.dumps(anchors).replace("'", "''"))


def _observed_envelope(envelope: str, anchors) -> str:
    marker = "    $fatal = $_\n"
    if envelope.count(marker) != 1:
        raise ValueError("Invalid fixture envelope")
    insertion = _error_shape_observer(anchors)
    observed = envelope.replace(marker, marker + insertion, 1)
    if observed.replace(insertion, "", 1) != envelope:
        raise ValueError("Invalid fixture envelope")
    return observed


def _validate_error_shape(value: object) -> None:
    fields = {
        f"record_{slot}_{field}"
        for slot in range(4)
        for field in ERROR_SHAPE_BOOL_FIELDS | {"error_id"}
    }
    if type(value) is not dict or value.keys() != fields | {
        "schema_version",
        "observer_valid",
        "records",
        "nodes",
        "cycle",
        "truncated",
        "source_read_ok",
        "source_anchors_ok",
    }:
        raise ValueError("Invalid error shape")
    if (
        type(value["schema_version"]) is not str
        or value["schema_version"] != "alpha-error-boundary-shape-v1"
    ):
        raise ValueError("Invalid error shape")
    for field, maximum in (("records", 4), ("nodes", 8)):
        if type(value[field]) is not int or not 0 <= value[field] <= maximum:
            raise ValueError("Invalid error shape")
    for field in ("observer_valid", "cycle", "truncated", "source_read_ok", "source_anchors_ok"):
        if type(value[field]) is not bool:
            raise ValueError("Invalid error shape")
    for slot in range(4):
        for field in ERROR_SHAPE_BOOL_FIELDS:
            if type(value[f"record_{slot}_{field}"]) is not bool:
                raise ValueError("Invalid error shape")
        identity = value[f"record_{slot}_error_id"]
        if type(identity) is not str or identity not in ERROR_SHAPE_IDS:
            raise ValueError("Invalid error shape")
        if value[f"record_{slot}_present"] != (slot < value["records"]):
            raise ValueError("Invalid error shape")
        if slot >= value["records"] and (
            identity != "absent"
            or any(value[f"record_{slot}_{field}"] for field in ERROR_SHAPE_BOOL_FIELDS)
        ):
            raise ValueError("Invalid error shape")


def _parse_error_boundary_output(module, result, *, observed):
    if result.returncode != 24 or result.stderr or len(result.stdout) > 12_288:
        raise ValueError("Invalid error boundary output")
    projections, shapes = [], []
    admitted = failed = 0
    for line in result.stdout.splitlines():
        if line.startswith(module.START_ERROR_PREFIX):
            projections.append(module.parse_start_error(line[len(module.START_ERROR_PREFIX) :]))
        elif line.startswith(ERROR_SHAPE_PREFIX):
            raw = line[len(ERROR_SHAPE_PREFIX) :]
            if len(raw) > 8192:
                raise ValueError("Invalid error shape")
            pairs = json.loads(raw, object_pairs_hook=list)
            if type(pairs) is not list or any(
                type(pair) is not tuple or len(pair) != 2 for pair in pairs
            ):
                raise ValueError("Invalid error shape")
            shape = dict(pairs)
            if len(shape) != len(pairs):
                raise ValueError("Invalid error shape")
            _validate_error_shape(shape)
            shapes.append(shape)
        elif line == b"K5 analytics configuration admitted; live provider acceptance is pending.":
            admitted += 1
        elif line == b"K5_ALPHA_START_FAILED":
            failed += 1
        else:
            raise ValueError("Invalid error boundary output")
    if admitted != 1 or failed != 1 or len(projections) != 1 or len(shapes) != int(observed):
        raise ValueError("Invalid error boundary output")
    return projections[0], shapes[0] if shapes else None


@pytest.mark.skipif(
    sys.platform != "win32", reason="Requires hosted Windows ErrorRecord propagation"
)
def test_windows_post_admission_error_boundary_preserves_useful_projection(tmp_path: Path):
    import os

    module = _startup_witness()
    completed = False
    regressions = []
    try:
        installed = tmp_path / "installed"
        venv = installed / ".venv"
        subprocess.run(
            [sys._base_executable, "-I", "-B", "-m", "venv", "--without-pip", str(venv)],
            capture_output=True,
            timeout=60,
            check=True,
        )
        package = venv / "Lib/site-packages/k5vision"
        package.mkdir()
        (package / "__init__.py").write_bytes(b"")
        (package / "cli.py").write_text(
            "import sys\nassert sys.flags.isolated and sys.flags.dont_write_bytecode\n"
            'assert sys.argv[1:] == ["analytics-preflight"]\n'
            'print(\'{"schema_version":"1","analytics_enabled":true,"status":"ready"}\')\n',
            encoding="ascii",
            newline="\n",
        )
        sessions = tmp_path / "temporary"
        sessions.mkdir()
        target = installed / "Start-K5VisionAlpha.ps1"
        source = START.read_text()
        marker = "if ($AnalyticsPreflightOnly) { return }\n"
        if source.count(marker) != 1:
            raise ValueError("Invalid fixture source")
        (installed / "foreign.ps1").write_text(
            'throw "Pinned MediaMTX executable failed its version probe."\n', encoding="ascii"
        )
        (installed / "fixture-stderr.py").write_text(
            "import sys\nsys.stderr.write('PRIVATE_ALL_STREAMS_SENTINEL')\nsys.exit(1)\n",
            encoding="ascii",
            newline="\n",
        )
        (installed / "foreign-runtime.ps1").write_text(
            "Set-StrictMode -Version Latest\n$null = $K5ForeignUndefinedVariable\n",
            encoding="ascii",
            newline="\n",
        )
        control = tmp_path / "control.ps1"
        observer = tmp_path / "observer.ps1"
        environment = {**os.environ, "TEMP": str(sessions), "TMP": str(sessions)}
        for case, fault in ERROR_BOUNDARY_CASES.items():
            injection = fault + '\nthrow "fixture_fault_did_not_terminate"\n'
            fixture = source.replace(marker, marker + injection, 1)
            if fixture.replace(injection, "", 1) != source or len(fixture.splitlines()) > 4096:
                raise ValueError("Invalid fixture source")
            target.write_text(fixture, encoding="ascii", newline="\n")
            bound = module.bind_start_envelope(hashlib.sha256(fixture.encode("ascii")).hexdigest())
            control.write_bytes(bound.encode("ascii"))
            observer.write_bytes(
                _observed_envelope(bound, module.START_OPERATION_ANCHORS).encode("ascii")
            )
            if control.read_bytes() != bound.encode("ascii"):
                raise ValueError("Invalid fixture envelope")
            outcomes = []
            for envelope, observed in ((control, False), (observer, True)):
                result = subprocess.run(
                    [
                        "powershell.exe",
                        "-NoLogo",
                        "-NoProfile",
                        "-NonInteractive",
                        "-File",
                        str(envelope),
                        "-Start",
                        str(target),
                        "-Port",
                        "8011",
                    ],
                    capture_output=True,
                    timeout=20,
                    check=False,
                    env=environment,
                )
                outcomes.append(_parse_error_boundary_output(module, result, observed=observed))
            projection, _ = outcomes[0]
            observed_projection, shape = outcomes[1]
            if (
                projection != observed_projection
                or not shape["observer_valid"]
                or shape["truncated"]
                or shape["records"] == 0
            ):
                raise ValueError("Observer changed projection or failed")
            if target.read_bytes() != fixture.encode("ascii"):
                raise ValueError("Fixture identity changed")
            if any(sessions.glob("K5VisionAlpha-*")):
                raise ValueError("Fixture crossed session boundary")
            # Emit only validated scalar records; no raw process output is shown.
            print("K5_ALPHA_ERROR_CASE=" + case)
            print(
                module.START_ERROR_PREFIX.decode() + json.dumps(projection, separators=(",", ":"))
            )
            print(ERROR_SHAPE_PREFIX.decode() + json.dumps(shape, separators=(",", ":")))
            expected_ids = {ERROR_BOUNDARY_EXPECTED_IDS[case]}
            if case == "native_all_streams":
                # Windows PowerShell may stop on redirected stderr before the
                # exact helper returns; otherwise exit 1 must become element refusal.
                expected_ids.add("native_stderr")
            if not expected_ids.intersection(
                {shape[f"record_{slot}_error_id"] for slot in range(4)}
            ):
                raise ValueError("Unexpected error boundary shape")
            recoverable = any(shape[f"record_{slot}_line_in_start"] for slot in range(4))
            if case in {"foreign_known", "foreign_runtime"}:
                if projection["origin"] != "unknown":
                    regressions.append(case)
            elif (
                (
                    case in {"missing_variable", "missing_property", "null_method"}
                    and projection["error_class"] != ERROR_BOUNDARY_EXPECTED_IDS[case]
                )
                or not recoverable
                or projection["origin"] != "start"
                or projection["operation"] == "unknown"
                or projection["error_class"] == "unknown"
            ):
                regressions.append(case)
        completed = True
    except Exception:
        pass  # Never include raw exception, source, child output or arguments.
    if not completed:
        pytest.fail("Post-admission ErrorRecord boundary fixture failed", pytrace=False)
    if regressions:
        print("K5_ALPHA_ERROR_PROJECTION_MISSES=" + str(len(regressions)))
        pytest.fail(
            "Post-admission ErrorRecord projection lost useful classification", pytrace=False
        )


def test_error_boundary_observer_preserves_all_original_envelope_bytes():
    module = _startup_witness()
    observed = _observed_envelope(module.ENVELOPE, module.START_OPERATION_ANCHORS)
    assert (
        observed.replace(_error_shape_observer(module.START_OPERATION_ANCHORS), "", 1)
        == module.ENVELOPE
    )
    assert observed.count("& $Start -Port $Port -ExitAfterPublicTest") == 1
    assert observed.count("    $fatal = $_\n") == 1
    assert (
        "ReferenceEquals" in ERROR_SHAPE_OBSERVER and "$shape.nodes -lt 8" in ERROR_SHAPE_OBSERVER
    )
    assert "$entry.Depth -gt 4" in ERROR_SHAPE_OBSERVER
    assert "$shape.records -ge 4" in ERROR_SHAPE_OBSERVER
    assert "Format-List" not in ERROR_SHAPE_OBSERVER and "ToString()" not in ERROR_SHAPE_OBSERVER
    test_version_guard_probe_is_hosted_only_and_keeps_existing_smoke_selection()


def test_error_boundary_shape_rejects_raw_duplicate_unbounded_or_coerced_values():
    value = {
        "schema_version": "alpha-error-boundary-shape-v1",
        "observer_valid": True,
        "records": 0,
        "nodes": 0,
        "cycle": False,
        "truncated": False,
        "source_read_ok": False,
        "source_anchors_ok": False,
        **{
            f"record_{slot}_{field}": False
            for slot in range(4)
            for field in ERROR_SHAPE_BOOL_FIELDS
        },
        **{f"record_{slot}_error_id": "absent" for slot in range(4)},
    }
    _validate_error_shape(value)
    for change in (
        {"private": "path"},
        {"nodes": True},
        {"nodes": 9},
        {"records": 5},
        {"record_0_error_id": "PRIVATE_NATIVE_SENTINEL"},
        {"record_0_invocation_present": 1},
        {"record_0_error_id": []},
        {"record_0_script_name_matches": True},
        {"schema_version": []},
    ):
        with pytest.raises(ValueError):
            _validate_error_shape({**value, **change})


def test_error_boundary_parser_rejects_raw_repeated_or_malformed_output():
    from types import SimpleNamespace

    module = _startup_witness()
    shape = {
        "schema_version": "alpha-error-boundary-shape-v1",
        "observer_valid": True,
        "records": 0,
        "nodes": 0,
        "cycle": False,
        "truncated": False,
        "source_read_ok": False,
        "source_anchors_ok": False,
        **{
            f"record_{slot}_{field}": False
            for slot in range(4)
            for field in ERROR_SHAPE_BOOL_FIELDS
        },
        **{f"record_{slot}_error_id": "absent" for slot in range(4)},
    }
    projection = {
        "schema_version": "alpha-start-error-v1",
        "phase": "primary",
        "origin": "unknown",
        "source_line": None,
        "operation": "unknown",
        "error_class": "unknown",
        "failure": "unknown",
    }
    admitted = b"K5 analytics configuration admitted; live provider acceptance is pending.\n"
    error = module.START_ERROR_PREFIX + json.dumps(projection).encode() + b"\n"
    metadata = ERROR_SHAPE_PREFIX + json.dumps(shape).encode() + b"\n"
    failed = b"K5_ALPHA_START_FAILED\n"
    raw = admitted + error + metadata + failed
    assert _parse_error_boundary_output(
        module, SimpleNamespace(returncode=24, stdout=raw, stderr=b""), observed=True
    ) == (projection, shape)
    duplicate_key = metadata.rstrip()[:-1] + b',"records":0}\n'
    bad_values = [
        (raw, b"PRIVATE_STDERR", 24),
        (raw, b"", 0),
        (raw + b"PRIVATE_STDOUT\n", b"", 24),
        (raw + error, b"", 24),
        (raw + metadata, b"", 24),
        (raw + admitted, b"", 24),
        (raw.replace(admitted, b""), b"", 24),
        (raw.replace(metadata, duplicate_key), b"", 24),
        (raw.replace(metadata, ERROR_SHAPE_PREFIX + b"[]\n"), b"", 24),
        (b"x" * 12_289, b"", 24),
    ]
    for stdout, stderr, code in bad_values:
        with pytest.raises((ValueError, TypeError)):
            _parse_error_boundary_output(
                module,
                SimpleNamespace(returncode=code, stdout=stdout, stderr=stderr),
                observed=True,
            )
    with pytest.raises(ValueError):
        _parse_error_boundary_output(
            module, SimpleNamespace(returncode=24, stdout=raw, stderr=b""), observed=False
        )


def test_all_streams_fixture_keeps_exact_gstreamer_probe_body():
    source = START.read_text()
    signature = "function Test-K5GStreamerElement([string]$Name) {\n"
    assert source.count(signature) == 1
    body = source.split(signature, 1)[1].split("\n}", 1)[0]
    assert body == "    & $gstInspect $Name *> $null\n    return $LASTEXITCODE -eq 0"
    fixture = ERROR_BOUNDARY_CASES["native_all_streams"]
    assert body in fixture
    assert signature not in fixture  # Existing source anchor remains unique.
    assert (
        'throw "Reviewed GStreamer runtime is missing required synthetic test element: ' in fixture
    )
    assert ERROR_BOUNDARY_EXPECTED_IDS["native_all_streams"] == "element_missing"


@pytest.mark.skipif(sys.platform != "win32", reason="Requires Windows typed ErrorRecord graphs")
def test_windows_error_selector_rejects_cycles_caps_and_competing_foreign_leaf(tmp_path: Path):
    import os

    module = _startup_witness()
    complete = False
    try:
        source = START.read_text()
        marker = "Set-StrictMode -Version Latest\n"
        fault = "if ($Port -eq 8012) { $null = 1 / 0 }\n$null = $K5FixtureUndefinedVariable\n"
        fixture = source.replace(marker, marker + fault, 1)
        start = tmp_path / "Start-K5VisionAlpha.ps1"
        start.write_text(fixture, encoding="ascii", newline="\n")
        foreign = tmp_path / "foreign-runtime.ps1"
        foreign.write_text(
            "Set-StrictMode -Version Latest\n$null = $K5ForeignUndefinedVariable\n",
            encoding="ascii",
            newline="\n",
        )
        bound = module.bind_start_envelope(hashlib.sha256(fixture.encode("ascii")).hexdigest())
        entry = "try {\n    & $Start -Port $Port -ExitAfterPublicTest\n"
        if bound.count(entry) != 1:
            raise ValueError("Invalid projection fixture")
        # Exact projection functions; do not execute the envelope's launcher tail.
        functions = bound.split(entry, 1)[0]
        harness = r"""
try {
    try { & $Start -Port $Port -ExitAfterPublicTest } catch { $owned = $_.Exception.ErrorRecord }
    try { & $Start -Port 8012 -ExitAfterPublicTest }
    catch { $arithmetic = $_.Exception.ErrorRecord }
    try { & (Join-Path (Split-Path -Parent $Start) 'foreign-runtime.ps1') }
    catch { $foreign = $_.Exception.ErrorRecord }
    if ($owned -isnot [System.Management.Automation.ErrorRecord] -or
        $foreign -isnot [System.Management.Automation.ErrorRecord] -or
        $arithmetic -isnot [System.Management.Automation.ErrorRecord] -or
        $null -eq $arithmetic.InvocationInfo -or
        $arithmetic.InvocationInfo.ScriptName -cne $Start -or
        $null -eq $owned.InvocationInfo -or $null -eq $foreign.InvocationInfo -or
        $owned.InvocationInfo.ScriptName -cne $Start -or
        $foreign.InvocationInfo.ScriptName -ceq $Start) { throw 'fixture_invalid' }
    # The two-argument RuntimeException constructor and virtual ErrorRecord
    # getter are present in Windows PowerShell 5.1. No newer three-argument API.
    $automationAssembly = [System.Management.Automation.RuntimeException].Assembly.Location
    Add-Type -ReferencedAssemblies $automationAssembly -TypeDefinition @'
using System;
using System.Management.Automation;
public sealed class K5FixtureRuntime : RuntimeException {
    public ErrorRecord Link;
    public K5FixtureRuntime(Exception inner) : base("PRIVATE_GRAPH_SENTINEL", inner) {}
    public override ErrorRecord ErrorRecord { get { return Link; } }
}
public sealed class K5FixtureMethod : MethodInvocationException {
    public ErrorRecord Link;
    public K5FixtureMethod() : base("PRIVATE_GRAPH_SENTINEL") {}
    public override ErrorRecord ErrorRecord { get { return Link; } }
}
'@
    function New-K5FixtureRecord([Exception]$Exception) {
        return [System.Management.Automation.ErrorRecord]::new(
            $Exception, 'PRIVATE_GRAPH_ID',
            [System.Management.Automation.ErrorCategory]::NotSpecified, $null)
    }
    $graphs = [Collections.Generic.List[object]]::new()
    $cycleException = [K5FixtureRuntime]::new($null)
    $cycle = New-K5FixtureRecord $cycleException
    $cycleException.Link = $cycle
    $graphs.Add($cycle)

    $depthException = [Exception]::new('PRIVATE_GRAPH_SENTINEL')
    foreach ($index in 1..5) {
        $depthException = [Exception]::new('PRIVATE_GRAPH_SENTINEL', $depthException)
    }
    $graphs.Add((New-K5FixtureRecord $depthException))

    $p0Exception = [System.Management.Automation.RuntimeException]::new('PRIVATE_GRAPH_SENTINEL')
    $p1Exception = [System.Management.Automation.RuntimeException]::new('PRIVATE_GRAPH_SENTINEL')
    $placeholder0 = $p0Exception.ErrorRecord
    $placeholder1 = $p1Exception.ErrorRecord
    if ($null -ne $placeholder0.InvocationInfo -or $null -ne $placeholder1.InvocationInfo -or
        $placeholder0.Exception -isnot
            [System.Management.Automation.ParentContainsErrorRecordException]) {
        throw 'fixture_invalid'
    }
    $tail = [Exception]::new('PRIVATE_GRAPH_SENTINEL', [Exception]::new('PRIVATE_GRAPH_SENTINEL'))
    $nodeInner = [K5FixtureRuntime]::new($tail)
    $nodeInner.Link = $placeholder1
    $nodeRoot = [K5FixtureRuntime]::new($nodeInner)
    $nodeRoot.Link = $placeholder0
    $graphs.Add((New-K5FixtureRecord $nodeRoot))

    $unexpected = [K5FixtureRuntime]::new($null)
    $unexpected.Link = New-K5FixtureRecord ([Exception]::new('PRIVATE_GRAPH_SENTINEL'))
    $graphs.Add((New-K5FixtureRecord $unexpected))

    # Independent leaves at unequal depths: choosing the deeper owned branch
    # would conceal the shallower foreign provenance and must remain unknown.
    $ownedBranch = [K5FixtureRuntime]::new($null)
    $ownedBranch.Link = $owned
    $foreignBranch = [K5FixtureRuntime]::new($ownedBranch)
    $foreignBranch.Link = $foreign
    $graphs.Add((New-K5FixtureRecord $foreignBranch))
    foreach ($graph in $graphs) {
        if ($null -ne (Get-K5SourceFailure -Root $graph)) { throw 'fixture_invalid' }
        Write-K5StartError -Failure $graph -Phase 'primary'
    }
    $singleBranch = [K5FixtureRuntime]::new($null)
    $singleBranch.Link = $owned
    $single = New-K5FixtureRecord $singleBranch
    if (-not [object]::ReferenceEquals((Get-K5SourceFailure -Root $single), $owned)) {
        throw 'fixture_invalid'
    }
    Write-K5StartError -Failure $single -Phase 'primary'
    $methodWrapper = [K5FixtureMethod]::new()
    $methodWrapper.Link = $arithmetic
    $methodRecord = New-K5FixtureRecord $methodWrapper
    if (-not [object]::ReferenceEquals((Get-K5SourceFailure -Root $methodRecord), $arithmetic)) {
        throw 'fixture_invalid'
    }
    Write-K5StartError -Failure $methodRecord -Phase 'primary'
    Write-Output 'K5_GRAPH_BOUNDS=passed'
    exit 0
} catch { exit 1 }
"""
        probe = tmp_path / "graph-probe.ps1"
        probe.write_text(functions + harness, encoding="ascii", newline="\n")
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-File",
                str(probe),
                "-Start",
                str(start),
                "-Port",
                "8011",
            ],
            capture_output=True,
            timeout=30,
            check=False,
            env={**os.environ, "TEMP": str(tmp_path), "TMP": str(tmp_path)},
        )
        if result.returncode != 0 or result.stderr or len(result.stdout) > 8192:
            raise ValueError("Invalid graph fixture")
        lines = result.stdout.splitlines()
        if len(lines) != 8 or lines[-1] != b"K5_GRAPH_BOUNDS=passed":
            raise ValueError("Invalid graph fixture")
        records = []
        for line in lines[:-1]:
            if not line.startswith(module.START_ERROR_PREFIX):
                raise ValueError("Invalid graph fixture")
            records.append(module.parse_start_error(line[len(module.START_ERROR_PREFIX) :]))
        for record in records[:-2]:
            if any(
                record[key] != "unknown"
                for key in ("origin", "operation", "error_class", "failure")
            ):
                raise ValueError("Invalid graph projection")
        for record, expected_class in zip(
            records[-2:], ("variable_undefined", "method_binding"), strict=True
        ):
            if record["origin"] != "start" or record["error_class"] != expected_class:
                raise ValueError("Invalid graph projection")
        complete = True
    except Exception:
        pass
    if not complete:
        pytest.fail("Typed ErrorRecord graph bounds fixture failed", pytrace=False)


ELEMENT_PROBE_PREFIX = b"K5_ELEMENT_PROBE="
ELEMENT_PROBE_SCHEMA = "element-native-exit-probe-v1"
ELEMENT_PROBE_OUTCOMES = {
    "true",
    "false",
    "variable_undefined",
    "native_stderr",
    "command_missing",
    "unexpected",
}
ELEMENT_PROBE_PYTHON_SOURCE = r"""
import os
import sys
import time

try:
    if not (sys.flags.isolated and sys.flags.no_site and sys.flags.dont_write_bytecode):
        os._exit(31)
    if (os.path.normcase(sys.base_prefix) != os.path.normcase(_fixture_prefix)
            or tuple(sys.version_info[:3]) != _fixture_version):
        os._exit(32)
    if _fixture_case not in ('zero', 'nonzero', 'stderr_zero', 'stderr_nonzero'):
        os._exit(33)
    time.sleep(0.5)
    if _fixture_case.startswith('stderr_'):
        if os.write(2, b'PRIVATE_NATIVE_FIXTURE') != 22:
            os._exit(34)
    os._exit(0 if _fixture_case in ('zero', 'stderr_zero') else 7)
except BaseException:
    os._exit(35)
"""


def _element_python_argument(script: Path, case: str) -> str:
    import base64

    if case not in {"zero", "nonzero", "stderr_zero", "stderr_nonzero"}:
        raise ValueError("Invalid fixed case")
    expected = ELEMENT_PROBE_PYTHON_SOURCE.encode("ascii")
    with script.open("rb") as stream:
        if stream.read(len(expected) + 1) != expected:
            raise ValueError("Invalid owned script identity")
    # A single native argument preserves the exact [string]$Name helper. CPython
    # parses clustered -I/-B/-S and an attached -c expression. The outer expression
    # contains no whitespace/double quotes, avoiding legacy PowerShell argv quoting.
    loader = (
        "import hashlib,os,sys\n"
        + f"with open({str(script)!r}, 'rb') as f: data = f.read(16385)\n"
        + f"if hashlib.sha256(data).hexdigest() != {hashlib.sha256(expected).hexdigest()!r}: "
        + "os._exit(36)\n"
        + f"_fixture_case={case!r}\n_fixture_prefix={sys.base_prefix!r}\n"
        + f"_fixture_version={sys.version_info[:3]!r}\n"
        + "exec(compile(data, '<owned-element-fixture>', 'exec'))\n"
    )
    encoded = base64.b64encode(loader.encode("utf-8")).decode("ascii")
    return "-IBScexec(__import__('base64').b64decode('" + encoded + "'))"


ELEMENT_CHILD_PREFIX = b"K5_ELEMENT_CHILD_FAILURE="
ELEMENT_CHILD_PHASES = {
    "source_select",
    "command_admission",
    "probe_invoke",
    "probe_record",
    "reference_start",
    "reference_wait",
    "reference_exit",
    "reference_cleanup",
}
ELEMENT_CHILD_ERRORS = {
    "unknown",
    "command_missing",
    "variable_undefined",
    "property_missing",
    "parameter_binding",
    "method_binding",
    "permission_denied",
    "file_missing",
    "io_error",
    "native_stderr",
    "timeout",
}
ELEMENT_CHILD_DIAGNOSTICS = r"""
function Write-K5ElementChildFailure([object]$Failure, [string]$Phase, [string]$Boundary) {
    $category = 'unknown'
    $code = $null
    if ($Failure -is [Management.Automation.ErrorRecord]) {
        $id = $Failure.FullyQualifiedErrorId.Split(',')[0]
        if ($id -ceq 'CommandNotFoundException') { $category = 'command_missing' }
        elseif ($id -ceq 'VariableIsUndefined') { $category = 'variable_undefined' }
        elseif ($id -cin @('PropertyNotFoundStrict','PropertyNotFound')) {
            $category = 'property_missing'
        }
        elseif ($id -cin @('NativeCommandError','NativeCommandErrorMessage')) {
            $category = 'native_stderr'
        }
        elseif ($Failure.Exception -is [Management.Automation.ParameterBindingException]) {
            $category = 'parameter_binding'
        } elseif ($Failure.Exception -is [Management.Automation.MethodInvocationException]) {
            $category = 'method_binding'
        }
        $exception = $Failure.Exception
        for ($depth = 0; $null -ne $exception -and $depth -lt 4; $depth++) {
            if ($exception -is [UnauthorizedAccessException]) { $category = 'permission_denied' }
            elseif ($exception -is [IO.FileNotFoundException]) { $category = 'file_missing' }
            elseif ($exception -is [TimeoutException]) { $category = 'timeout' }
            elseif ($exception -is [IO.IOException] -and $category -ceq 'unknown') {
                $category = 'io_error'
            }
            if ($depth -eq 0) { $code = [int]$exception.HResult }
            $exception = $exception.InnerException
        }
    }
    $record = [ordered]@{
        schema_version = 'element-child-failure-v1'
        phase = $Phase
        boundary = $Boundary
        error = $category
        hresult = $code
    }
    Write-Output ('K5_ELEMENT_CHILD_FAILURE=' + ($record | ConvertTo-Json -Compress))
}
"""
ELEMENT_PROBE_SCRIPT = r"""
param([string]$Start, [string]$Executable, [string]$Name, [string]$Variant, [string]$Initial)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
__ELEMENT_CHILD_DIAGNOSTICS__
$fixturePhase = 'source_select'
try {
    if ($Variant -cnotin @('original','pipeline') -or
        $Initial -cnotin @('absent','stale_zero','stale_nonzero')) { throw 'fixture_invalid' }
    if ($null -ne (Get-Variable LASTEXITCODE -Scope Global -ErrorAction SilentlyContinue)) {
        throw 'fixture_not_fresh'
    }
    $tokens = $null
    $errors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile(
        $Start, [ref]$tokens, [ref]$errors)
    $functions = @($ast.FindAll({ param($node)
        $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -ceq 'Test-K5GStreamerElement'
    }, $true))
    if ($errors.Count -ne 0 -or $functions.Count -ne 1) { throw 'fixture_invalid' }
    $original = $functions[0].Extent.Text
    $needle = '& $gstInspect $Name *> $null'
    if (($original.Split(@($needle), [StringSplitOptions]::None)).Count -ne 2) {
        throw 'fixture_invalid'
    }
    $selected = $original
    if ($Variant -ceq 'pipeline') {
        $selected = $original.Replace($needle, $needle + ' | Out-Null')
    }
    . ([scriptblock]::Create($selected))
    function K5FixtureNoNative { param($Name) }
    Set-Alias -Name K5FixtureAlias -Value K5FixtureNoNative
    $fixturePhase = 'command_admission'
    $gstInspect = $Executable
    $kind = 'missing'
    $bound = $false
    try {
        $command = Get-Command -Name $gstInspect -ErrorAction Stop
        if ($command.CommandType -eq [Management.Automation.CommandTypes]::Application) {
            $kind = 'application'
            $bound = [string]::Equals([IO.Path]::GetFullPath($command.Path),
                [IO.Path]::GetFullPath($Executable), [StringComparison]::OrdinalIgnoreCase)
        } elseif ($command.CommandType -eq [Management.Automation.CommandTypes]::Alias) {
            $kind = 'alias'
        } elseif ($command.CommandType -eq [Management.Automation.CommandTypes]::Function) {
            $kind = 'function'
        } else { throw 'fixture_invalid' }
    } catch {
        if ($kind -cne 'missing') { throw 'fixture_invalid' }
    }
    # Deliberately stale inputs are negative fixtures, never a proposed repair.
    if ($Initial -ceq 'stale_zero') { $global:LASTEXITCODE = 0 }
    elseif ($Initial -ceq 'stale_nonzero') { $global:LASTEXITCODE = 9 }
    $fixturePhase = 'probe_invoke'
    $outcome = 'unexpected'
    $watch = [Diagnostics.Stopwatch]::StartNew()
    try {
        $answer = Test-K5GStreamerElement -Name $Name
        if ($answer -is [bool]) { $outcome = if ($answer) { 'true' } else { 'false' } }
    } catch {
        $id = $_.FullyQualifiedErrorId.Split(',')[0]
        if ($id -ceq 'VariableIsUndefined') { $outcome = 'variable_undefined' }
        elseif ($id -cin @('NativeCommandError','NativeCommandErrorMessage')) {
            $outcome = 'native_stderr'
        } elseif ($id -ceq 'CommandNotFoundException') { $outcome = 'command_missing' }
    }
    $watch.Stop()
    $fixturePhase = 'probe_record'
    $last = Get-Variable LASTEXITCODE -Scope Global -ErrorAction SilentlyContinue
    $value = $null
    if ($null -ne $last) {
        if ($last.Value -isnot [int] -or $last.Value -notin @(0,7,9)) { throw 'fixture_invalid' }
        $value = $last.Value
    }
    $record = [ordered]@{
        schema_version = 'element-native-exit-probe-v1'
        variant = $Variant
        initial = $Initial
        fresh_session = $true
        command_kind = $kind
        application_bound = $bound
        outcome = $outcome
        last_exit = $value
        waited_floor = $watch.ElapsedMilliseconds -ge 400
    }
    Write-Output ('K5_ELEMENT_PROBE=' + ($record | ConvertTo-Json -Compress))
    exit 0
} catch { Write-K5ElementChildFailure $_ $fixturePhase 'primary'; exit 1 }
""".replace("__ELEMENT_CHILD_DIAGNOSTICS__", ELEMENT_CHILD_DIAGNOSTICS)
ELEMENT_REFERENCE_SCRIPT = r"""
import json
import subprocess
import sys
import threading
import time


def checkpoint(value):
    print('K5_ELEMENT_CHECKPOINT=' + value, flush=True)


def failure(phase, boundary, error):
    print('K5_ELEMENT_CHILD_FAILURE=' + json.dumps(dict(
        schema_version='element-child-failure-v1', phase=phase, boundary=boundary,
        error='timeout' if isinstance(error, subprocess.TimeoutExpired) else
              'permission_denied' if isinstance(error, PermissionError) else
              'file_missing' if isinstance(error, FileNotFoundError) else 'unknown',
        hresult=None,
    ), separators=(',', ':')), flush=True)


checkpoint('reference_entered')
child = None
readers = []
outputs = [None, None]
failed = False
phase = 'reference_start'
try:
    if len(sys.argv) != 4 or sys.argv[3] not in ('zero','nonzero','stderr_zero','stderr_nonzero'):
        raise ValueError()
    executable, argument, case = sys.argv[1:]
    checkpoint('reference_start_requested')
    child = subprocess.Popen([executable, argument], stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    checkpoint('reference_started')
    deadline = time.monotonic() + 10

    def read(index, stream):
        try:
            outputs[index] = stream.read(65)
        except BaseException:
            outputs[index] = None

    for index, stream in enumerate((child.stdout, child.stderr)):
        reader = threading.Thread(target=read, args=(index, stream), daemon=True)
        reader.start()
        readers.append(reader)
    phase = 'reference_wait'
    code = child.wait(timeout=5)
    checkpoint('reference_waited')
    for reader in readers:
        reader.join(max(0, min(5, deadline - time.monotonic())))
    if any(reader.is_alive() for reader in readers):
        raise subprocess.TimeoutExpired('owned', 5)
    phase = 'reference_exit'
    expected = b'PRIVATE_NATIVE_FIXTURE' if case.startswith('stderr_') else b''
    if outputs != [b'', expected] or code != (0 if case in ('zero','stderr_zero') else 7):
        raise ValueError()
    checkpoint('reference_stdio_verified')
    print('K5_NATIVE_REFERENCE=' + str(code), flush=True)
except BaseException as error:
    failed = True
    failure(phase, 'primary', error)
finally:
    try:
        if child is not None:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
            deadline = time.monotonic() + 5
            for reader in readers:
                reader.join(max(0, deadline - time.monotonic()))
            if any(reader.is_alive() for reader in readers):
                raise subprocess.TimeoutExpired('owned', 5)
            child.stdout.close()
            child.stderr.close()
        checkpoint('reference_cleanup_complete')
    except BaseException as error:
        failed = True
        failure('reference_cleanup', 'cleanup', error)
sys.exit(1 if failed else 0)
"""
ELEMENT_CHECKPOINT_PREFIX = b"K5_ELEMENT_CHECKPOINT="
ELEMENT_CHECKPOINTS = {
    "shell_control_entered",
    "shell_control_exit",
    "python_control_entered",
    "reference_entered",
    "reference_start_requested",
    "reference_started",
    "reference_waited",
    "reference_stdio_verified",
    "reference_cleanup_complete",
}
ELEMENT_ARGV_PREFIX = b"K5_ELEMENT_ARGV="


def _element_complete_records(raw):
    """Only complete fixed lines can survive a timeout; a suffix proves nothing."""
    if len(raw) > 4096:
        raise ValueError("Invalid checkpoint bytes")
    complete = raw[: raw.rfind(b"\n") + 1]
    checkpoints = []
    arguments = []
    for line in complete.splitlines():
        if line.startswith(ELEMENT_CHECKPOINT_PREFIX):
            value = line[len(ELEMENT_CHECKPOINT_PREFIX) :].decode("ascii")
            if value not in ELEMENT_CHECKPOINTS or value in checkpoints:
                raise ValueError("Invalid checkpoint")
            checkpoints.append(value)
        elif line.startswith(ELEMENT_ARGV_PREFIX):
            pairs = json.loads(line[len(ELEMENT_ARGV_PREFIX) :], object_pairs_hook=list)
            if (
                type(pairs) is not list
                or len(pairs) != 2
                or any(type(pair) is not tuple or len(pair) != 2 for pair in pairs)
            ):
                raise ValueError("Invalid argument observation")
            value = dict(pairs)
            if value.keys() != {"equal", "hash_equal"} or any(
                type(v) is not bool for v in value.values()
            ):
                raise ValueError("Invalid argument observation")
            arguments.append(value)
            if len(arguments) > 1:
                raise ValueError("Invalid argument observation")
    return complete, checkpoints, arguments


def _element_control_script(argument):
    expected = argument.replace("'", "''")
    digest = hashlib.sha256(argument.encode("ascii")).hexdigest()
    return r"""
param([string]$Name = '')
$ErrorActionPreference = 'Stop'
Write-Output 'K5_ELEMENT_CHECKPOINT=shell_control_entered'
if ($Name -cne '') {
    $equal = $Name -ceq '__EXPECTED__'
    $sha = [Security.Cryptography.SHA256]::Create()
    try { $bytes = $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($Name)) }
    finally { $sha.Dispose() }
    $actual = ([BitConverter]::ToString($bytes)).Replace('-', '').ToLowerInvariant()
    $hashEqual = $actual -ceq '__HASH__'
    $record = @{equal=$equal;hash_equal=$hashEqual}
    Write-Output ('K5_ELEMENT_ARGV=' + ($record | ConvertTo-Json -Compress))
    if (-not $equal -or -not $hashEqual) { exit 1 }
}
Write-Output 'K5_ELEMENT_CHECKPOINT=shell_control_exit'
exit 0
""".replace("__EXPECTED__", expected).replace("__HASH__", digest)


def _pe_fixture_subsystem(path: Path) -> int:
    import struct

    with path.open("rb") as stream:
        raw = stream.read(1_048_577)
    if len(raw) > 1_048_576 or len(raw) < 256 or raw[:2] != b"MZ":
        raise ValueError("Invalid owned fixture PE")
    offset = struct.unpack_from("<I", raw, 0x3C)[0]
    if offset + 94 > len(raw) or raw[offset : offset + 4] != b"PE\0\0":
        raise ValueError("Invalid owned fixture PE")
    if struct.unpack_from("<H", raw, offset + 24)[0] not in (0x10B, 0x20B):
        raise ValueError("Invalid owned fixture PE")
    return struct.unpack_from("<H", raw, offset + 24 + 68)[0]


def _parse_element_probe(raw: bytes) -> dict:
    if len(raw) > 2048 or len(raw.splitlines()) != 1 or not raw.startswith(ELEMENT_PROBE_PREFIX):
        raise ValueError("Invalid element probe")
    pairs = json.loads(raw[len(ELEMENT_PROBE_PREFIX) :], object_pairs_hook=list)
    if type(pairs) is not list or any(type(pair) is not tuple or len(pair) != 2 for pair in pairs):
        raise ValueError("Invalid element probe")
    value = dict(pairs)
    if len(value) != len(pairs) or value.keys() != {
        "schema_version",
        "variant",
        "initial",
        "fresh_session",
        "command_kind",
        "application_bound",
        "outcome",
        "last_exit",
        "waited_floor",
    }:
        raise ValueError("Invalid element probe")
    for field, allowed in (
        ("schema_version", {ELEMENT_PROBE_SCHEMA}),
        ("variant", {"original", "pipeline"}),
        ("initial", {"absent", "stale_zero", "stale_nonzero"}),
        ("command_kind", {"application", "missing", "alias", "function"}),
        ("outcome", ELEMENT_PROBE_OUTCOMES),
    ):
        if type(value[field]) is not str or value[field] not in allowed:
            raise ValueError("Invalid element probe")
    for field in ("fresh_session", "application_bound", "waited_floor"):
        if type(value[field]) is not bool:
            raise ValueError("Invalid element probe")
    if value["last_exit"] is not None and (
        type(value["last_exit"]) is not int or value["last_exit"] not in (0, 7, 9)
    ):
        raise ValueError("Invalid element probe")
    if value["application_bound"] and value["command_kind"] != "application":
        raise ValueError("Invalid element probe")
    return value


ELEMENT_FIXTURE_PREFIX = "K5_ELEMENT_FIXTURE_DIAGNOSTIC="
ELEMENT_FIXTURE_PHASES = {
    "shell_devnull_control",
    "shell_captured_control",
    "python_control",
    "argv_control",
    "module_load",
    "runtime_admission",
    "environment",
    "pe_admission",
    "fixture_scripts",
    "reference",
    "probe",
    "negative",
    "matrix",
}
ELEMENT_FIXTURE_ERRORS = {
    "none",
    "unexpected",
    "contract",
    "permission_denied",
    "file_missing",
    "os_error",
    "admission_failed",
    "identity_mismatch",
    "child_failed",
    "child_timeout",
    "output_invalid",
    "cleanup_incomplete",
}


def _element_context(phase, kind="none", case="none", variant="none", initial="none"):
    return dict(phase=phase, kind=kind, case=case, variant=variant, initial=initial)


def _validate_element_diagnostic(value):
    allowed = {
        "schema_version": {"element-fixture-diagnostic-v1"},
        "phase": ELEMENT_FIXTURE_PHASES,
        "kind": {"none", "ConsoleApplication", "WindowsApplication", "missing", "alias"},
        "case": {"none", "zero", "nonzero", "stderr_zero", "stderr_nonzero"},
        "variant": {"none", "original", "pipeline"},
        "initial": {"none", "absent", "stale_zero", "stale_nonzero"},
        "boundary": {"primary", "owned_cleanup", "child_primary", "child_cleanup"},
        "status": {"started", "passed", "failed"},
        "error": ELEMENT_FIXTURE_ERRORS,
        "child_phase": {"none"} | ELEMENT_CHILD_PHASES,
        "child_error": {"none"} | ELEMENT_CHILD_ERRORS,
        "gate_state": {
            "none",
            "not_used",
            "waiting",
            "opened",
            "started",
            "exited",
            "launch_failed",
            "refused",
        },
    }
    if type(value) is not dict or value.keys() != allowed.keys() | {
        "child_exit",
        "relay_exit",
        "timed_out",
        "hresult",
    }:
        raise ValueError("Invalid element diagnostic")
    for key, choices in allowed.items():
        if type(value[key]) is not str or value[key] not in choices:
            raise ValueError("Invalid element diagnostic")
    if type(value["timed_out"]) is not bool:
        raise ValueError("Invalid element diagnostic")
    for key in ("child_exit", "relay_exit", "hresult"):
        if value[key] is not None and (
            type(value[key]) is not int or not -(2**31) <= value[key] < 2**32
        ):
            raise ValueError("Invalid element diagnostic")


def _element_diagnostic(
    context, status, error=None, *, common=None, boundary="primary", child=None
):
    record = dict(
        schema_version="element-fixture-diagnostic-v1",
        **context,
        boundary=boundary,
        status=status,
        error="none",
        child_phase="none",
        child_error="none",
        hresult=None,
        child_exit=None,
        relay_exit=None,
        timed_out=False,
        gate_state="none",
    )
    if error is not None:
        record["error"] = (
            "permission_denied"
            if isinstance(error, PermissionError)
            else "file_missing"
            if isinstance(error, FileNotFoundError)
            else "os_error"
            if isinstance(error, OSError)
            else "contract"
            if isinstance(error, ValueError)
            else "unexpected"
        )
        if common is not None and type(error) is common.WitnessError:
            if len(error.args) == 1 and error.args[0] in ELEMENT_FIXTURE_ERRORS:
                record["error"] = error.args[0]
            if error.diagnostic is not None:
                common.validate_diagnostic(error.diagnostic)
                record.update(
                    child_exit=error.diagnostic["child_exit_code"],
                    relay_exit=error.diagnostic["relay_exit_code"],
                    timed_out=error.diagnostic["timed_out"],
                    gate_state=error.diagnostic["gate_state"],
                )
    if child is not None:
        record.update(
            child_phase=child["phase"], child_error=child["error"], hresult=child["hresult"]
        )
    _validate_element_diagnostic(record)
    print(ELEMENT_FIXTURE_PREFIX + json.dumps(record, separators=(",", ":")))
    return record


def _element_child_records(raw):
    if len(raw) > 4096:
        raise ValueError("Invalid child diagnostics")
    records = []
    for line in raw.splitlines():
        if not line.startswith(ELEMENT_CHILD_PREFIX):
            continue  # Unrecognized native output is never echoed.
        pairs = json.loads(line[len(ELEMENT_CHILD_PREFIX) :], object_pairs_hook=list)
        if type(pairs) is not list or any(
            type(pair) is not tuple or len(pair) != 2 for pair in pairs
        ):
            raise ValueError("Invalid child diagnostics")
        value = dict(pairs)
        if len(value) != len(pairs) or value.keys() != {
            "schema_version",
            "phase",
            "boundary",
            "error",
            "hresult",
        }:
            raise ValueError("Invalid child diagnostics")
        for field, allowed in (
            ("schema_version", {"element-child-failure-v1"}),
            ("phase", ELEMENT_CHILD_PHASES),
            ("boundary", {"primary", "cleanup"}),
            ("error", ELEMENT_CHILD_ERRORS),
        ):
            if type(value[field]) is not str or value[field] not in allowed:
                raise ValueError("Invalid child diagnostics")
        if value["hresult"] is not None and (
            type(value["hresult"]) is not int or not -(2**31) <= value["hresult"] < 2**31
        ):
            raise ValueError("Invalid child diagnostics")
        records.append(value)
        if len(records) > 3 or sum(r["boundary"] == "primary" for r in records) > 1:
            raise ValueError("Invalid child diagnostics")
    return records


class _ElementCaptureFailure(Exception):
    """All underlying failures have already been projected into fixed records."""


def _capture_element_child(common, arguments, *, cwd, env, context):
    # Reuse the reviewed relay/Job, with the same bounded stdout and wait limits
    # as common.capture. Keep primary and cleanup errors separately observable.
    import threading

    owned = reader = stream = None
    reader_started = False
    result = bytearray()
    read_failed = []
    failed = False

    def emit(status, error=None, **kwargs):
        nonlocal failed
        try:
            _element_diagnostic(context, status, error, common=common, **kwargs)
        except Exception:
            failed = True  # Reporting cannot bypass ownership cleanup or yield success.

    emit("started")
    if failed:
        raise _ElementCaptureFailure from None
    try:
        owned = common.OwnedProcess(
            arguments,
            cwd=cwd,
            env=env,
            operation="probe_admission",
            stdout=subprocess.PIPE,
        )
        stream = owned.process.stdout

        def read():
            try:
                while len(result) < 4097:
                    block = stream.read1(4097 - len(result))
                    if not block:
                        break
                    result.extend(block)
            except Exception:
                read_failed.append(True)

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        reader_started = True
        reader.join(15)
        if reader.is_alive():
            raise owned.failure("child_timeout", timed_out=True)
        if read_failed or len(result) > 4096:
            raise common.WitnessError("output_invalid")
        owned.wait(5)
    except Exception as error:
        failed = True
        emit("failed", error)
    finally:
        cleanup_failed = False
        if owned is not None:
            try:
                owned.close()
            except Exception as error:
                failed = cleanup_failed = True
                emit("failed", error, boundary="owned_cleanup")
        if reader_started:
            reader.join(5)
            if reader.is_alive():
                failed = cleanup_failed = True
                emit("failed", common.WitnessError("cleanup_incomplete"), boundary="owned_cleanup")
        if stream is not None and (not reader_started or not reader.is_alive()):
            try:
                stream.close()
            except Exception as error:
                failed = cleanup_failed = True
                emit("failed", error, boundary="owned_cleanup")
        if owned is not None and not cleanup_failed:
            emit("passed", boundary="owned_cleanup")
    # The buffer is inspected only after the reader ends, including after a
    # timed-out Job was closed. No partial/raw suffix is ever printed.
    if reader_started and not reader.is_alive():
        try:
            complete, checkpoints, arguments = _element_complete_records(bytes(result))
            if not failed and len(complete) != len(result):
                raise common.WitnessError("output_invalid")
            for child in _element_child_records(complete):
                failed = True
                emit("failed", boundary="child_" + child["boundary"], child=child)
            for checkpoint in checkpoints:
                print(ELEMENT_CHECKPOINT_PREFIX.decode() + checkpoint)
            for argument in arguments:
                print(ELEMENT_ARGV_PREFIX.decode() + json.dumps(argument, separators=(",", ":")))
                if argument != {"equal": True, "hash_equal": True}:
                    failed = True
        except Exception as error:
            failed = True
            emit("failed", error)
    if failed:
        raise _ElementCaptureFailure from None
    return bytes(result)


def _element_devnull_control(common, arguments, *, cwd, env, context):
    _element_diagnostic(context, "started")
    try:
        common.run(arguments, cwd=cwd, env=env, operation="probe_admission", seconds=15)
    except Exception as error:
        try:
            _element_diagnostic(context, "failed", error, common=common)
            known = type(error) is common.WitnessError
            if known and error.args in (("child_failed",), ("child_timeout",)):
                _element_diagnostic(context, "passed", boundary="owned_cleanup")
            elif known and error.args == ("cleanup_incomplete",):
                _element_diagnostic(
                    context, "failed", error, common=common, boundary="owned_cleanup"
                )
            # Other exceptions do not establish whether ownership cleanup finished.
        except Exception:
            pass
        raise _ElementCaptureFailure from None
    _element_diagnostic(context, "passed", boundary="owned_cleanup")
    _element_diagnostic(context, "passed")


@pytest.mark.skipif(sys.platform != "win32", reason="Requires Windows GUI/CUI process semantics")
def test_windows_exact_element_probe_uses_fresh_actual_native_exit(tmp_path: Path):
    import os

    common = None
    context = _element_context("module_load")
    complete = False
    original_misses = pipeline_misses = missing_refusals = 0
    try:
        module = _startup_witness()
        common = module.common
        context = _element_context("runtime_admission")
        base = common.local_path(Path(sys._base_executable))
        base_hash = common.file_hash(base)
        supplied = {key: os.environ.get(key) for key in common.GATE_RUNTIME_KEYS}
        if any(value is not None for value in supplied.values()) and supplied != {
            "K5_WITNESS_BASE_PYTHON": str(base),
            "K5_WITNESS_BASE_PYTHON_SHA256": base_hash,
        }:
            raise ValueError("Invalid fixture base binding")
        context = _element_context("environment")
        env = module.clean_environment(dict(os.environ), tmp_path)
        env.update(K5_WITNESS_BASE_PYTHON=str(base), K5_WITNESS_BASE_PYTHON_SHA256=base_hash)
        for name in ("TEMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA"):
            Path(env[name]).mkdir(parents=True, exist_ok=True)
        common.admitted_gate_python(env)
        powershell = common.local_path(
            Path(env["SYSTEMROOT"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        )
        context = _element_context("fixture_scripts")
        fixture = tmp_path / "owned-element-fixture.py"
        fixture.write_bytes(ELEMENT_PROBE_PYTHON_SOURCE.encode("ascii"))

        def capture(script, *arguments):
            return _capture_element_child(
                common,
                [
                    str(powershell),
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-File",
                    str(script),
                    *map(str, arguments),
                ],
                cwd=tmp_path,
                env=env,
                context=context,
            )

        binaries = {}
        for kind, candidate, expected_subsystem in (
            ("ConsoleApplication", base, 3),
            ("WindowsApplication", base.with_name("pythonw.exe"), 2),
        ):
            context = _element_context("pe_admission", kind)
            executable = common.local_path(candidate)
            if (
                executable.parent != base.parent
                or _pe_fixture_subsystem(executable) != expected_subsystem
            ):
                raise ValueError("Invalid admitted runtime subsystem")
            binaries[kind] = (executable, common.file_hash(executable))
        if binaries["ConsoleApplication"][1] != base_hash:
            raise ValueError("Admitted base runtime changed")
        context = _element_context("fixture_scripts")
        reference = tmp_path / "reference.py"
        reference.write_text(ELEMENT_REFERENCE_SCRIPT, encoding="ascii", newline="\n")
        probe = tmp_path / "probe.ps1"
        probe.write_text(ELEMENT_PROBE_SCRIPT, encoding="ascii", newline="\n")
        control = tmp_path / "control.ps1"
        control_argument = _element_python_argument(fixture, "zero")
        control.write_text(
            _element_control_script(control_argument), encoding="ascii", newline="\n"
        )
        control_command = [
            str(powershell),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(control),
        ]
        context = _element_context("shell_devnull_control")
        _element_devnull_control(common, control_command, cwd=tmp_path, env=env, context=context)
        context = _element_context("shell_captured_control")
        output = capture(control)
        if output.splitlines() != [
            b"K5_ELEMENT_CHECKPOINT=shell_control_entered",
            b"K5_ELEMENT_CHECKPOINT=shell_control_exit",
        ]:
            raise ValueError("Invalid shell control")
        context = _element_context("python_control")
        output = _capture_element_child(
            common,
            [
                str(base),
                "-I",
                "-B",
                "-S",
                "-c",
                "print('K5_ELEMENT_CHECKPOINT=python_control_entered',flush=True);"
                "raise SystemExit(0)",
            ],
            cwd=tmp_path,
            env=env,
            context=context,
        )
        if output.strip() != b"K5_ELEMENT_CHECKPOINT=python_control_entered":
            raise ValueError("Invalid Python control")
        context = _element_context("argv_control")
        output = capture(control, "-Name:", control_argument)
        _, checkpoints, arguments = _element_complete_records(output)
        if checkpoints != ["shell_control_entered", "shell_control_exit"] or arguments != [
            {"equal": True, "hash_equal": True},
        ]:
            raise ValueError("Invalid argument control")
        for kind, (executable, identity) in binaries.items():
            for name in ("zero", "nonzero", "stderr_zero", "stderr_nonzero"):
                expected_exit = 0 if name.endswith("zero") and not name.endswith("nonzero") else 7
                context = _element_context("reference", kind, name)
                if common.file_hash(executable) != identity:
                    raise ValueError("Admitted runtime changed")
                argument = _element_python_argument(fixture, name)
                if reference.read_bytes() != ELEMENT_REFERENCE_SCRIPT.encode("ascii"):
                    raise ValueError("Invalid oracle identity")
                raw = _capture_element_child(
                    common,
                    [str(base), "-I", "-B", "-S", str(reference), str(executable), argument, name],
                    cwd=tmp_path,
                    env=env,
                    context=context,
                )
                complete, checkpoints, observations = _element_complete_records(raw)
                if (
                    checkpoints
                    != [
                        "reference_entered",
                        "reference_start_requested",
                        "reference_started",
                        "reference_waited",
                        "reference_stdio_verified",
                        "reference_cleanup_complete",
                    ]
                    or observations
                ):
                    raise ValueError("Invalid oracle checkpoints")
                actual = b"\n".join(
                    line
                    for line in complete.splitlines()
                    if not line.startswith(ELEMENT_CHECKPOINT_PREFIX)
                ).strip()
                if actual != b"K5_NATIVE_REFERENCE=" + str(expected_exit).encode():
                    raise ValueError("Actual reference exit mismatch")
                print("K5_NATIVE_REFERENCE_CASE=" + kind + ":" + name + ":" + str(expected_exit))
                states = (
                    ("absent", "stale_zero", "stale_nonzero")
                    if not name.startswith("stderr")
                    else ("absent",)
                )
                for initial in states:
                    results = {}
                    for variant in ("original", "pipeline"):
                        context = _element_context("probe", kind, name, variant, initial)
                        if common.file_hash(executable) != identity:
                            raise ValueError("Owned executable changed")
                        if _element_python_argument(fixture, name) != argument:
                            raise ValueError("Owned script changed")
                        result = _parse_element_probe(
                            capture(
                                probe,
                                "-Start",
                                START,
                                "-Executable",
                                executable,
                                "-Name:",
                                argument,
                                "-Variant",
                                variant,
                                "-Initial",
                                initial,
                            )
                        )
                        if (
                            not result["fresh_session"]
                            or not result["application_bound"]
                            or result["command_kind"] != "application"
                        ):
                            raise ValueError("Owned application identity missing")
                        print("K5_ELEMENT_CASE=" + kind + ":" + name + ":" + initial)
                        print(
                            ELEMENT_PROBE_PREFIX.decode()
                            + json.dumps(result, separators=(",", ":"))
                        )
                        results[variant] = result
                    for variant, result in results.items():
                        if name.startswith("stderr"):
                            correct = result["outcome"] == "native_stderr"
                        else:
                            correct = (
                                result["outcome"] == ("true" if expected_exit == 0 else "false")
                                and result["last_exit"] == expected_exit
                                and result["waited_floor"]
                            )
                        if not correct:
                            if variant == "original":
                                original_misses += 1
                            else:
                                pipeline_misses += 1
        for command, expected_kind in (
            (tmp_path / "missing.exe", "missing"),
            ("K5FixtureAlias", "alias"),
        ):
            for initial in ("absent", "stale_zero", "stale_nonzero"):
                for variant in ("original", "pipeline"):
                    context = _element_context("negative", expected_kind, "zero", variant, initial)
                    result = _parse_element_probe(
                        capture(
                            probe,
                            "-Start",
                            START,
                            "-Executable",
                            command,
                            "-Name:",
                            "zero",
                            "-Variant",
                            variant,
                            "-Initial",
                            initial,
                        )
                    )
                    if result["command_kind"] != expected_kind or result["application_bound"]:
                        raise ValueError("Non-application qualified as an owned executable")
                    if expected_kind == "missing" and initial == "stale_zero":
                        missing_refusals += int(result["outcome"] == "command_missing")
                    # Observe substitution behavior; do not mislabel it as helper rejection.
                    print("K5_ELEMENT_CASE=" + expected_kind + ":zero:" + initial)
                    print(ELEMENT_PROBE_PREFIX.decode() + json.dumps(result, separators=(",", ":")))
        complete = True
    except _ElementCaptureFailure:
        pass  # Primary and cleanup diagnostics were both emitted before reaching here.
    except Exception as error:
        try:
            _element_diagnostic(context, "failed", error, common=common)
        except Exception:
            pass  # Fixed pytest failure below; never expose a chained setup/printing error.
    if not complete:
        pytest.fail("Owned native element-probe fixture failed", pytrace=False)
    try:
        print("K5_ELEMENT_ORIGINAL_MISSES=" + str(original_misses))
        print("K5_ELEMENT_PIPELINE_MISSES=" + str(pipeline_misses))
        print("K5_ELEMENT_MISSING_REFUSALS=" + str(missing_refusals))
        matrix_failed = bool(original_misses or pipeline_misses or missing_refusals != 2)
        _element_diagnostic(
            _element_context("matrix"),
            "failed" if matrix_failed else "passed",
            ValueError() if matrix_failed else None,
        )
    except Exception:
        pytest.fail("Owned native element-probe reporting failed", pytrace=False)
    if missing_refusals != 2:
        pytest.fail("Missing executable did not refuse under stale-zero state", pytrace=False)
    if original_misses:
        pytest.fail(
            "Original element probe did not bind its own native process exit", pytrace=False
        )
    if pipeline_misses:
        pytest.fail(
            "Candidate pipeline did not preserve actual exit/stderr semantics", pytrace=False
        )


def test_element_probe_parser_rejects_raw_duplicate_and_coerced_evidence():
    value = {
        "schema_version": ELEMENT_PROBE_SCHEMA,
        "variant": "original",
        "initial": "absent",
        "fresh_session": True,
        "command_kind": "application",
        "application_bound": True,
        "outcome": "variable_undefined",
        "last_exit": None,
        "waited_floor": False,
    }
    raw = ELEMENT_PROBE_PREFIX + json.dumps(value).encode() + b"\n"
    assert _parse_element_probe(raw) == value
    for change in (
        {"raw": "PRIVATE_VALUE"},
        {"outcome": "PRIVATE_VALUE"},
        {"last_exit": True},
        {"last_exit": 8},
        {"application_bound": 1},
        {"waited_floor": "true"},
        {"command_kind": "alias"},
        {"variant": []},
        {"initial": "guessed"},
    ):
        with pytest.raises(ValueError):
            _parse_element_probe(ELEMENT_PROBE_PREFIX + json.dumps({**value, **change}).encode())
    for bad in (
        raw + raw,
        b"PRIVATE_RAW\n" + raw,
        b"x" * 2049,
        ELEMENT_PROBE_PREFIX + b"[]",
        raw.rstrip()[:-1] + b',"last_exit":null}',
    ):
        with pytest.raises(ValueError):
            _parse_element_probe(bad)


def test_owned_fixture_pe_reader_checks_subsystem_and_bounds(tmp_path: Path):
    import struct

    raw = bytearray(512)
    raw[:2] = b"MZ"
    struct.pack_into("<I", raw, 0x3C, 128)
    raw[128:132] = b"PE\0\0"
    path = tmp_path / "owned-fixture.exe"
    for magic in (0x10B, 0x20B):
        for subsystem in (2, 3):
            struct.pack_into("<H", raw, 152, magic)
            struct.pack_into("<H", raw, 220, subsystem)
            path.write_bytes(raw)
            assert _pe_fixture_subsystem(path) == subsystem
    for bad in (b"MZ", b"x" * 512, bytes(raw[:200]), b"x" * 1_048_577):
        path.write_bytes(bad)
        with pytest.raises(ValueError):
            _pe_fixture_subsystem(path)


def test_element_probe_comparison_changes_only_downstream_pipeline():
    assert "$functions.Count -ne 1" in ELEMENT_PROBE_SCRIPT
    assert "$node.Name -ceq 'Test-K5GStreamerElement'" in ELEMENT_PROBE_SCRIPT
    assert "$selected = $original.Replace($needle, $needle + ' | Out-Null')" in ELEMENT_PROBE_SCRIPT
    assert "Get-Variable LASTEXITCODE -Scope Global" in ELEMENT_PROBE_SCRIPT
    assert "$global:LASTEXITCODE = 0" in ELEMENT_PROBE_SCRIPT  # Explicit stale-input case only.
    assert (
        "subprocess.Popen([executable, argument], stdin=subprocess.DEVNULL"
        in ELEMENT_REFERENCE_SCRIPT
    )
    assert "child.wait(timeout=5)" in ELEMENT_REFERENCE_SCRIPT
    assert "child.kill()" in ELEMENT_REFERENCE_SCRIPT
    assert "stream.read(65)" in ELEMENT_REFERENCE_SCRIPT
    assert "outputs != [b'', expected]" in ELEMENT_REFERENCE_SCRIPT
    test_version_guard_probe_is_hosted_only_and_keeps_existing_smoke_selection()


def test_element_fixture_setup_failure_reports_phase_without_raw_error(
    tmp_path, monkeypatch, capsys
):
    from types import SimpleNamespace

    def fail(_path):
        raise RuntimeError("PRIVATE_SETUP_PATH_OR_ERROR")

    monkeypatch.setitem(
        test_windows_exact_element_probe_uses_fresh_actual_native_exit.__globals__,
        "_startup_witness",
        lambda: SimpleNamespace(
            common=SimpleNamespace(
                local_path=fail, WitnessError=type("FixedFailure", (Exception,), {})
            )
        ),
    )
    with pytest.raises(pytest.fail.Exception, match="Owned native element-probe fixture failed"):
        test_windows_exact_element_probe_uses_fresh_actual_native_exit(tmp_path)
    output = capsys.readouterr()
    assert "PRIVATE_SETUP" not in output.out + output.err
    lines = output.out.splitlines()
    assert lines
    record = json.loads(lines[-1].removeprefix("K5_ELEMENT_FIXTURE_DIAGNOSTIC="))
    assert record["phase"] == "runtime_admission"
    assert record["status"] == "failed"
    assert record["error"] == "unexpected"


def _fixture_records(output):
    records = [
        json.loads(line[len(ELEMENT_FIXTURE_PREFIX) :])
        for line in output.splitlines()
        if line.startswith(ELEMENT_FIXTURE_PREFIX)
    ]
    for record in records:
        _validate_element_diagnostic(record)
    return records


def test_element_fixture_diagnostic_contract_rejects_raw_and_coerced_values(capsys):
    value = _element_diagnostic(_element_context("reference", "WindowsApplication"), "started")
    assert _fixture_records(capsys.readouterr().out) == [value]
    for patch in (
        {"path": "PRIVATE_PATH"},
        {"phase": "PRIVATE_PHASE"},
        {"kind": []},
        {"error": "PRIVATE_ERROR"},
        {"child_phase": "PRIVATE_SOURCE"},
        {"hresult": True},
        {"child_exit": 2**32},
        {"relay_exit": -(2**31) - 1},
        {"timed_out": 1},
        {"child_error": {"message": "PRIVATE_MESSAGE"}},
    ):
        with pytest.raises(ValueError):
            _validate_element_diagnostic({**value, **patch})


def test_element_child_failure_parser_is_strict_and_source_free():
    value = dict(
        schema_version="element-child-failure-v1",
        phase="reference_start",
        boundary="primary",
        error="method_binding",
        hresult=-2146233087,
    )
    raw = ELEMENT_CHILD_PREFIX + json.dumps(value).encode() + b"\n"
    assert _element_child_records(b"PRIVATE_RAW_COMPILER_OUTPUT\n" + raw) == [value]
    for patch in (
        {"message": "PRIVATE"},
        {"phase": "PRIVATE_PATH"},
        {"boundary": "PRIVATE"},
        {"error": "PRIVATE"},
        {"hresult": True},
        {"hresult": 2**31},
        {"phase": []},
    ):
        with pytest.raises(ValueError):
            _element_child_records(ELEMENT_CHILD_PREFIX + json.dumps({**value, **patch}).encode())
    for bad in (
        raw + raw,
        b"x" * 4097,
        ELEMENT_CHILD_PREFIX + b"[]",
        raw.rstrip()[:-1] + b',"hresult":0}',
    ):
        with pytest.raises(ValueError):
            _element_child_records(bad)


@pytest.mark.parametrize(
    "mode", ["nonzero", "timeout", "dual", "cleanup_only", "malformed", "oversize", "child_zero"]
)
def test_element_capture_preserves_primary_and_owned_cleanup(mode, monkeypatch, tmp_path, capsys):
    import io
    from types import SimpleNamespace

    common = _startup_witness().common
    closed = []
    raw = b"PRIVATE_RAW_OUTPUT"
    if mode in {"nonzero", "dual", "child_zero"}:
        raw += (
            b"\n"
            + ELEMENT_CHILD_PREFIX
            + json.dumps(
                dict(
                    schema_version="element-child-failure-v1",
                    phase="reference_start",
                    boundary="primary",
                    error="method_binding",
                    hresult=-2146233087,
                )
            ).encode()
            + b"\n"
        )
    elif mode == "malformed":
        raw = ELEMENT_CHILD_PREFIX + b'{"PRIVATE_PATH": true}\n'
    elif mode == "oversize":
        raw = b"PRIVATE" * 700

    class Owned:
        def __init__(self, *args, **kwargs):
            self.process = SimpleNamespace(stdout=io.BytesIO(raw))

        def wait(self, seconds):
            assert seconds == 5
            if mode in {"nonzero", "dual", "timeout"}:
                timeout = mode == "timeout"
                raise common.WitnessError(
                    "child_timeout" if timeout else "child_failed",
                    common.diagnostic(
                        "probe_admission",
                        child_exit_code=None if timeout else 1,
                        relay_exit_code=None if timeout else 1,
                        timed_out=timeout,
                        gate_state="started" if timeout else "exited",
                    ),
                )

        def close(self):
            closed.append(True)
            if mode in {"dual", "cleanup_only"}:
                raise common.WitnessError("cleanup_incomplete")

    monkeypatch.setattr(common, "OwnedProcess", Owned)
    with pytest.raises(_ElementCaptureFailure):
        _capture_element_child(
            common,
            ["PRIVATE_COMMAND"],
            cwd=tmp_path,
            env={},
            context=_element_context("reference", "ConsoleApplication"),
        )
    output = capsys.readouterr()
    assert "PRIVATE" not in output.out + output.err
    assert closed == [True]
    records = _fixture_records(output.out)
    assert all(record["phase"] == "reference" for record in records)
    failures = [record for record in records if record["status"] == "failed"]
    assert failures
    if mode in {"nonzero", "dual"}:
        assert failures[0]["error"] == "child_failed"
        assert any(record["child_error"] == "method_binding" for record in failures)
        primary = next(record for record in failures if record["boundary"] == "primary")
        assert primary["error"] == "child_failed"
        assert primary["child_exit"] == primary["relay_exit"] == 1
    if mode == "timeout":
        assert failures[0]["timed_out"] and failures[0]["error"] == "child_timeout"
    if mode == "dual":
        cleanup = next(record for record in failures if record["boundary"] == "owned_cleanup")
        assert cleanup["error"] == "cleanup_incomplete"


def test_element_fixture_child_contexts_and_reference_cleanup_are_fixed():
    source = Path(__file__).read_text()
    section = source.split("def test_windows_exact_element_probe_uses_fresh_actual_native_exit", 1)[
        1
    ]
    section = section.split("def test_element_probe_parser", 1)[0]
    for phase in ELEMENT_FIXTURE_PHASES:
        assert f'_element_context("{phase}"' in section
    assert "_capture_element_child(" in section and "common.capture(" not in section
    assert "failure(phase, 'primary', error)" in ELEMENT_REFERENCE_SCRIPT
    assert "failure('reference_cleanup', 'cleanup', error)" in ELEMENT_REFERENCE_SCRIPT
    assert ELEMENT_REFERENCE_SCRIPT.index("'primary'") < ELEMENT_REFERENCE_SCRIPT.index("'cleanup'")
    assert "sys.exit(1 if failed else 0)" in ELEMENT_REFERENCE_SCRIPT
    assert "Add-Type" not in section and "ELEMENT_COMPILE_SCRIPT" not in section
    assert 'base.with_name("pythonw.exe")' in section


def test_element_fixture_module_load_failure_is_fixed(tmp_path, monkeypatch, capsys):
    def fail():
        raise ImportError("PRIVATE_IMPORT_PATH")

    monkeypatch.setitem(
        test_windows_exact_element_probe_uses_fresh_actual_native_exit.__globals__,
        "_startup_witness",
        fail,
    )
    with pytest.raises(pytest.fail.Exception, match="Owned native element-probe fixture failed"):
        test_windows_exact_element_probe_uses_fresh_actual_native_exit(tmp_path)
    output = capsys.readouterr()
    assert "PRIVATE" not in output.out + output.err
    (record,) = _fixture_records(output.out)
    assert record["phase"] == "module_load" and record["error"] == "unexpected"


@pytest.mark.parametrize("primary", [True, False])
def test_element_capture_emission_failure_cannot_skip_cleanup_or_pass(
    primary,
    tmp_path,
    monkeypatch,
    capsys,
):
    import io
    from types import SimpleNamespace

    common = _startup_witness().common
    closed = []
    seen = []
    real_emit = _element_diagnostic

    class Owned:
        def __init__(self, *args, **kwargs):
            self.process = SimpleNamespace(stdout=io.BytesIO(b"K5_NATIVE_COMPILE=passed\n"))

        def wait(self, _seconds):
            if primary:
                raise common.WitnessError("child_failed")

        def close(self):
            closed.append(True)

    def emit(context, status, error=None, **kwargs):
        seen.append((status, error.args[0] if error else None, kwargs.get("boundary", "primary")))
        if status == "failed" or kwargs.get("boundary") == "owned_cleanup":
            raise ValueError("PRIVATE_PROJECTION_ERROR")
        return real_emit(context, status, error, **kwargs)

    monkeypatch.setattr(common, "OwnedProcess", Owned)
    monkeypatch.setitem(_capture_element_child.__globals__, "_element_diagnostic", emit)
    with pytest.raises(_ElementCaptureFailure):
        _capture_element_child(
            common, [], cwd=tmp_path, env={}, context=_element_context("reference")
        )
    assert closed == [True]
    assert ("passed", None, "owned_cleanup") in seen
    if primary:
        assert ("failed", "child_failed", "primary") in seen
    output = capsys.readouterr()
    assert "PRIVATE" not in output.out + output.err


def test_element_top_level_emission_failure_never_exposes_setup_error(
    tmp_path, monkeypatch, capsys
):
    def fail_load():
        raise ImportError("PRIVATE_SETUP_PATH")

    def fail_emit(*args, **kwargs):
        raise RuntimeError("PRIVATE_PROJECTION_PATH")

    scope = test_windows_exact_element_probe_uses_fresh_actual_native_exit.__globals__
    monkeypatch.setitem(scope, "_startup_witness", fail_load)
    monkeypatch.setitem(scope, "_element_diagnostic", fail_emit)
    with pytest.raises(pytest.fail.Exception, match="Owned native element-probe fixture failed"):
        test_windows_exact_element_probe_uses_fresh_actual_native_exit(tmp_path)
    output = capsys.readouterr()
    assert "PRIVATE" not in output.out + output.err


def test_element_capture_stuck_collector_never_reports_cleanup_success(
    tmp_path,
    monkeypatch,
    capsys,
):
    import io
    import threading
    from types import SimpleNamespace

    common = _startup_witness().common
    closed = []

    class Reader:
        def __init__(self, **kwargs):
            pass

        def start(self):
            pass

        def join(self, seconds):
            assert seconds in (5, 15)

        def is_alive(self):
            return True

    class Owned:
        def __init__(self, *args, **kwargs):
            self.process = SimpleNamespace(stdout=io.BytesIO())

        def failure(self, code, *, timed_out):
            return common.WitnessError(
                code,
                common.diagnostic(
                    "probe_admission",
                    gate_state="started",
                    timed_out=timed_out,
                ),
            )

        def close(self):
            closed.append(True)

    monkeypatch.setattr(common, "OwnedProcess", Owned)
    monkeypatch.setattr(threading, "Thread", Reader)
    with pytest.raises(_ElementCaptureFailure):
        _capture_element_child(common, [], cwd=tmp_path, env={}, context=_element_context("probe"))
    assert closed == [True]
    records = _fixture_records(capsys.readouterr().out)
    assert not any(r["boundary"] == "owned_cleanup" and r["status"] == "passed" for r in records)
    assert [r["error"] for r in records if r["status"] == "failed"] == [
        "child_timeout",
        "cleanup_incomplete",
    ]


@pytest.mark.parametrize("case", ["zero", "nonzero", "stderr_zero", "stderr_nonzero"])
def test_element_python_single_argument_binds_flags_source_exit_and_stderr(tmp_path, case):
    script = tmp_path / "owned fixture.py"
    script.write_bytes(ELEMENT_PROBE_PYTHON_SOURCE.encode("ascii"))
    argument = _element_python_argument(script, case)
    assert argument.startswith("-IBSc") and not any(c.isspace() or c == '"' for c in argument)
    result = subprocess.run([sys.executable, argument], capture_output=True, timeout=5, check=False)
    assert result.returncode == (0 if case in {"zero", "stderr_zero"} else 7)
    assert result.stdout == b""
    assert result.stderr == (b"PRIVATE_NATIVE_FIXTURE" if case.startswith("stderr_") else b"")
    # Flags and the admitted bytes are acceptance inputs, never defaults.
    result = subprocess.run(
        [sys.executable, "-c" + argument[5:]], capture_output=True, timeout=5, check=False
    )
    assert result.returncode == 31 and not result.stdout and not result.stderr
    script.write_bytes(ELEMENT_PROBE_PYTHON_SOURCE.encode("ascii") + b"# changed")
    with pytest.raises(ValueError):
        _element_python_argument(script, case)
    result = subprocess.run([sys.executable, argument], capture_output=True, timeout=5, check=False)
    assert result.returncode == 36 and not result.stdout and not result.stderr


def test_element_runtime_pair_replaces_compiler_without_widening_probe_or_budget():
    source = Path(__file__).read_text()
    section = source.split("def test_windows_exact_element_probe_uses_fresh_actual_native_exit", 1)[
        1
    ]
    section = section.split("def test_element_probe_parser", 1)[0]
    assert "Add-Type" not in section and "compile.ps1" not in section
    assert '"-Name",' not in section and section.count('"-Name:",') == 3
    assert '("ConsoleApplication", base, 3)' in section
    assert '("WindowsApplication", base.with_name("pythonw.exe"), 2)' in section
    assert "executable.parent != base.parent" in section
    assert section.count("common.file_hash(executable) != identity") == 2
    assert "$selected = $original.Replace($needle, $needle + ' | Out-Null')" in ELEMENT_PROBE_SCRIPT
    assert "Popen([executable, argument]" in ELEMENT_REFERENCE_SCRIPT
    assert "code = child.wait(timeout=5)" in ELEMENT_REFERENCE_SCRIPT
    assert "outputs != [b'', expected]" in ELEMENT_REFERENCE_SCRIPT
    assert "stream.read(65)" in ELEMENT_REFERENCE_SCRIPT
    assert "time.monotonic() + 10" in ELEMENT_REFERENCE_SCRIPT
    assert "reference_wait" in ELEMENT_REFERENCE_SCRIPT
    test_version_guard_probe_is_hosted_only_and_keeps_existing_smoke_selection()


def test_element_timeout_recovers_only_complete_checkpoints_after_cleanup(
    tmp_path,
    monkeypatch,
    capsys,
):
    import threading
    from types import SimpleNamespace

    common = _startup_witness().common
    release, ready = threading.Event(), threading.Event()
    raw = b"PRIVATE_RAW\nK5_ELEMENT_CHECKPOINT=reference_entered\n"
    suffix = b"K5_ELEMENT_CHECKPOINT=reference_sta"
    closed = []
    thread_type = threading.Thread

    class Stream:
        index = 0

        def read(self, limit):  # Also exercises the exact preceding EOF-only implementation.
            ready.set()
            assert release.wait(2)
            return raw + suffix

        def read1(self, limit):
            self.index += 1
            if self.index == 1:
                ready.set()
                return raw
            if self.index == 2:
                assert release.wait(2)
                return suffix
            return b""

        def close(self):
            closed.append("stream")

    class Reader:
        def __init__(self, **kwargs):
            self.thread = thread_type(**kwargs)

        def start(self):
            self.thread.start()

        def join(self, seconds):
            if seconds == 15:
                assert ready.wait(2)
            else:
                self.thread.join(seconds)

        def is_alive(self):
            return self.thread.is_alive()

    class Owned:
        def __init__(self, *args, **kwargs):
            self.process = SimpleNamespace(stdout=Stream())

        def failure(self, code, *, timed_out):
            return common.WitnessError(
                code,
                common.diagnostic(
                    "probe_admission",
                    gate_state="started",
                    timed_out=timed_out,
                ),
            )

        def close(self):
            closed.append("job")
            release.set()

    monkeypatch.setattr(threading, "Thread", Reader)
    monkeypatch.setattr(common, "OwnedProcess", Owned)
    with pytest.raises(_ElementCaptureFailure):
        _capture_element_child(
            common, [], cwd=tmp_path, env={}, context=_element_context("reference")
        )
    output = capsys.readouterr()
    assert closed == ["job", "stream"]
    assert "PRIVATE" not in output.out + output.err
    assert output.out.splitlines()[-1] == "K5_ELEMENT_CHECKPOINT=reference_entered"
    assert "reference_sta" not in output.out
    failures = [value for value in _fixture_records(output.out) if value["status"] == "failed"]
    assert failures[0]["error"] == "child_timeout" and failures[0]["timed_out"]


def test_element_checkpoint_and_argv_records_reject_forged_or_partial_evidence():
    raw = b'K5_ELEMENT_ARGV={"equal":true,"hash_equal":true}\n'
    assert _element_complete_records(raw)[2] == [{"equal": True, "hash_equal": True}]
    assert _element_complete_records(raw[:-1]) == (b"", [], [])
    for bad in (
        raw + raw,
        b"x" * 4097,
        b"K5_ELEMENT_CHECKPOINT=PRIVATE\n",
        b"K5_ELEMENT_CHECKPOINT=reference_entered\n" * 2,
        b'K5_ELEMENT_ARGV={"equal":1,"hash_equal":true}\n',
        b'K5_ELEMENT_ARGV={"equal":true,"equal":true}\n',
        b'K5_ELEMENT_ARGV={"equal":true,"hash_equal":true,"raw":"PRIVATE"}\n',
    ):
        with pytest.raises(ValueError):
            _element_complete_records(bad)


@pytest.mark.parametrize("case", ["zero", "nonzero", "stderr_zero", "stderr_nonzero"])
def test_element_direct_python_reference_proves_real_exit_and_bounded_stdio(tmp_path, case):
    fixture = tmp_path / "owned fixture.py"
    fixture.write_bytes(ELEMENT_PROBE_PYTHON_SOURCE.encode("ascii"))
    oracle = tmp_path / "oracle.py"
    oracle.write_bytes(ELEMENT_REFERENCE_SCRIPT.encode("ascii"))
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-S",
            str(oracle),
            sys.executable,
            _element_python_argument(fixture, case),
            case,
        ],
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0 and not result.stderr and len(result.stdout) <= 4096
    complete, checkpoints, arguments = _element_complete_records(result.stdout)
    assert checkpoints == [
        "reference_entered",
        "reference_start_requested",
        "reference_started",
        "reference_waited",
        "reference_stdio_verified",
        "reference_cleanup_complete",
    ]
    assert arguments == []
    assert b"K5_NATIVE_REFERENCE=" + (b"0" if case in {"zero", "stderr_zero"} else b"7") in complete
    assert "PRIVATE" not in result.stdout.decode()


def test_element_exit_zero_partial_failure_suffix_is_rejected(tmp_path, monkeypatch, capsys):
    import io
    from types import SimpleNamespace

    common = _startup_witness().common
    closed = []

    class Owned:
        def __init__(self, *args, **kwargs):
            self.process = SimpleNamespace(
                stdout=io.BytesIO(
                    b"K5_ELEMENT_CHECKPOINT=reference_entered\nK5_NATIVE_REFERENCE=0\n"
                    b'K5_ELEMENT_CHILD_FAILURE={"PRIVATE_PARTIAL":'
                )
            )

        def wait(self, seconds):
            pass

        def close(self):
            closed.append(True)

    monkeypatch.setattr(common, "OwnedProcess", Owned)
    with pytest.raises(_ElementCaptureFailure):
        _capture_element_child(
            common, [], cwd=tmp_path, env={}, context=_element_context("reference")
        )
    assert closed == [True]
    output = capsys.readouterr()
    assert "PRIVATE" not in output.out + output.err
    assert any(r["error"] == "output_invalid" for r in _fixture_records(output.out))


def test_element_devnull_timeout_preserves_primary_and_successful_owned_cleanup(
    tmp_path,
    monkeypatch,
    capsys,
):
    common = _startup_witness().common

    def run(arguments, **kwargs):
        assert kwargs["seconds"] == 15 and "stdout" not in kwargs
        raise common.WitnessError(
            "child_timeout",
            common.diagnostic(
                "probe_admission",
                timed_out=True,
                gate_state="started",
            ),
        )

    monkeypatch.setattr(common, "run", run)
    with pytest.raises(_ElementCaptureFailure):
        _element_devnull_control(
            common, [], cwd=tmp_path, env={}, context=_element_context("shell_devnull_control")
        )
    records = _fixture_records(capsys.readouterr().out)
    assert records[1]["error"] == "child_timeout" and records[1]["timed_out"]
    assert records[2]["boundary"] == "owned_cleanup" and records[2]["status"] == "passed"


def test_element_devnull_unexpected_failure_leaves_cleanup_unconfirmed(
    tmp_path, monkeypatch, capsys
):
    common = _startup_witness().common

    def fail(*args, **kwargs):
        raise RuntimeError("PRIVATE_UNEXPECTED")

    monkeypatch.setattr(common, "run", fail)
    with pytest.raises(_ElementCaptureFailure):
        _element_devnull_control(
            common, [], cwd=tmp_path, env={}, context=_element_context("shell_devnull_control")
        )
    output = capsys.readouterr()
    assert "PRIVATE" not in output.out + output.err
    records = _fixture_records(output.out)
    assert records[-1]["error"] == "unexpected"
    assert not any(r["boundary"] == "owned_cleanup" for r in records)
