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
        $_.Right.Extent.Text -ceq (
            'Invoke-K5NativeProbe -Executable $mediaMtx -Arguments @("--version") -CaptureOutput')
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
    $probeFunction = @($ast.FindAll({ param($node)
        $node -is [Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -ceq 'Invoke-K5NativeProbe'
    }, $true))
    if ($probeFunction.Count -ne 1) { throw 'fixture_invalid' }
    . ([scriptblock]::Create($probeFunction[0].Extent.Text))
    $mediaMtx = $Python
    $code = @(
        'function Invoke-K5VersionGuardOnly {',
        $assignment.Extent.Text,
        '$script:pinPreserved = ($MediaMtxVersion -is [string] -and',
        '    $MediaMtxVersion -ceq ''1.21.1'')',
        ('$testOutput = $' + $outputName),
        '$script:wrongProbeConfirmed = ($testOutput.ExitCode -eq 0 -and',
        '    $testOutput.Stdout.TrimEnd([char[]]@(13,10)) -cmatch',
        '    ''^Python [0-9]+\.[0-9]+\.[0-9]+[a-z0-9.+-]*$'')',
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


def test_native_probe_dependency_is_exact_and_keeps_existing_smoke_selection() -> None:
    from fnmatch import fnmatchcase

    native = (ROOT / ".github/workflows/installed-analytics-candidate.yml").read_text()
    push = native.split("  push:\n", 1)[1].split("\n\n", 1)[0]
    patterns = [
        line.strip()[2:].strip("\"'")
        for line in push.split("    paths:\n", 1)[1].splitlines()
        if line.strip().startswith("- ")
    ]
    dependency = "tests/test_windows_alpha_analytics.py"
    assert [pattern for pattern in patterns if fnmatchcase(dependency, pattern)] == [dependency]
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
    assert (
        'Invoke-K5NativeProbe -Executable $mediaMtx -Arguments @("--version") -CaptureOutput'
        in VERSION_GUARD_SCRIPT
    )
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


def test_media_mtx_version_guard_has_distinct_output_and_exact_official_token():
    source = START.read_text()
    guard = source.split("$mediaMtxVersionOutput =", 1)[1].split("$validation =", 1)[0]
    assert 'Invoke-K5NativeProbe -Executable $mediaMtx -Arguments @("--version")' in guard
    assert "$mediaMtxVersionOutput.ExitCode -ne 0" in guard
    assert "$mediaMtxVersionOutput.Stdout -isnot [string]" in guard
    assert "$mediaMtxVersionOutput.Stdout -cnotin" in guard
    assert '("v" + $MediaMtxVersion)' in guard
    assert ".Contains(" not in guard and "-join" not in guard
    assert "$LASTEXITCODE" not in guard


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
        [
            dict(text="\n".join(map(str, lines)), exit=exit_code, accept=accept)
            for lines, exit_code, accept in cases
        ]
    )
    script = (
        selection
        + r"""
    $code = @(
        'function Invoke-K5ControlledVersionGuard { param($case)',
        ('$' + $outputName + ' = [pscustomobject]@{ExitCode=$case.exit; Stdout=$case.text}'),
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
function Invoke-K5NativeProbe {
    param($Executable, [string[]]$Arguments, [switch]$CaptureOutput, [switch]$DiscardStderr)
    if ($Executable -cne 'Test-K5FixtureMediaMtx' -or $DiscardStderr) {
        throw 'fixture_invalid'
    }
    $text = if ($Arguments[0] -ceq '--version') { 'v1.21.1' } else { '' }
    return [pscustomobject]@{ ExitCode = 0; Stdout = $text }
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
    # Frozen old-helper error-chain evidence, independent of current product behavior.
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
                # historical helper returns; otherwise exit 1 becomes element refusal.
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
    test_native_probe_dependency_is_exact_and_keeps_existing_smoke_selection()


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


def test_all_streams_fixture_keeps_frozen_historical_error_boundary():
    source = ELEMENT_HISTORICAL_HELPER
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
ELEMENT_PROBE_SCHEMA = "element-native-exit-probe-v2"
ELEMENT_PROBE_OUTCOMES = {
    "true",
    "false",
    "variable_undefined",
    "native_stderr",
    "command_missing",
    "unexpected",
    "timeout",
    "output_limit",
    "cleanup_failed",
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
    if _fixture_case not in ('zero', 'nonzero', 'stderr_zero', 'stderr_nonzero',
                             'stdout_bound', 'stdout_overflow', 'timeout'):
        os._exit(33)
    if _fixture_case == 'timeout':
        time.sleep(30)
        os._exit(37)
    time.sleep(0.5)
    if _fixture_case in ('stdout_bound', 'stdout_overflow'):
        size = 131072 if _fixture_case == 'stdout_bound' else 131073
        data = b'x' * size
        while data:
            written = os.write(1, data)
            if written <= 0:
                os._exit(38)
            data = data[written:]
        os._exit(0)
    if _fixture_case.startswith('stderr_'):
        if os.write(2, b'PRIVATE_NATIVE_FIXTURE') != 22:
            os._exit(34)
    os._exit(0 if _fixture_case in ('zero', 'stderr_zero') else 7)
except BaseException:
    os._exit(35)
"""


def _element_python_argument(script: Path, case: str) -> str:
    import base64

    if case not in {
        "zero",
        "nonzero",
        "stderr_zero",
        "stderr_nonzero",
        "stdout_bound",
        "stdout_overflow",
        "timeout",
    }:
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
    "utility_observation",
    "utility_manifest",
    "utility_import",
    "utility_binding",
    "utility_cmdlet",
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
    if ($Phase -cnotin @('utility_manifest','utility_import','utility_binding','utility_cmdlet',
        'utility_observation',
        'source_select','command_admission','probe_invoke','probe_record',
        'reference_start','reference_wait','reference_exit','reference_cleanup') -or
        $Boundary -cnotin @('primary','cleanup')) { throw 'fixture_diagnostic_invalid' }
    $codeText = 'null'
    if ($null -ne $code) {
        $codeText = $code.ToString([Globalization.CultureInfo]::InvariantCulture)
    }
    $json = ('{{"schema_version":"element-child-failure-v1","phase":"{0}",' +
        '"boundary":"{1}","error":"{2}","hresult":{3}}}') -f
        $Phase, $Boundary, $category, $codeText
    [Console]::Out.WriteLine('K5_ELEMENT_CHILD_FAILURE=' + $json)
    [Console]::Out.Flush()
}
"""
ELEMENT_UTILITY_OBSERVATION = r"""
function Write-K5UtilityBindingObservation {
    $observed = [ordered]@{
        schema_version = 'element-utility-binding-v3'
        pshome_verified = $true; manifest_hash_verified = $true; manifest_reparse_clear = $true
        module_count_ok = $false; module_type_ok = $false; module_name_ok = $false
        module_base_ok = $false; module_path_ok = $false; cmdlet_type_ok = $false
        implementation_type_ok = $false; assembly_name_ok = $false
        utility_token_length_ok = $false; core_token_length_ok = $false; token_match = $false
        metadata_complete = $true; module_base_kind = 'unavailable'
        module_path_kind = 'unavailable'
    }
    $observedModule = $null; $observedCmdlet = $null; $observedType = $null
    $observedAssembly = $null; $observedToken = $null; $observedCoreToken = $null
    $observed.module_count_ok = $utilityModules.Count -eq 1
    if ($observed.module_count_ok) {
        $observed.module_type_ok = $utilityModules[0] -is [Management.Automation.PSModuleInfo]
        if ($observed.module_type_ok) { $observedModule = $utilityModules[0] }
    }
    if ($null -ne $observedModule) {
        try { $observed.module_name_ok = $observedModule.Name -ceq 'Microsoft.PowerShell.Utility' }
        catch { $observed.metadata_complete = $false }
        try {
            $observedBase = [IO.Path]::GetFullPath($observedModule.ModuleBase)
            $observed.module_base_ok = [string]::Equals($observedBase, $expectedHome,
                [StringComparison]::OrdinalIgnoreCase)
            $observed.module_base_kind = 'other'
            if ($observed.module_base_ok) {
                $observed.module_base_kind = 'exact_pshome'
            }
        } catch { $observed.metadata_complete = $false }
        try {
            $observedPath = [IO.Path]::GetFullPath($observedModule.Path)
            $observed.module_path_ok = [string]::Equals($observedPath, $utilityManifest,
                [StringComparison]::OrdinalIgnoreCase)
            $observed.module_path_kind = 'other'
            if ($observed.module_path_ok) { $observed.module_path_kind = 'admitted_manifest' }
        } catch { $observed.metadata_complete = $false }
        try {
            $observedCmdlet = $observedModule.ExportedCmdlets['Write-Output']
            $observed.cmdlet_type_ok = $observedCmdlet -is [Management.Automation.CmdletInfo]
        } catch { $observed.metadata_complete = $false }
    } else { $observed.metadata_complete = $false }
    if ($observed.cmdlet_type_ok) {
        try {
            $observedType = $observedCmdlet.ImplementingType
            if ($observedType -isnot [type]) { throw 'observation_unavailable' }
            $observed.implementation_type_ok = $observedType.FullName -ceq
                'Microsoft.PowerShell.Commands.WriteOutputCommand'
        } catch { $observed.metadata_complete = $false }
    } else { $observed.metadata_complete = $false }
    if ($observedType -is [type]) {
        try {
            $observedAssembly = $observedType.Assembly.GetName()
            $observed.assembly_name_ok = $observedAssembly.Name -ceq
                'Microsoft.PowerShell.Commands.Utility'
        } catch { $observed.metadata_complete = $false }
    }
    if ($null -ne $observedAssembly) {
        try {
            $observedToken = $observedAssembly.GetPublicKeyToken()
            $observed.utility_token_length_ok = $observedToken.Length -eq 8
        } catch { $observed.metadata_complete = $false }
    }
    try {
        $observedCoreToken = [Management.Automation.PSObject].Assembly.GetName().GetPublicKeyToken()
        $observed.core_token_length_ok = $observedCoreToken.Length -eq 8
    } catch { $observed.metadata_complete = $false }
    if ($null -ne $observedToken -and $null -ne $observedCoreToken) {
        try {
            $observed.token_match = [BitConverter]::ToString($observedToken) -ceq
                [BitConverter]::ToString($observedCoreToken)
        } catch { $observed.metadata_complete = $false }
    }
    $observedJson = ('{{"schema_version":"element-utility-binding-v3","pshome_verified":{0}' +
        ',"manifest_hash_verified":{1},"manifest_reparse_clear":{2},"module_count_ok":{3}' +
        ',"module_type_ok":{4},"module_name_ok":{5},"module_base_ok":{6},"module_path_ok":{7}' +
        ',"cmdlet_type_ok":{8},"implementation_type_ok":{9},"assembly_name_ok":{10}' +
        ',"utility_token_length_ok":{11},"core_token_length_ok":{12},"token_match":{13}' +
        ',"metadata_complete":{14},"module_base_kind":"{15}","module_path_kind":"{16}"}}') -f
        $observed.pshome_verified.ToString().ToLowerInvariant(),
        $observed.manifest_hash_verified.ToString().ToLowerInvariant(),
        $observed.manifest_reparse_clear.ToString().ToLowerInvariant(),
        $observed.module_count_ok.ToString().ToLowerInvariant(),
        $observed.module_type_ok.ToString().ToLowerInvariant(),
        $observed.module_name_ok.ToString().ToLowerInvariant(),
        $observed.module_base_ok.ToString().ToLowerInvariant(),
        $observed.module_path_ok.ToString().ToLowerInvariant(),
        $observed.cmdlet_type_ok.ToString().ToLowerInvariant(),
        $observed.implementation_type_ok.ToString().ToLowerInvariant(),
        $observed.assembly_name_ok.ToString().ToLowerInvariant(),
        $observed.utility_token_length_ok.ToString().ToLowerInvariant(),
        $observed.core_token_length_ok.ToString().ToLowerInvariant(),
        $observed.token_match.ToString().ToLowerInvariant(),
        $observed.metadata_complete.ToString().ToLowerInvariant(),
        $observed.module_base_kind,
        $observed.module_path_kind
    [Console]::Out.WriteLine('K5_ELEMENT_UTILITY_BINDING=' + $observedJson)
    [Console]::Out.Flush()
    if (-not $observed.metadata_complete) {
        throw 'fixture_identity'
    }
}
"""
ELEMENT_UTILITY_IMPORT = r"""
__UTILITY_OBSERVATION__
$fixturePhase = 'utility_manifest'
[Console]::Out.WriteLine('K5_ELEMENT_CHECKPOINT=utility_manifest_requested')
[Console]::Out.Flush()
$expectedHome = '__UTILITY_HOME__'
if (-not [string]::Equals([IO.Path]::GetFullPath($PSHOME), $expectedHome,
        [StringComparison]::OrdinalIgnoreCase)) { throw 'fixture_identity' }
$utilityBase = [IO.Path]::Combine($PSHOME, 'Modules\Microsoft.PowerShell.Utility')
$utilityManifest = [IO.Path]::Combine($utilityBase, 'Microsoft.PowerShell.Utility.psd1')
foreach ($checkedPath in @($PSHOME, [IO.Path]::Combine($PSHOME, 'Modules'),
        $utilityBase, $utilityManifest)) {
    if (([IO.File]::GetAttributes($checkedPath) -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw 'fixture_identity'
    }
}
$manifestStream = [IO.File]::OpenRead($utilityManifest)
$manifestHasher = $null
try {
    if ($manifestStream.Length -le 0 -or $manifestStream.Length -gt 65536) {
        throw 'fixture_identity'
    }
    $manifestBytes = [Array]::CreateInstance([byte], [int]$manifestStream.Length)
    $manifestOffset = 0
    while ($manifestOffset -lt $manifestBytes.Length) {
        $manifestRead = $manifestStream.Read($manifestBytes, $manifestOffset,
            $manifestBytes.Length - $manifestOffset)
        if ($manifestRead -le 0) { throw 'fixture_identity' }
        $manifestOffset += $manifestRead
    }
    if ($manifestStream.ReadByte() -ne -1) { throw 'fixture_identity' }
    $manifestHasher = [Security.Cryptography.SHA256]::Create()
    $manifestDigest = $manifestHasher.ComputeHash($manifestBytes)
    $manifestHash = [BitConverter]::ToString($manifestDigest).Replace('-', '').ToLowerInvariant()
    if ($manifestHash -cne '__UTILITY_SHA256__') { throw 'fixture_identity' }
} finally {
    if ($null -ne $manifestHasher) { $manifestHasher.Dispose() }
    $manifestStream.Dispose()
}
$fixturePhase = 'utility_import'
[Console]::Out.WriteLine('K5_ELEMENT_CHECKPOINT=utility_import_requested')
[Console]::Out.Flush()
$utilityModules = @(
    Microsoft.PowerShell.Core\Import-Module -Name $utilityManifest -PassThru -ErrorAction Stop
)
[Console]::Out.WriteLine('K5_ELEMENT_CHECKPOINT=utility_import_returned')
[Console]::Out.Flush()
$fixturePhase = 'utility_observation'
Write-K5UtilityBindingObservation
$fixturePhase = 'utility_binding'
if ($utilityModules.Count -ne 1 -or
    $utilityModules[0] -isnot [Management.Automation.PSModuleInfo]) {
    throw 'fixture_identity'
}
$utilityModule = $utilityModules[0]
if ($utilityModule.Name -cne 'Microsoft.PowerShell.Utility' -or
    -not [string]::Equals([IO.Path]::GetFullPath($utilityModule.ModuleBase), $expectedHome,
        [StringComparison]::OrdinalIgnoreCase) -or
    -not [string]::Equals([IO.Path]::GetFullPath($utilityModule.Path), $utilityManifest,
        [StringComparison]::OrdinalIgnoreCase)) { throw 'fixture_identity' }
$utilityWriteOutput = $utilityModule.ExportedCmdlets['Write-Output']
if ($utilityWriteOutput -isnot [Management.Automation.CmdletInfo] -or
    $utilityWriteOutput.ImplementingType.FullName -cne
        'Microsoft.PowerShell.Commands.WriteOutputCommand') {
    throw 'fixture_identity'
}
$utilityAssembly = $utilityWriteOutput.ImplementingType.Assembly.GetName()
$utilityToken = $utilityAssembly.GetPublicKeyToken()
$coreToken = [Management.Automation.PSObject].Assembly.GetName().GetPublicKeyToken()
if ($utilityAssembly.Name -cne 'Microsoft.PowerShell.Commands.Utility' -or
    $utilityToken.Length -ne 8 -or $coreToken.Length -ne 8 -or
    [BitConverter]::ToString($utilityToken) -cne [BitConverter]::ToString($coreToken)) {
    throw 'fixture_identity'
}
[Console]::Out.WriteLine('K5_ELEMENT_CHECKPOINT=utility_binding_verified')
[Console]::Out.Flush()
""".replace("__UTILITY_OBSERVATION__", ELEMENT_UTILITY_OBSERVATION)
# Exact current source identities: the shared pump and its element wrapper.
# Native packaging keeps raw source hashes; only these two test representations
# are accepted before any line-ending conversion.
ELEMENT_PRODUCT_HASHES = {
    "Test-K5GStreamerElement": (
        "7411df1749e08a316df8cf82fd8b99391b6200ffbf124bd1cb9f40c6f2ea8492",
        "ea128d8494db74a3dbdc6da93f3e751047b0e8e5d1fcbe7d2472b45e20fd0414",
    ),
    "Invoke-K5NativeProbe": (
        "95d57f2441a79c3bee6fdb6cdc0e64d6869aedde301294ef9440d890a2ef477f",
        "4d3a655eb0266e2ea72e4806df3767b1bc979e14de4cff8f95f4d7d0176d20c6",
    ),
}

ELEMENT_HISTORICAL_HELPER = r"""function Test-K5GStreamerElement([string]$Name) {
    & $gstInspect $Name *> $null
    return $LASTEXITCODE -eq 0
}"""


def _element_product_helper(source):
    if type(source) is not bytes or len(source) > 262144:
        raise ValueError("Invalid product helper source")
    bodies = []
    for name, digests in ELEMENT_PRODUCT_HASHES.items():
        matches = re.findall(
            rb"(?ms)^function " + name.encode() + rb"(?:\([^\r\n]*\))? \{\r?\n.*?^\}", source
        )
        if len(matches) != 1 or hashlib.sha256(matches[0]).hexdigest() not in digests:
            raise ValueError("Unqualified product helper source")
        bodies.append(matches[0].replace(b"\r\n", b"\n").decode("ascii"))
    return "\n" + "\n\n".join(bodies) + "\n"


ELEMENT_PRODUCT_HELPER = _element_product_helper(START.read_bytes())
ELEMENT_PROCESS_INSERTIONS = {
    "        $exitCode = $child.ExitCode\n": "        $script:fixtureActualExit = $exitCode\n",
    "        # The exact started Process and its streams are now closed.\n": (
        "        $script:fixtureProcessCleaned = $true\n"
    ),
}
ELEMENT_PRODUCT_OBSERVED = ELEMENT_PRODUCT_HELPER
for _anchor, _insertion in ELEMENT_PROCESS_INSERTIONS.items():
    if ELEMENT_PRODUCT_OBSERVED.count(_anchor) != 1:
        raise ValueError("Invalid process fixture anchor")
    ELEMENT_PRODUCT_OBSERVED = ELEMENT_PRODUCT_OBSERVED.replace(_anchor, _anchor + _insertion, 1)

ELEMENT_PROBE_SCRIPT = (
    r"""
param([string]$Start, [string]$Executable, [string]$Name, [string]$Variant, [string]$Initial)
$ErrorActionPreference = 'Stop'
__ELEMENT_CHILD_DIAGNOSTICS__
$fixturePhase = 'utility_manifest'
try {
__ELEMENT_UTILITY_IMPORT__
    Set-StrictMode -Version Latest
    $fixturePhase = 'source_select'
    if ($Variant -cnotin @('original','process') -or
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
        $node.Name -cin @('Test-K5GStreamerElement','Invoke-K5NativeProbe')
    }, $true))
    if ($errors.Count -ne 0 -or $functions.Count -ne 2) { throw 'fixture_invalid' }
    $helperNames = @('Test-K5GStreamerElement','Invoke-K5NativeProbe')
    $helperHashes = @{ __QUALIFIED_ELEMENTS__ }
    $actualParts = [Collections.Generic.List[string]]::new()
    $helperHasher = [Security.Cryptography.SHA256]::Create()
    try {
        for ($index = 0; $index -lt 2; $index++) {
            if ($functions[$index].Name -cne $helperNames[$index]) { throw 'fixture_identity' }
            $actual = $functions[$index].Extent.Text
            $helperBytes = [Text.UTF8Encoding]::new($false, $true).GetBytes($actual)
            $helperHash = [BitConverter]::ToString(
                $helperHasher.ComputeHash($helperBytes)).Replace('-', '').ToLowerInvariant()
            if ($helperHash -cnotin $helperHashes[$helperNames[$index]]) {
                throw 'fixture_identity'
            }
            $actualParts.Add($actual.Replace("`r`n", "`n"))
        }
    } finally { $helperHasher.Dispose() }
    if ($Variant -ceq 'process') {
        $selected = [string]::Join("`n`n", $actualParts.ToArray()) + "`n"
__ELEMENT_PROCESS_SCALAR_INSERTIONS__
    } else {
        # Frozen historical error/exit baseline; never the current product source.
        $selected = @'
__ELEMENT_HISTORICAL_HELPER__
'@
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
    $script:fixtureActualExit = $null
    $script:fixtureProcessCleaned = $false
    $outcome = 'unexpected'
    $watch = [Diagnostics.Stopwatch]::StartNew()
    try {
        $answer = Test-K5GStreamerElement -Name $Name
        if ($answer -is [bool]) { $outcome = if ($answer) { 'true' } else { 'false' } }
    } catch {
        $id = $_.FullyQualifiedErrorId.Split(',')[0]
        if ($_.Exception -is [TimeoutException]) { $outcome = 'timeout' }
        elseif ($_.Exception -is [IO.InvalidDataException] -and
                $_.Exception.Message -ceq 'K5 native stderr refused.') {
            $outcome = 'native_stderr'
        }
        elseif ($_.Exception -is [IO.InvalidDataException] -and
                $_.Exception.Message -ceq 'K5 native output limit.') { $outcome = 'output_limit' }
        elseif ($_.Exception -is [InvalidOperationException] -and
                $_.Exception.Message -ceq 'K5 native cleanup failed.') {
            $outcome = 'cleanup_failed'
        }
        elseif ($_.Exception -is [Management.Automation.CommandNotFoundException]) {
            $outcome = 'command_missing'
        }
        elseif ($id -ceq 'VariableIsUndefined') { $outcome = 'variable_undefined' }
        elseif ($id -cin @('NativeCommandError','NativeCommandErrorMessage')) {
            $outcome = 'native_stderr'
        } elseif ($id -ceq 'CommandNotFoundException') { $outcome = 'command_missing' }
        if ($outcome -eq 'cleanup_failed') {
            $linked = $_.Exception.Data['K5ElementPrimaryErrorRecord']
            if ($linked -is [Management.Automation.ErrorRecord]) {
                Write-K5ElementChildFailure $linked $fixturePhase 'primary'
            }
            Write-K5ElementChildFailure $_ $fixturePhase 'cleanup'
            exit 1
        }
        if ($outcome -eq 'unexpected') {
            Write-K5ElementChildFailure $_ $fixturePhase 'primary'
            exit 1
        }
    }
    $watch.Stop()
    $fixturePhase = 'probe_record'
    $last = Get-Variable LASTEXITCODE -Scope Global -ErrorAction SilentlyContinue
    $value = $null
    if ($null -ne $last) {
        if ($last.Value -isnot [int] -or $last.Value -notin @(0,7,9)) { throw 'fixture_invalid' }
        $value = $last.Value
    }
    $valueText = 'null'
    if ($null -ne $value) {
        $valueText = $value.ToString([Globalization.CultureInfo]::InvariantCulture)
    }
    $actualText = 'null'
    if ($null -ne $script:fixtureActualExit) {
        if ($script:fixtureActualExit -isnot [int] -or
            $script:fixtureActualExit -notin @(0,7)) { throw 'fixture_invalid' }
        $actualText = $script:fixtureActualExit.ToString(
            [Globalization.CultureInfo]::InvariantCulture)
    }
    $cleanedText = $script:fixtureProcessCleaned.ToString().ToLowerInvariant()
    $boundText = $bound.ToString().ToLowerInvariant()
    $waitedText = ($watch.ElapsedMilliseconds -ge 400).ToString().ToLowerInvariant()
    $json = ('{{"schema_version":"element-native-exit-probe-v2","variant":"{0}",' +
        '"initial":"{1}","fresh_session":true,"command_kind":"{2}",' +
        '"application_bound":{3},"outcome":"{4}","last_exit":{5},"waited_floor":{6},' +
        '"actual_exit":{7},"process_cleaned":{8}}}') -f
        $Variant, $Initial, $kind, $boundText, $outcome, $valueText, $waitedText,
        $actualText, $cleanedText
    [Console]::Out.WriteLine('K5_ELEMENT_PROBE=' + $json)
    [Console]::Out.Flush()
    exit 0
} catch { Write-K5ElementChildFailure $_ $fixturePhase 'primary'; exit 1 }
""".replace("__ELEMENT_CHILD_DIAGNOSTICS__", ELEMENT_CHILD_DIAGNOSTICS)
    .replace("__ELEMENT_UTILITY_IMPORT__", ELEMENT_UTILITY_IMPORT)
    .replace(
        "__QUALIFIED_ELEMENTS__",
        "; ".join(
            "'" + name + "' = @('" + "','".join(digests) + "')"
            for name, digests in ELEMENT_PRODUCT_HASHES.items()
        ),
    )
    .replace("__ELEMENT_HISTORICAL_HELPER__", ELEMENT_HISTORICAL_HELPER)
    .replace(
        "__ELEMENT_PROCESS_SCALAR_INSERTIONS__",
        "\n".join(
            "        $anchor = '" + anchor.rstrip("\n").replace("'", "''") + '\' + "`n"\n'
            "        if (($selected.Split(@($anchor), [StringSplitOptions]::None)).Count -ne 2) {\n"
            "            throw 'fixture_identity'\n        }\n"
            "        $selected = $selected.Replace($anchor, $anchor + '"
            + insertion.rstrip("\n").replace("'", "''")
            + '\' + "`n")'
            for anchor, insertion in ELEMENT_PROCESS_INSERTIONS.items()
        ),
    )
)
ELEMENT_UTILITY_CONTROL = (
    "$ErrorActionPreference = 'Stop'\n"
    + ELEMENT_CHILD_DIAGNOSTICS
    + "\n$fixturePhase = 'utility_manifest'\ntry {\n"
    + ELEMENT_UTILITY_IMPORT
    + r"""
$fixturePhase = 'utility_cmdlet'
[Console]::Out.WriteLine('K5_ELEMENT_CHECKPOINT=utility_cmdlet_requested')
[Console]::Out.Flush()
& $utilityWriteOutput 'K5_ELEMENT_CHECKPOINT=utility_cmdlet_result'
[Console]::Out.WriteLine('K5_ELEMENT_CHECKPOINT=utility_cmdlet_returned')
[Console]::Out.Flush()
exit 0
} catch { Write-K5ElementChildFailure $_ $fixturePhase 'primary'; exit 1 }
"""
)
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
    "utility_manifest_requested",
    "utility_import_requested",
    "utility_import_returned",
    "utility_binding_verified",
    "utility_cmdlet_requested",
    "utility_cmdlet_result",
    "utility_cmdlet_returned",
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


def _element_direct_control_script(argument):
    original = _element_control_script(argument)
    replacements = (
        (
            "Write-Output 'K5_ELEMENT_CHECKPOINT=shell_control_entered'",
            "[Console]::Out.WriteLine('K5_ELEMENT_CHECKPOINT=shell_control_entered'); "
            "[Console]::Out.Flush()",
        ),
        (
            "Write-Output 'K5_ELEMENT_CHECKPOINT=shell_control_exit'",
            "[Console]::Out.WriteLine('K5_ELEMENT_CHECKPOINT=shell_control_exit'); "
            "[Console]::Out.Flush()",
        ),
        (
            "Write-Output ('K5_ELEMENT_ARGV=' + ($record | ConvertTo-Json -Compress))",
            '[Console]::Out.WriteLine((\'K5_ELEMENT_ARGV={{"equal":{0},"hash_equal":{1}}}\' -f '
            "$equal.ToString().ToLowerInvariant(), $hashEqual.ToString().ToLowerInvariant())); "
            "[Console]::Out.Flush()",
        ),
    )
    for before, after in replacements:
        if original.count(before) != 1:
            raise ValueError("Invalid fixed output anchor")
        original = original.replace(before, after, 1)
    return original


def _element_bind_utility(script, powershell, common):
    home = common.local_path(powershell).parent
    manifest = common.local_path(
        home / "Modules/Microsoft.PowerShell.Utility/Microsoft.PowerShell.Utility.psd1"
    )
    with manifest.open("rb") as stream:
        raw = stream.read(65537)
    if not raw or len(raw) > 65536:
        raise ValueError("Invalid scoped manifest")
    for marker, value in (
        ("__UTILITY_HOME__", str(home).replace("'", "''")),
        ("__UTILITY_SHA256__", hashlib.sha256(raw).hexdigest()),
    ):
        if script.count(marker) != 1:
            raise ValueError("Invalid scoped module template")
        script = script.replace(marker, value, 1)
    return script


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
        "actual_exit",
        "process_cleaned",
    }:
        raise ValueError("Invalid element probe")
    for field, allowed in (
        ("schema_version", {ELEMENT_PROBE_SCHEMA}),
        ("variant", {"original", "process"}),
        ("initial", {"absent", "stale_zero", "stale_nonzero"}),
        ("command_kind", {"application", "missing", "alias", "function"}),
        ("outcome", ELEMENT_PROBE_OUTCOMES),
    ):
        if type(value[field]) is not str or value[field] not in allowed:
            raise ValueError("Invalid element probe")
    for field in ("fresh_session", "application_bound", "waited_floor", "process_cleaned"):
        if type(value[field]) is not bool:
            raise ValueError("Invalid element probe")
    if value["last_exit"] is not None and (
        type(value["last_exit"]) is not int or value["last_exit"] not in (0, 7, 9)
    ):
        raise ValueError("Invalid element probe")
    if value["actual_exit"] is not None and (
        type(value["actual_exit"]) is not int or value["actual_exit"] not in (0, 7)
    ):
        raise ValueError("Invalid actual exit")
    if value["variant"] == "original" and (
        value["actual_exit"] is not None or value["process_cleaned"]
    ):
        raise ValueError("Original has no Process observation")
    if value["application_bound"] and value["command_kind"] != "application":
        raise ValueError("Invalid element probe")
    return value


ELEMENT_UTILITY_BINDING_PREFIX = b"K5_ELEMENT_UTILITY_BINDING="
ELEMENT_UTILITY_PREDICATES = {
    "pshome_verified",
    "manifest_hash_verified",
    "manifest_reparse_clear",
    "module_count_ok",
    "module_type_ok",
    "module_name_ok",
    "module_base_ok",
    "module_path_ok",
    "cmdlet_type_ok",
    "implementation_type_ok",
    "assembly_name_ok",
    "utility_token_length_ok",
    "core_token_length_ok",
    "token_match",
    "metadata_complete",
}
ELEMENT_UTILITY_CATEGORIES = {
    "schema_version": {"element-utility-binding-v3"},
    "module_base_kind": {"exact_pshome", "other", "unavailable"},
    "module_path_kind": {"admitted_manifest", "other", "unavailable"},
}


def _element_utility_binding_records(raw):
    if len(raw) > 4096:
        raise ValueError("Invalid utility observation")
    records = []
    for line in raw.splitlines():
        if not line.startswith(ELEMENT_UTILITY_BINDING_PREFIX):
            continue
        pairs = json.loads(line[len(ELEMENT_UTILITY_BINDING_PREFIX) :], object_pairs_hook=list)
        if type(pairs) is not list or any(
            type(pair) is not tuple or len(pair) != 2 for pair in pairs
        ):
            raise ValueError("Invalid utility observation")
        value = dict(pairs)
        bools = ELEMENT_UTILITY_PREDICATES
        if len(value) != len(pairs) or value.keys() != bools | ELEMENT_UTILITY_CATEGORIES.keys():
            raise ValueError("Invalid utility observation")
        if any(type(value[key]) is not bool for key in bools):
            raise ValueError("Invalid utility observation")
        for key, allowed in ELEMENT_UTILITY_CATEGORIES.items():
            if type(value[key]) is not str or value[key] not in allowed:
                raise ValueError("Invalid utility observation")
        if value["module_base_ok"] != (value["module_base_kind"] == "exact_pshome") or value[
            "module_path_ok"
        ] != (value["module_path_kind"] == "admitted_manifest"):
            raise ValueError("Inconsistent utility observation")
        records.append(value)
    if len(records) > 1:
        raise ValueError("Duplicate utility observation")
    return records


def _element_require_utility_observation(line):
    records = _element_utility_binding_records(line)
    if len(records) != 1 or not all(records[0][key] for key in ELEMENT_UTILITY_PREDICATES):
        raise ValueError("Incomplete utility observation")


def _parse_imported_element_probe(raw):
    lines = raw.splitlines()
    if (
        len(raw) > 4096
        or not raw.endswith(b"\n")
        or len(lines) != 6
        or lines[:3]
        != [
            ELEMENT_CHECKPOINT_PREFIX + name.encode("ascii")
            for name in (
                "utility_manifest_requested",
                "utility_import_requested",
                "utility_import_returned",
            )
        ]
        or lines[4] != ELEMENT_CHECKPOINT_PREFIX + b"utility_binding_verified"
    ):
        raise ValueError("Invalid official import observation")
    _element_require_utility_observation(lines[3])
    return _parse_element_probe(lines[5] + b"\n")


ELEMENT_FIXTURE_PREFIX = "K5_ELEMENT_FIXTURE_DIAGNOSTIC="
ELEMENT_FIXTURE_PHASES = {
    "shell_direct_control",
    "shell_utility_control",
    "utility_admission",
    "python_control",
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
        "case": {
            "none",
            "zero",
            "nonzero",
            "stderr_zero",
            "stderr_nonzero",
            "stdout_bound",
            "stdout_overflow",
            "timeout",
        },
        "variant": {"none", "original", "process"},
        "initial": {"none", "absent", "stale_zero", "stale_nonzero", "stale_seven"},
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
            for binding in _element_utility_binding_records(complete):
                print(
                    ELEMENT_UTILITY_BINDING_PREFIX.decode()
                    + json.dumps(binding, separators=(",", ":"))
                )
            for binding in _run_facade_management_binding_records(complete):
                print(
                    RUN_FACADE_MANAGEMENT_PREFIX.decode()
                    + json.dumps(binding, separators=(",", ":"))
                )
            for inner in _test_native_inner_records(complete):
                print(TEST_NATIVE_INNER_PREFIX.decode() + json.dumps(inner, separators=(",", ":")))
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


def _element_utility_controls(common, direct_command, utility_command, *, cwd, env):
    # c6079f32 proved the first Write-Output boundary stalls; compare an exact
    # official import with direct fixture output, retaining every native gate.
    context = _element_context("shell_direct_control")
    try:
        output = _capture_element_child(common, direct_command, cwd=cwd, env=env, context=context)
        if output.splitlines() != [
            b"K5_ELEMENT_CHECKPOINT=shell_control_entered",
            b'K5_ELEMENT_ARGV={"equal":true,"hash_equal":true}',
            b"K5_ELEMENT_CHECKPOINT=shell_control_exit",
        ]:
            raise ValueError("Invalid fixed direct control")
        _element_diagnostic(context, "passed")
        context = _element_context("shell_utility_control")
        output = _capture_element_child(common, utility_command, cwd=cwd, env=env, context=context)
        lines = output.splitlines()
        if (
            len(lines) != 8
            or lines[:3]
            != [
                ELEMENT_CHECKPOINT_PREFIX + name.encode()
                for name in (
                    "utility_manifest_requested",
                    "utility_import_requested",
                    "utility_import_returned",
                )
            ]
            or lines[4:]
            != [
                ELEMENT_CHECKPOINT_PREFIX + name.encode()
                for name in (
                    "utility_binding_verified",
                    "utility_cmdlet_requested",
                    "utility_cmdlet_result",
                    "utility_cmdlet_returned",
                )
            ]
        ):
            raise ValueError("Invalid official module control")
        _element_require_utility_observation(lines[3])
        _element_diagnostic(context, "passed")
    except _ElementCaptureFailure:
        raise
    except Exception as error:
        try:
            _element_diagnostic(context, "failed", error, common=common)
        except Exception:
            pass
        raise _ElementCaptureFailure from None


@pytest.mark.skipif(sys.platform != "win32", reason="Requires Windows GUI/CUI process semantics")
def test_windows_exact_element_probe_uses_fresh_actual_native_exit(tmp_path: Path):
    import os

    common = None
    context = _element_context("module_load")
    complete = False
    original_misses = process_misses = missing_refusals = 0
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
        context = _element_context("utility_admission")
        probe.write_text(
            _element_bind_utility(ELEMENT_PROBE_SCRIPT, powershell, common),
            encoding="ascii",
            newline="\n",
        )
        control = tmp_path / "control.ps1"
        control_argument = _element_python_argument(fixture, "zero")
        control.write_text(
            _element_direct_control_script(control_argument), encoding="ascii", newline="\n"
        )
        utility_control = tmp_path / "utility-control.ps1"
        utility_control.write_text(
            _element_bind_utility(ELEMENT_UTILITY_CONTROL, powershell, common),
            encoding="ascii",
            newline="\n",
        )
        shell_arguments = [str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-File"]
        _element_utility_controls(
            common,
            shell_arguments + [str(control), "-Name:", control_argument],
            shell_arguments + [str(utility_control)],
            cwd=tmp_path,
            env=env,
        )
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
                reference_output, checkpoints, observations = _element_complete_records(raw)
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
                    for line in reference_output.splitlines()
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
                    for variant in ("original", "process"):
                        context = _element_context("probe", kind, name, variant, initial)
                        if common.file_hash(executable) != identity:
                            raise ValueError("Owned executable changed")
                        if _element_python_argument(fixture, name) != argument:
                            raise ValueError("Owned script changed")
                        result = _parse_imported_element_probe(
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
                            result["variant"] != variant
                            or result["initial"] != initial
                            or not result["fresh_session"]
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
                        expected_outcome = (
                            "native_stderr"
                            if name.startswith("stderr")
                            else ("true" if expected_exit == 0 else "false")
                        )
                        if variant == "original":
                            correct = result["outcome"] == expected_outcome and (
                                name.startswith("stderr")
                                or (result["last_exit"] == expected_exit and result["waited_floor"])
                            )
                        else:
                            correct = (
                                result["outcome"] == expected_outcome
                                and result["actual_exit"] == expected_exit
                                and result["last_exit"]
                                == {"absent": None, "stale_zero": 0, "stale_nonzero": 9}[initial]
                                and result["waited_floor"]
                                and result["process_cleaned"]
                            )
                        if not correct:
                            if variant == "original":
                                original_misses += 1
                            else:
                                process_misses += 1
        for kind, (executable, identity) in binaries.items():
            for name, expected_outcome, expected_exit in (
                ("stdout_bound", "true", 0),
                ("stdout_overflow", "output_limit", None),
                ("timeout", "timeout", None),
            ):
                context = _element_context("probe", kind, name, "process", "stale_zero")
                if common.file_hash(executable) != identity:
                    raise ValueError("Admitted runtime changed")
                argument = _element_python_argument(fixture, name)
                result = _parse_imported_element_probe(
                    capture(
                        probe,
                        "-Start",
                        START,
                        "-Executable",
                        executable,
                        "-Name:",
                        argument,
                        "-Variant",
                        "process",
                        "-Initial",
                        "stale_zero",
                    )
                )
                print("K5_ELEMENT_CASE=" + kind + ":" + name + ":stale_zero")
                print(ELEMENT_PROBE_PREFIX.decode() + json.dumps(result, separators=(",", ":")))
                if not (
                    result["fresh_session"]
                    and result["initial"] == "stale_zero"
                    and result["application_bound"]
                    and result["command_kind"] == "application"
                    and result["variant"] == "process"
                    and result["process_cleaned"]
                    and result["last_exit"] == 0
                    and result["waited_floor"]
                    and result["outcome"] == expected_outcome
                    and result["actual_exit"] == expected_exit
                ):
                    process_misses += 1
        for command, expected_kind in (
            (tmp_path / "missing.exe", "missing"),
            ("K5FixtureAlias", "alias"),
        ):
            for initial in ("absent", "stale_zero", "stale_nonzero"):
                for variant in ("original", "process"):
                    context = _element_context("negative", expected_kind, "zero", variant, initial)
                    result = _parse_imported_element_probe(
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
                    if (
                        result["variant"] != variant
                        or result["initial"] != initial
                        or result["command_kind"] != expected_kind
                        or result["application_bound"]
                    ):
                        raise ValueError("Non-application qualified as an owned executable")
                    if expected_kind == "missing" and initial == "stale_zero":
                        missing_refusals += int(result["outcome"] == "command_missing")
                    if variant == "process" and (
                        result["outcome"] != "command_missing"
                        or result["actual_exit"] is not None
                        or not result["process_cleaned"]
                        or result["last_exit"]
                        != {"absent": None, "stale_zero": 0, "stale_nonzero": 9}[initial]
                    ):
                        process_misses += 1
                    # Original substitution remains evidence, not helper admission.
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
    if complete is not True:
        pytest.fail("Owned native element-probe fixture failed", pytrace=False)
    try:
        print("K5_ELEMENT_ORIGINAL_MISSES=" + str(original_misses))
        print("K5_ELEMENT_PROCESS_MISSES=" + str(process_misses))
        print("K5_ELEMENT_MISSING_REFUSALS=" + str(missing_refusals))
        # Original counters preserve the demonstrated baseline. Qualification
        # below covers the actual product helper, not full installed Start acceptance.
        matrix_failed = bool(process_misses or missing_refusals != 2)
        print("K5_ELEMENT_PROCESS_QUALIFIED=" + str(not matrix_failed).lower())
        _element_diagnostic(
            _element_context("matrix"),
            "failed" if matrix_failed else "passed",
            ValueError() if matrix_failed else None,
        )
    except Exception:
        pytest.fail("Owned native element-probe reporting failed", pytrace=False)
    if missing_refusals != 2:
        pytest.fail("Missing executable did not refuse under stale-zero state", pytrace=False)
    if process_misses:
        pytest.fail("Product helper did not preserve actual exit/stderr semantics", pytrace=False)


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
        "actual_exit": None,
        "process_cleaned": False,
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


def test_element_probe_comparison_preserves_original_and_selects_process_prototype():
    assert "$functions.Count -ne 2" in ELEMENT_PROBE_SCRIPT
    assert "@'" not in ELEMENT_PRODUCT_HELPER
    assert "'Test-K5GStreamerElement','Invoke-K5NativeProbe'" in ELEMENT_PROBE_SCRIPT
    assert ELEMENT_PRODUCT_OBSERVED not in ELEMENT_PROBE_SCRIPT
    assert "$actual = $functions[$index].Extent.Text" in ELEMENT_PROBE_SCRIPT
    assert " | Out-Null" not in ELEMENT_PROBE_SCRIPT
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
    test_native_probe_dependency_is_exact_and_keeps_existing_smoke_selection()


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
    for phase in ELEMENT_FIXTURE_PHASES - {
        "shell_direct_control",
        "shell_utility_control",
    }:
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
    assert '"-Name",' not in section and section.count('"-Name:",') == 4
    assert '("ConsoleApplication", base, 3)' in section
    assert '("WindowsApplication", base.with_name("pythonw.exe"), 2)' in section
    assert "executable.parent != base.parent" in section
    assert section.count("common.file_hash(executable) != identity") == 3
    assert ELEMENT_PRODUCT_OBSERVED not in ELEMENT_PROBE_SCRIPT
    assert "$actual = $functions[$index].Extent.Text" in ELEMENT_PROBE_SCRIPT
    assert " | Out-Null" not in ELEMENT_PROBE_SCRIPT
    assert "Popen([executable, argument]" in ELEMENT_REFERENCE_SCRIPT
    assert "code = child.wait(timeout=5)" in ELEMENT_REFERENCE_SCRIPT
    assert "outputs != [b'', expected]" in ELEMENT_REFERENCE_SCRIPT
    assert "stream.read(65)" in ELEMENT_REFERENCE_SCRIPT
    assert "time.monotonic() + 10" in ELEMENT_REFERENCE_SCRIPT
    assert "reference_wait" in ELEMENT_REFERENCE_SCRIPT
    test_native_probe_dependency_is_exact_and_keeps_existing_smoke_selection()


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


def test_element_utility_route_is_exact_scoped_and_keeps_native_acceptance():
    import inspect

    assert (
        "Microsoft.PowerShell.Core\\Import-Module -Name $utilityManifest -PassThru"
        in ELEMENT_UTILITY_IMPORT
    )
    assert "$PSHOME" in ELEMENT_UTILITY_IMPORT and "__UTILITY_HOME__" in ELEMENT_UTILITY_IMPORT
    assert "__UTILITY_SHA256__" in ELEMENT_UTILITY_IMPORT
    assert "ExportedCmdlets['Write-Output']" in ELEMENT_UTILITY_IMPORT
    assert "Microsoft.PowerShell.Commands.WriteOutputCommand" in ELEMENT_UTILITY_IMPORT
    assert "Microsoft.PowerShell.Commands.Utility" in ELEMENT_UTILITY_IMPORT
    assert "GetPublicKeyToken()" in ELEMENT_UTILITY_IMPORT
    assert (
        "Get-Command" not in ELEMENT_UTILITY_IMPORT
        and "$env:PSModulePath" not in ELEMENT_UTILITY_IMPORT
    )
    assert ELEMENT_PROBE_SCRIPT.index(ELEMENT_UTILITY_IMPORT) < ELEMENT_PROBE_SCRIPT.index(
        "Get-Variable LASTEXITCODE"
    )
    assert "Get-Variable LASTEXITCODE -Scope Global" in ELEMENT_PROBE_SCRIPT
    assert "Set-Alias -Name K5FixtureAlias" in ELEMENT_PROBE_SCRIPT
    assert ELEMENT_PRODUCT_OBSERVED not in ELEMENT_PROBE_SCRIPT
    assert "$actual = $functions[$index].Extent.Text" in ELEMENT_PROBE_SCRIPT
    assert " | Out-Null" not in ELEMENT_PROBE_SCRIPT
    assert "Get-Command -Name $gstInspect -ErrorAction Stop" in ELEMENT_PROBE_SCRIPT
    assert "ConvertTo-Json" not in ELEMENT_PROBE_SCRIPT
    assert "Write-Output (" not in ELEMENT_PROBE_SCRIPT
    main = inspect.getsource(test_windows_exact_element_probe_uses_fresh_actual_native_exit)
    assert "_element_body_controls(" not in main
    assert "_element_utility_controls(" in main
    assert main.index("_element_utility_controls(") < main.index(
        '_element_context("python_control")'
    )
    assert "_element_bind_utility(ELEMENT_PROBE_SCRIPT" in main
    test_native_probe_dependency_is_exact_and_keeps_existing_smoke_selection()


def test_element_imported_probe_requires_complete_exact_prefix_and_existing_schema():
    record = dict(
        schema_version=ELEMENT_PROBE_SCHEMA,
        variant="original",
        initial="absent",
        fresh_session=True,
        command_kind="application",
        application_bound=True,
        outcome="true",
        last_exit=0,
        waited_floor=True,
        actual_exit=None,
        process_cleaned=False,
    )
    prefix = b"".join(
        ELEMENT_CHECKPOINT_PREFIX + name.encode() + b"\n"
        for name in (
            "utility_manifest_requested",
            "utility_import_requested",
            "utility_import_returned",
            "utility_binding_verified",
        )
    )
    payload = ELEMENT_PROBE_PREFIX + json.dumps(record).encode() + b"\n"
    parts = prefix.splitlines(keepends=True)
    prefix = b"".join(parts[:3]) + _utility_observation_bytes() + parts[3]
    raw = prefix + payload
    assert _parse_imported_element_probe(raw) == _parse_element_probe(payload) == record
    lines = raw.splitlines(keepends=True)
    for forged in (
        payload,
        raw[:-1],
        prefix + payload + payload,
        raw + b"PRIVATE\n",
        b"PRIVATE\n" + raw,
        b"".join(lines[1:]),
        b"".join([lines[1], lines[0], *lines[2:]]),
        b"".join([lines[0], lines[0], *lines[2:]]),
        b"x" * 4097,
        prefix + ELEMENT_PROBE_PREFIX + b'{"raw":"PRIVATE"}\n',
    ):
        with pytest.raises(ValueError):
            _parse_imported_element_probe(forged)


def test_element_direct_output_preserves_control_bytes_except_fixed_serialization():
    direct = _element_direct_control_script("-fixed")
    original = _element_control_script("-fixed")
    for name in ("shell_control_entered", "shell_control_exit"):
        before = (
            "[Console]::Out.WriteLine('K5_ELEMENT_CHECKPOINT=" + name + "'); [Console]::Out.Flush()"
        )
        assert direct.count(before) == 1
        direct = direct.replace(before, "Write-Output 'K5_ELEMENT_CHECKPOINT=" + name + "'", 1)
    before = (
        '[Console]::Out.WriteLine((\'K5_ELEMENT_ARGV={{"equal":{0},"hash_equal":{1}}}\' -f '
        "$equal.ToString().ToLowerInvariant(), $hashEqual.ToString().ToLowerInvariant())); "
        "[Console]::Out.Flush()"
    )
    assert direct.count(before) == 1
    direct = direct.replace(
        before, "Write-Output ('K5_ELEMENT_ARGV=' + ($record | ConvertTo-Json -Compress))", 1
    )
    assert direct.encode() == original.encode()
    generated = _element_direct_control_script("-fixed")
    assert "Write-Output" not in generated and "ConvertTo-Json" not in generated
    assert "if (-not $equal -or -not $hashEqual) { exit 1 }" in generated
    template = re.search(r"'K5_ELEMENT_ARGV=(.*?)' -f", generated).group(1)
    for equal in (True, False):
        for hashed in (True, False):
            raw = (
                ELEMENT_ARGV_PREFIX
                + template.format(str(equal).lower(), str(hashed).lower()).encode()
                + b"\n"
            )
            assert _element_complete_records(raw)[2] == [dict(equal=equal, hash_equal=hashed)]


def test_element_fixed_json_templates_preserve_scalar_fields_without_cmdlets():
    def template(source):
        expression = source.split("$json = (", 1)[1].split(") -f", 1)[0]
        return "".join(re.findall(r"'([^']*)'", expression))

    error_template = template(ELEMENT_CHILD_DIAGNOSTICS)
    for phase in ELEMENT_CHILD_PHASES:
        assert "'" + phase + "'" in ELEMENT_CHILD_DIAGNOSTICS
        for boundary in ("primary", "cleanup"):
            for code in (None, -(2**31), 0, 2**31 - 1):
                raw = (
                    ELEMENT_CHILD_PREFIX
                    + error_template.format(
                        phase, boundary, "unknown", "null" if code is None else str(code)
                    ).encode()
                    + b"\n"
                )
                assert _element_child_records(raw) == [
                    dict(
                        schema_version="element-child-failure-v1",
                        phase=phase,
                        boundary=boundary,
                        error="unknown",
                        hresult=code,
                    )
                ]
    probe_template = template(ELEMENT_PROBE_SCRIPT.split("$fixturePhase = 'probe_record'", 1)[1])
    for outcome in ELEMENT_PROBE_OUTCOMES:
        for code in (None, 0, 7, 9):
            for bound in (True, False):
                raw = (
                    ELEMENT_PROBE_PREFIX
                    + probe_template.format(
                        "process",
                        "stale_zero",
                        "application",
                        str(bound).lower(),
                        outcome,
                        "null" if code is None else str(code),
                        "false",
                        "null",
                        "false",
                    ).encode()
                    + b"\n"
                )
                assert _parse_element_probe(raw) == dict(
                    schema_version=ELEMENT_PROBE_SCHEMA,
                    variant="process",
                    initial="stale_zero",
                    fresh_session=True,
                    command_kind="application",
                    application_bound=bound,
                    outcome=outcome,
                    last_exit=code,
                    waited_floor=False,
                    actual_exit=None,
                    process_cleaned=False,
                )
    assert "ConvertTo-Json" not in ELEMENT_CHILD_DIAGNOSTICS
    assert "$Boundary -cnotin @('primary','cleanup')" in ELEMENT_CHILD_DIAGNOSTICS
    assert "$Phase -cnotin @(" in ELEMENT_CHILD_DIAGNOSTICS
    assert (
        "$value -notin @(0,7,9)" not in ELEMENT_PROBE_SCRIPT
    )  # The admitted Value is checked first.
    assert "$last.Value -notin @(0,7,9)" in ELEMENT_PROBE_SCRIPT


@pytest.mark.parametrize(
    "mode", ["valid", "missing", "empty", "oversize", "alias", "denied", "template", "duplicate"]
)
def test_element_manifest_binding_is_bounded_exact_and_does_not_search(mode, tmp_path, monkeypatch):
    common = _startup_witness().common
    home = tmp_path / "PowerShell home'owned"
    home.mkdir()
    powershell = home / "powershell.exe"
    powershell.write_bytes(b"owned-shell-placeholder")
    manifest = home / "Modules/Microsoft.PowerShell.Utility/Microsoft.PowerShell.Utility.psd1"
    manifest.parent.mkdir(parents=True)
    raw = b"@{ModuleVersion='3.1.0.0'}"
    if mode != "missing":
        manifest.write_bytes(
            b"" if mode == "empty" else b"x" * 65537 if mode == "oversize" else raw
        )
    if mode == "alias":
        target = manifest.with_suffix(".owned")
        manifest.rename(target)
        manifest.symlink_to(target)
    calls = []
    checked = common.local_path

    def local(path):
        calls.append(path)
        if mode == "denied":
            raise PermissionError("PRIVATE_DENIAL")
        return checked(path)

    monkeypatch.setattr(common, "local_path", local)
    source = ELEMENT_UTILITY_CONTROL
    if mode == "template":
        source = source.replace("__UTILITY_SHA256__", "0" * 64)
    elif mode == "duplicate":
        source += "__UTILITY_HOME__"
    if mode == "valid":
        bound = _element_bind_utility(source, powershell, common)
        expected_hash = hashlib.sha256(raw).hexdigest()
        assert expected_hash in bound and "__UTILITY_" not in bound
        assert str(home).replace("'", "''") in bound
        manifest.write_bytes(raw + b"changed")
        assert hashlib.sha256(manifest.read_bytes()).hexdigest() not in bound
        assert "if ($manifestHash -cne '" + expected_hash + "')" in bound
    else:
        with pytest.raises((ValueError, OSError, common.WitnessError)):
            _element_bind_utility(source, powershell, common)
    assert calls == ([powershell] if mode == "denied" else [powershell, manifest])


@pytest.mark.parametrize(
    "failure",
    [
        "none",
        "direct_timeout",
        "direct_extra",
        "utility_timeout",
        "utility_missing",
        "utility_unknown",
    ],
)
def test_element_utility_controls_require_both_routes_before_matrix(
    failure, tmp_path, monkeypatch, capsys
):
    common = _startup_witness().common
    direct = ["powershell.exe", "-File", "direct.ps1", "-Name:", "-fixed"]
    utility = ["powershell.exe", "-File", "utility.ps1"]
    env = {"fixed": "admitted"}
    calls = []

    def capture(_common, command, **kwargs):
        assert _common is common and kwargs["cwd"] == tmp_path and kwargs["env"] is env
        first = command is direct
        assert first or command is utility
        phase = "shell_direct_control" if first else "shell_utility_control"
        assert kwargs["context"] == _element_context(phase)
        calls.append(phase)
        if failure == ("direct_timeout" if first else "utility_timeout"):
            raise _ElementCaptureFailure
        if not first and failure == "utility_unknown":
            raise RuntimeError("PRIVATE_FAILURE")
        if first:
            raw = (
                b"K5_ELEMENT_CHECKPOINT=shell_control_entered\n"
                b'K5_ELEMENT_ARGV={"equal":true,"hash_equal":true}\n'
                b"K5_ELEMENT_CHECKPOINT=shell_control_exit\n"
            )
            return raw + b"PRIVATE_EXTRA\n" if failure == "direct_extra" else raw
        raw = b"".join(
            ELEMENT_CHECKPOINT_PREFIX + name.encode() + b"\n"
            for name in (
                "utility_manifest_requested",
                "utility_import_requested",
                "utility_import_returned",
                "utility_binding_verified",
                "utility_cmdlet_requested",
                "utility_cmdlet_result",
                "utility_cmdlet_returned",
            )
        )
        parts = raw.splitlines(keepends=True)
        raw = b"".join(parts[:3]) + _utility_observation_bytes() + b"".join(parts[3:])
        return b"" if failure == "utility_missing" else raw

    monkeypatch.setitem(_element_utility_controls.__globals__, "_capture_element_child", capture)
    if failure == "none":
        _element_utility_controls(common, direct, utility, cwd=tmp_path, env=env)
    else:
        with pytest.raises(_ElementCaptureFailure):
            _element_utility_controls(common, direct, utility, cwd=tmp_path, env=env)
    assert calls == (
        ["shell_direct_control"]
        if failure.startswith("direct_")
        else ["shell_direct_control", "shell_utility_control"]
    )
    output = capsys.readouterr()
    assert "PRIVATE" not in output.out + output.err


def _utility_observation_bytes(**changes):
    value = {key: True for key in ELEMENT_UTILITY_PREDICATES}
    value.update(
        schema_version="element-utility-binding-v3",
        module_base_kind="exact_pshome",
        module_path_kind="admitted_manifest",
    )
    value.update(changes)
    return (
        ELEMENT_UTILITY_BINDING_PREFIX + json.dumps(value, separators=(",", ":")).encode() + b"\n"
    )


def test_element_utility_binding_record_rejects_raw_forged_and_inconsistent_fields():
    valid = _utility_observation_bytes()
    assert len(_element_utility_binding_records(valid)) == 1
    _element_require_utility_observation(valid)
    for field in ELEMENT_UTILITY_PREDICATES:
        for forged in (None, 1, "PRIVATE", [], {}, 2**65):
            with pytest.raises(ValueError):
                _element_utility_binding_records(_utility_observation_bytes(**{field: forged}))
    for field in ELEMENT_UTILITY_CATEGORIES:
        for forged in (None, 1, True, "PRIVATE_PATH", [], {}):
            with pytest.raises(ValueError):
                _element_utility_binding_records(_utility_observation_bytes(**{field: forged}))
    for forged in (
        valid + valid,
        b"x" * 4097,
        ELEMENT_UTILITY_BINDING_PREFIX + b"[]",
        _utility_observation_bytes(raw="PRIVATE_PATH"),
        valid.rstrip()[:-1] + b',"token_match":true}\n',
        _utility_observation_bytes(module_base_kind="admitted_module_directory"),
        _utility_observation_bytes(module_base_ok=False),
        _utility_observation_bytes(module_path_ok=False),
        _utility_observation_bytes(module_path_kind="exact_pshome_utility_dll"),
    ):
        with pytest.raises(ValueError):
            _element_utility_binding_records(forged)
    # Diagnostics may report a complete mismatch; they cannot permit execution.
    mismatch = _utility_observation_bytes(module_base_ok=False, module_base_kind="other")
    assert not _element_utility_binding_records(mismatch)[0]["module_base_ok"]
    for rejected in (
        mismatch,
        _utility_observation_bytes(metadata_complete=False),
    ):
        with pytest.raises(ValueError):
            _element_require_utility_observation(rejected)


def test_element_utility_observation_has_complete_fixed_template_and_guarded_getters():
    expression = ELEMENT_UTILITY_OBSERVATION.split("$observedJson = (", 1)[1].split(") -f", 1)[0]
    template = "".join(re.findall(r"'([^']*)'", expression))
    args = ELEMENT_UTILITY_OBSERVATION.split(") -f\n", 1)[1].split("[Console]::Out.WriteLine", 1)[0]
    names = re.findall(r"\$observed\.([a-z_]+)", args)
    valid = _element_utility_binding_records(_utility_observation_bytes())[0]
    assert set(names) == valid.keys() - {"schema_version"} and len(set(names)) == len(names)
    rendered = template.format(
        *(str(valid[name]).lower() if type(valid[name]) is bool else valid[name] for name in names)
    )
    assert json.loads(rendered) == valid
    assert "SafeGetValue" not in ELEMENT_UTILITY_OBSERVATION
    assert "ReadAll" not in ELEMENT_UTILITY_OBSERVATION
    assert "GetAttributes" not in ELEMENT_UTILITY_OBSERVATION  # No extra assembly path reads.
    assert ELEMENT_UTILITY_IMPORT.index(
        "ComputeHash($manifestBytes)"
    ) < ELEMENT_UTILITY_IMPORT.index("\nWrite-K5UtilityBindingObservation\n")
    for predicate in ELEMENT_UTILITY_PREDICATES:
        assert predicate in ELEMENT_UTILITY_OBSERVATION
    assert "$observedModule = $null" in ELEMENT_UTILITY_OBSERVATION
    assert "if ($observed.cmdlet_type_ok)" in ELEMENT_UTILITY_OBSERVATION
    assert "if ($observedType -is [type])" in ELEMENT_UTILITY_OBSERVATION
    assert "if ($null -ne $observedAssembly)" in ELEMENT_UTILITY_OBSERVATION
    assert "$observed.metadata_complete = $false" in ELEMENT_UTILITY_OBSERVATION


@pytest.mark.parametrize("malformed", [False, True])
def test_element_utility_observation_survives_failure_without_raw_output_or_success(
    malformed, tmp_path, monkeypatch, capsys
):
    import io
    from types import SimpleNamespace

    common = _startup_witness().common
    closed = []
    raw = (
        _utility_observation_bytes(raw="PRIVATE")
        if malformed
        else _utility_observation_bytes(
            module_path_ok=False,
            module_path_kind="other",
        )
    )

    class Owned:
        def __init__(self, *args, **kwargs):
            self.process = SimpleNamespace(stdout=io.BytesIO(raw))

        def wait(self, seconds):
            raise common.WitnessError(
                "child_failed",
                common.diagnostic(
                    "probe_admission", child_exit_code=1, relay_exit_code=1, gate_state="exited"
                ),
            )

        def close(self):
            closed.append(True)

    monkeypatch.setattr(common, "OwnedProcess", Owned)
    with pytest.raises(_ElementCaptureFailure):
        _capture_element_child(
            common, [], cwd=tmp_path, env={}, context=_element_context("shell_utility_control")
        )
    output = capsys.readouterr()
    assert closed == [True] and "PRIVATE" not in output.out + output.err
    diagnostics = _fixture_records(output.out)
    assert diagnostics[1]["error"] == "child_failed"
    assert diagnostics[1]["child_exit"] == diagnostics[1]["relay_exit"] == 1
    observations = _element_utility_binding_records(output.out.encode())
    assert len(observations) == (0 if malformed else 1)
    if observations:
        assert not observations[0]["module_path_ok"]


def test_element_incomplete_binding_observation_refuses_before_native_invocation():
    emitted = ELEMENT_UTILITY_OBSERVATION.index(
        "[Console]::Out.WriteLine('K5_ELEMENT_UTILITY_BINDING='"
    )
    refusal = ELEMENT_UTILITY_OBSERVATION.index("if (-not $observed.metadata_complete)")
    assert (
        emitted < refusal < ELEMENT_UTILITY_OBSERVATION.index("throw 'fixture_identity'", refusal)
    )
    assert "'utility_observation'" in ELEMENT_CHILD_DIAGNOSTICS
    assert ELEMENT_PROBE_SCRIPT.index(
        "\nWrite-K5UtilityBindingObservation\n"
    ) < ELEMENT_PROBE_SCRIPT.index("$answer = Test-K5GStreamerElement")


def test_element_module_base_requires_only_qualified_pshome():
    assert "GetFullPath($utilityModule.ModuleBase), $expectedHome," in ELEMENT_UTILITY_IMPORT
    assert (
        "$observed.module_base_ok = [string]::Equals($observedBase, $expectedHome,"
        in ELEMENT_UTILITY_OBSERVATION
    )
    valid = _utility_observation_bytes(module_base_ok=True, module_base_kind="exact_pshome")
    _element_require_utility_observation(valid)
    for kind in ("other", "unavailable"):
        observation = _utility_observation_bytes(module_base_ok=False, module_base_kind=kind)
        _element_utility_binding_records(observation)
        with pytest.raises(ValueError):
            _element_require_utility_observation(observation)


def test_element_binding_v3_refuses_legacy_shapes_and_removed_metadata():
    for schema in ("element-utility-binding-v1", "element-utility-binding-v2"):
        with pytest.raises(ValueError):
            _element_utility_binding_records(_utility_observation_bytes(schema_version=schema))
    for field, value in (
        ("manifest_shape", "supported"),
        ("root_module_kind", "absent"),
        ("nested_modules_kind", "absent"),
        ("declared_binary_path_match", False),
    ):
        with pytest.raises(ValueError):
            _element_utility_binding_records(_utility_observation_bytes(**{field: value}))
    for field, value in (
        ("module_base_kind", "admitted_module_directory"),
        ("module_base_kind", "exact_verified_export_directory"),
        ("module_path_kind", "exact_pshome_utility_dll"),
        ("module_path_kind", "exact_verified_export_assembly"),
    ):
        with pytest.raises(ValueError):
            _element_utility_binding_records(_utility_observation_bytes(**{field: value}))


def test_element_utility_observation_has_no_manifest_shape_prerequisite():
    for retired in (
        "ParseInput",
        "RootModule",
        "NestedModules",
        "manifest_shape",
        "root_module_kind",
        "nested_modules_kind",
        "declared_binary_path_match",
        "Assembly.Location",
    ):
        assert retired not in ELEMENT_UTILITY_OBSERVATION
    assert "UTILITY_AST_CASES" not in globals()
    assert "_utility_ast_fixture_script" not in globals()
    assert "utility_ast" not in ELEMENT_FIXTURE_PHASES
    assert not any(name.startswith("utility_ast_") for name in ELEMENT_CHECKPOINTS)


def test_element_utility_retains_exact_manifest_and_security_binding():
    import hashlib

    start = ELEMENT_UTILITY_IMPORT.index("$fixturePhase = 'utility_manifest'\n")
    observation = ELEMENT_UTILITY_IMPORT.index("$fixturePhase = 'utility_observation'\n")
    binding = ELEMENT_UTILITY_IMPORT.index("$fixturePhase = 'utility_binding'\n")
    assert hashlib.sha256(ELEMENT_UTILITY_IMPORT[start:observation].encode()).hexdigest() == (
        "9379db8d74280be6e6adc2f245860f46c373864673b237c27d117baf2bed3fa2"
    )
    assert hashlib.sha256(ELEMENT_UTILITY_IMPORT[binding:].encode()).hexdigest() == (
        "21665ed036596fa4136700b6c6515cc7bb5b5fe0cb905dbae13a4756fecd4c0e"
    )


def test_element_binding_v3_contains_only_retained_predicates_and_categories():
    value = _element_utility_binding_records(_utility_observation_bytes())[0]
    assert value["schema_version"] == "element-utility-binding-v3"
    assert value.keys() == ELEMENT_UTILITY_PREDICATES | {
        "schema_version",
        "module_base_kind",
        "module_path_kind",
    }
    _element_require_utility_observation(_utility_observation_bytes())


@pytest.mark.parametrize("predicate", sorted(ELEMENT_UTILITY_PREDICATES))
def test_element_utility_each_retained_predicate_is_required(predicate):
    changes = {predicate: False}
    if predicate == "module_base_ok":
        changes["module_base_kind"] = "other"
    elif predicate == "module_path_ok":
        changes["module_path_kind"] = "other"
    raw = _utility_observation_bytes(**changes)
    assert _element_utility_binding_records(raw)[0][predicate] is False
    with pytest.raises(ValueError):
        _element_require_utility_observation(raw)


def test_element_process_candidate_binds_actual_exit_without_ambient_writes():
    prototype = globals().get("ELEMENT_PRODUCT_HELPER", "")
    assert "$exitCode = $child.ExitCode" in prototype
    assert "LASTEXITCODE" not in prototype
    assert "$info.FileName = $command.Path" in prototype
    assert "$info.Arguments = [string]::Join" in prototype
    assert "131072" in prototype and "$child.Kill()" in prototype
    assert "ReadAsync" in prototype and "$child.Dispose()" in prototype


def test_element_process_probe_has_distinct_actual_exit_schema():
    assert ELEMENT_PROBE_SCHEMA == "element-native-exit-probe-v2"
    assert '"actual_exit"' in ELEMENT_PROBE_SCRIPT
    assert " | Out-Null" not in ELEMENT_PROBE_SCRIPT


def test_element_process_scalar_observations_strip_to_exact_product_helper():
    stripped = ELEMENT_PRODUCT_OBSERVED
    assert set(ELEMENT_PROCESS_INSERTIONS.values()) == {
        "        $script:fixtureActualExit = $exitCode\n",
        "        $script:fixtureProcessCleaned = $true\n",
    }
    for anchor, insertion in ELEMENT_PROCESS_INSERTIONS.items():
        assert stripped.count(anchor + insertion) == 1
        stripped = stripped.replace(anchor + insertion, anchor, 1)
    assert stripped.encode() == ELEMENT_PRODUCT_HELPER.encode()
    assert "fixture" not in ELEMENT_PRODUCT_HELPER
    assert ELEMENT_PRODUCT_OBSERVED not in ELEMENT_PROBE_SCRIPT
    assert "$actual = $functions[$index].Extent.Text" in ELEMENT_PROBE_SCRIPT


def test_element_process_drains_concurrently_and_cleanup_is_owned_bounded():
    source = ELEMENT_PRODUCT_HELPER
    first_read = source.index("$outRead = $stdoutStream.ReadAsync")
    second_read = source.index("$errRead = $stderrStream.ReadAsync")
    loop = source.index("while ($true)")
    exited = source.index("if ($child.HasExited -and $outDone -and $errDone)")
    actual = source.index("$exitCode = $child.ExitCode")
    stderr = source.index("if ($errCount -ne 0 -and -not $DiscardStderr)")
    assert first_read < second_read < loop < exited < actual < stderr
    assert source.count("$outCount + $errCount -gt 131072") == 2
    assert source.count("$stdoutStream.ReadAsync") == source.count("$stderrStream.ReadAsync") == 2
    assert "$watch.Elapsed.TotalSeconds -ge 5" in source
    assert "$info.UseShellExecute = $false" in source
    assert "$command -isnot [Management.Automation.ApplicationInfo]" in source
    guard = source.index("$child.Handle -ne $childHandle")
    kill = source.index("$child.Kill()")
    dispose = source.index("$child.Dispose()")
    tasks = source.index("while (($null -ne $outRead")
    done = source.index("# The exact started Process")
    assert guard < kill < dispose < tasks < done
    assert "$child.WaitForExit(5000)" in source
    assert "$cleanupWatch.Elapsed.TotalSeconds -ge 5" in source
    assert "foreach ($ownedStream in @($stdoutStream, $stderrStream, $capturedStdout))" in source
    assert "$ownedStream.Close()" in source and "catch { $cleanupFailed = $true }" in source
    assert "GetProcessById" not in source and "Stop-Process" not in source
    assert "ReadToEnd" not in source
    assert "if ($CaptureOutput) { $capturedStdout = [IO.MemoryStream]::new() }" in source


def test_element_process_name_guard_preserves_one_exact_bounded_native_argument(tmp_path):
    expression = re.search(r"\$Name -match '([^']+)'", ELEMENT_PRODUCT_HELPER).group(1)
    script = tmp_path / "owned fixture 'one'.py"
    script.write_bytes(ELEMENT_PROBE_PYTHON_SOURCE.encode("ascii"))
    for name in ("rtspsrc", "d3d11h264dec", _element_python_argument(script, "zero")):
        assert 0 < len(name) <= 8192 and re.search(expression, name) is None
        assert subprocess.list2cmdline([name]) == name
    for name in ("two names", "line\nfeed", "tab\tvalue", 'a"b', "a\\b", "a\x00b", "é"):
        assert re.search(expression, name) is not None
    assert "$Name.Length -eq 0 -or $Name.Length -gt 8192" in ELEMENT_PRODUCT_HELPER
    assert "$info.Arguments = [string]::Join" in ELEMENT_PRODUCT_HELPER


@pytest.mark.parametrize("case,size", [("stdout_bound", 131072), ("stdout_overflow", 131073)])
def test_element_process_output_cases_use_finite_owned_bytes(tmp_path, case, size):
    script = tmp_path / "owned.py"
    script.write_bytes(ELEMENT_PROBE_PYTHON_SOURCE.encode("ascii"))
    argument = _element_python_argument(script, case)
    result = subprocess.run([sys.executable, argument], capture_output=True, timeout=5, check=False)
    assert result.returncode == 0 and not result.stderr
    assert result.stdout == b"x" * size


def test_element_process_record_rejects_forged_exit_cleanup_and_legacy_fields():
    value = dict(
        schema_version=ELEMENT_PROBE_SCHEMA,
        variant="process",
        initial="stale_nonzero",
        fresh_session=True,
        command_kind="application",
        application_bound=True,
        outcome="true",
        last_exit=9,
        waited_floor=True,
        actual_exit=0,
        process_cleaned=True,
    )

    def encoded(changes):
        return ELEMENT_PROBE_PREFIX + json.dumps({**value, **changes}).encode() + b"\n"

    assert _parse_element_probe(encoded({})) == value
    for changes in (
        {"actual_exit": True},
        {"actual_exit": "0"},
        {"actual_exit": 9},
        {"actual_exit": 2**64},
        {"process_cleaned": 1},
        {"process_cleaned": "PRIVATE"},
        {"schema_version": "element-native-exit-probe-v1"},
        {"variant": "pipeline"},
        {"variant": "original"},
        {"raw": "PRIVATE"},
    ):
        with pytest.raises(ValueError):
            _parse_element_probe(encoded(changes))


def test_element_process_matrix_requires_real_exit_stale_independence_and_cleanup():
    import inspect

    source = inspect.getsource(test_windows_exact_element_probe_uses_fresh_actual_native_exit)
    assert 'result["actual_exit"] == expected_exit' in source
    assert 'result["process_cleaned"]' in source
    assert 'result["waited_floor"]' in source
    assert '"absent": None, "stale_zero": 0, "stale_nonzero": 9' in source
    for case in ("stdout_bound", "stdout_overflow", "timeout"):
        assert '"' + case + '"' in source
    assert "matrix_failed = bool(process_misses or missing_refusals != 2)" in source
    assert "if original_misses:" not in source
    assert "K5_ELEMENT_ORIGINAL_MISSES=" in source
    assert "K5_ELEMENT_PROCESS_QUALIFIED=" in source


def _element_process_cleanup_fixture():
    start = ELEMENT_PRODUCT_HELPER.index("        $cleanupWatch = ")
    end = ELEMENT_PRODUCT_HELPER.index("        # The exact started Process", start)
    cleanup = ELEMENT_PRODUCT_HELPER[start:end]
    return (
        "$ErrorActionPreference='Stop'\nSet-StrictMode -Version Latest\n"
        "function Invoke-OwnedCleanup {\n"
        + cleanup
        + "}\n"
        + r"""
foreach ($case in @('first_close','changed_handle','wait_failed','pending_read')) {
    [Console]::Out.WriteLine('K5_ELEMENT_CHECKPOINT=process_cleanup_' + $case + '_requested')
    [Console]::Out.Flush()
    $script:secondClosed = $false; $script:disposed = $false; $script:killed = $false
    $primaryFailure = [Management.Automation.ErrorRecord]::new(
        [IO.IOException]::new('PRIVATE_STARTUP'), 'fixed',
        [Management.Automation.ErrorCategory]::NotSpecified, $null)
    $stdoutStream = [pscustomobject]@{}
    $stdoutStream.PSObject.Members.Add([Management.Automation.PSScriptMethod]::new('Close', {
        if ($case -ceq 'first_close') { throw 'PRIVATE_FIRST_CLOSE' }
    }))
    $stderrStream = [pscustomobject]@{}
    $stderrStream.PSObject.Members.Add([Management.Automation.PSScriptMethod]::new('Close', {
        $script:secondClosed = $true
    }))
    $child = [pscustomobject]@{Handle=[IntPtr]1;HasExited=$false}
    $child.PSObject.Members.Add([Management.Automation.PSScriptMethod]::new('Kill', {
        $script:killed = $true
    }))
    $child.PSObject.Members.Add([Management.Automation.PSScriptMethod]::new('WaitForExit', {
        param($milliseconds)
        if ($milliseconds -ne 5000) { throw 'PRIVATE_WAIT' }
        return $false
    }))
    $child.PSObject.Members.Add([Management.Automation.PSScriptMethod]::new('Dispose', {
        $script:disposed = $true
    }))
    $started = $case -in @('changed_handle','wait_failed')
    $childHandle = if ($case -ceq 'changed_handle') { [IntPtr]2 } else { [IntPtr]1 }
    $outRead = $null; $errRead = $null; $capturedStdout = $null
    if ($case -ceq 'pending_read') { $outRead = [pscustomobject]@{IsCompleted=$false} }
    $refused = $false
    try { Invoke-OwnedCleanup }
    catch {
        $refused = $_.Exception -is [InvalidOperationException] -and
            $_.Exception.Message -ceq 'K5 native cleanup failed.' -and
            [object]::ReferenceEquals(
                $_.Exception.Data['K5ElementPrimaryErrorRecord'], $primaryFailure)
    }
    if (-not $refused -or -not $script:secondClosed -or -not $script:disposed -or
        $script:killed -ne ($case -ceq 'wait_failed')) { exit 1 }
    [Console]::Out.WriteLine('K5_ELEMENT_CHECKPOINT=process_cleanup_' + $case + '_passed')
    [Console]::Out.Flush()
}
exit 0
"""
    )


ELEMENT_CHECKPOINTS.update(
    "process_cleanup_" + case + "_" + state
    for case in ("first_close", "changed_handle", "wait_failed", "pending_read")
    for state in ("requested", "passed")
)


def test_element_process_cleanup_failure_fixture_uses_exact_owned_finally():
    script = _element_process_cleanup_fixture()
    start = ELEMENT_PRODUCT_HELPER.index("        $cleanupWatch = ")
    end = ELEMENT_PRODUCT_HELPER.index("        # The exact started Process", start)
    assert script.count(ELEMENT_PRODUCT_HELPER[start:end]) == 1
    assert "Process]::new" not in script and "Get-Command" not in script
    assert "ReferenceEquals" in script and "-not $script:secondClosed" in script
    assert len(script.encode()) < 8192


@pytest.mark.skipif(sys.platform != "win32", reason="Requires Windows PowerShell cleanup semantics")
def test_windows_element_process_cleanup_failures_are_fatal_and_independent(tmp_path):
    import os

    common = None
    context = _element_context("probe")
    try:
        module = _startup_witness()
        common = module.common
        base = common.local_path(Path(sys._base_executable))
        binding = dict(
            K5_WITNESS_BASE_PYTHON=str(base), K5_WITNESS_BASE_PYTHON_SHA256=common.file_hash(base)
        )
        supplied = {key: os.environ.get(key) for key in common.GATE_RUNTIME_KEYS}
        if any(value is not None for value in supplied.values()) and supplied != binding:
            raise ValueError("Invalid fixture runtime")
        env = module.clean_environment(dict(os.environ), tmp_path)
        env.update(binding)
        for key in ("TEMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA"):
            Path(env[key]).mkdir(parents=True, exist_ok=True)
        shell = common.local_path(
            Path(env["SYSTEMROOT"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        )
        script = tmp_path / "process-cleanup.ps1"
        script.write_text(_element_process_cleanup_fixture(), encoding="ascii", newline="\n")
        output = _capture_element_child(
            common,
            [str(shell), "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(script)],
            cwd=tmp_path,
            env=env,
            context=context,
        )
        expected = [
            ELEMENT_CHECKPOINT_PREFIX + ("process_cleanup_" + case + "_" + state).encode()
            for case in ("first_close", "changed_handle", "wait_failed", "pending_read")
            for state in ("requested", "passed")
        ]
        if output.splitlines() != expected:
            raise ValueError("Invalid fixed cleanup result")
    except Exception as error:
        try:
            _element_diagnostic(context, "failed", error, common=common)
        except Exception:
            pass
        pytest.fail("Owned Process cleanup fixture failed", pytrace=False)


def test_element_process_late_alias_failure_cannot_qualify(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace

    function = test_windows_exact_element_probe_uses_fresh_actual_native_exit
    namespace = function.__globals__
    keys = ("K5_WITNESS_BASE_PYTHON", "K5_WITNESS_BASE_PYTHON_SHA256")
    for key in keys:
        monkeypatch.delenv(key, raising=False)
    common = SimpleNamespace(
        GATE_RUNTIME_KEYS=keys,
        local_path=lambda path: Path(path),
        file_hash=lambda path: "fixed",
        admitted_gate_python=lambda env: None,
    )
    env = {
        key: str(tmp_path / key)
        for key in (
            "TEMP",
            "USERPROFILE",
            "APPDATA",
            "LOCALAPPDATA",
            "SYSTEMROOT",
        )
    }
    monkeypatch.setitem(
        namespace,
        "_startup_witness",
        lambda: SimpleNamespace(
            common=common,
            clean_environment=lambda *args: env.copy(),
        ),
    )
    monkeypatch.setitem(namespace, "_element_bind_utility", lambda script, *args: script)
    monkeypatch.setitem(
        namespace, "_pe_fixture_subsystem", lambda path: 2 if path.name == "pythonw.exe" else 3
    )
    monkeypatch.setitem(namespace, "_element_utility_controls", lambda *args, **kwargs: None)
    calls = []

    def capture(common, arguments, *, cwd, env, context):
        calls.append(context.copy())
        phase, case = context["phase"], context["case"]
        if phase == "python_control":
            return b"K5_ELEMENT_CHECKPOINT=python_control_entered\n"
        if phase == "reference":
            code = 7 if case in ("nonzero", "stderr_nonzero") else 0
            return (
                b"".join(
                    ELEMENT_CHECKPOINT_PREFIX + name.encode() + b"\n"
                    for name in (
                        "reference_entered",
                        "reference_start_requested",
                        "reference_started",
                        "reference_waited",
                        "reference_stdio_verified",
                    )
                )
                + b"K5_NATIVE_REFERENCE="
                + str(code).encode()
                + b"\n"
                + (ELEMENT_CHECKPOINT_PREFIX + b"reference_cleanup_complete\n")
            )
        kind = context["kind"] if phase == "negative" else "application"
        if kind == "alias":
            raise _ElementCaptureFailure
        variant, initial = context["variant"], context["initial"]
        outcome = (
            "command_missing"
            if kind == "missing"
            else "native_stderr"
            if case.startswith("stderr_")
            else "output_limit"
            if case == "stdout_overflow"
            else "timeout"
            if case == "timeout"
            else "false"
            if case == "nonzero"
            else "true"
        )
        actual = 7 if case in ("nonzero", "stderr_nonzero") else 0
        if kind == "missing" or case in ("timeout", "stdout_overflow") or variant == "original":
            actual = None
        value = dict(
            schema_version=ELEMENT_PROBE_SCHEMA,
            variant=variant,
            initial=initial,
            fresh_session=True,
            command_kind=kind,
            application_bound=kind == "application",
            outcome=outcome,
            last_exit={"absent": None, "stale_zero": 0, "stale_nonzero": 9}[initial],
            waited_floor=True,
            actual_exit=actual,
            process_cleaned=variant == "process",
        )
        prefix = b"".join(
            ELEMENT_CHECKPOINT_PREFIX + name.encode() + b"\n"
            for name in (
                "utility_manifest_requested",
                "utility_import_requested",
                "utility_import_returned",
            )
        )
        return (
            prefix
            + _utility_observation_bytes()
            + (
                ELEMENT_CHECKPOINT_PREFIX
                + b"utility_binding_verified\n"
                + ELEMENT_PROBE_PREFIX
                + json.dumps(value).encode()
                + b"\n"
            )
        )

    monkeypatch.setitem(namespace, "_capture_element_child", capture)
    with pytest.raises(pytest.fail.Exception, match="Owned native element-probe fixture failed"):
        function(tmp_path)
    assert calls[-1]["kind"] == "alias"
    assert sum(c["kind"] == "missing" for c in calls) == 6
    assert "K5_ELEMENT_PROCESS_QUALIFIED=true" not in capsys.readouterr().out


def test_actual_start_native_helpers_match_checked_source_hashes():
    assert _element_product_helper(START.read_bytes()) == ELEMENT_PRODUCT_HELPER


def test_element_product_extraction_accepts_only_checked_raw_representations():
    canonical = ELEMENT_PRODUCT_HELPER.strip("\n").encode("ascii")
    for source in (canonical, canonical.replace(b"\n", b"\r\n")):
        assert _element_product_helper(source) == ELEMENT_PRODUCT_HELPER
    for source in (
        canonical.replace(b"\n", b"\r\n", 1),
        canonical.replace(b"$exitCode = $child.ExitCode", b"$exitCode = 0"),
        canonical + b"\n" + canonical,
        ELEMENT_HISTORICAL_HELPER.encode(),
        b"x" * 262145,
        b"",
        canonical.decode(),
    ):
        with pytest.raises(ValueError):
            _element_product_helper(source)


def test_element_actual_ast_selection_checks_raw_identity_before_scalar_insertions():
    source = ELEMENT_PROBE_SCRIPT
    extent = source.index("$actual = $functions[$index].Extent.Text")
    admission = source.index("if ($helperHash -cnotin $helperHashes", extent)
    normalized = source.index('$actualParts.Add($actual.Replace("`r`n", "`n"))', admission)
    selected = source.index('$selected = [string]::Join("`n`n",', normalized)
    instrumented = source.index("$selected = $selected.Replace($anchor", selected)
    invoked = source.index(". ([scriptblock]::Create($selected))", instrumented)
    assert extent < admission < normalized < selected < instrumented < invoked
    for digests in ELEMENT_PRODUCT_HASHES.values():
        for digest in digests:
            assert source.count(digest) == 1
    assert source.count("if (($selected.Split(@($anchor)") == 2
    assert "$helperHasher.Dispose()" in source
    assert "[Diagnostics.ProcessStartInfo]::new()" not in source
    assert ELEMENT_HISTORICAL_HELPER in source
    assert "ELEMENT_PROCESS_PROTOTYPE" not in globals()


def test_start_remaining_native_admissions_never_read_ambient_exit_status():
    source = START.read_text()
    assert "$LASTEXITCODE" not in source
    for call in (
        'Invoke-K5NativeProbe -Executable $mediaMtx -Arguments @("--version") -CaptureOutput',
        'Invoke-K5NativeProbe -Executable $mediaMtx -Arguments @("--validate-conf", $configPath)',
        "Invoke-K5NativeProbe -Executable $python "
        '-Arguments @("-I", "-B", "-c", $resolveCode, $PublicRtspSource) '
        "-CaptureOutput -DiscardStderr",
    ):
        assert call in source


def test_start_shared_native_probe_is_single_actual_process_pump():
    source = START.read_text()
    assert source.count("function Invoke-K5NativeProbe {") == 1
    helper = source.split("function Invoke-K5NativeProbe {", 1)[1].split(
        "\nfunction Get-K5MediaMtx", 1
    )[0]
    assert "$info.Arguments = [string]::Join" in helper
    assert "$exitCode = $child.ExitCode" in helper
    assert "Stdout = $outputText" in helper


SHARED_NATIVE_CASES = {
    "version": (
        ("version_ok", True, 0, "none"),
        ("version_wrong", False, 0, "caller"),
        ("version_multi", False, 0, "caller"),
        ("version_empty", False, 0, "caller"),
        ("version_nonzero", False, 7, "caller"),
        ("version_stderr", False, 0, "stderr"),
        ("version_utf8", False, 0, "decode"),
    ),
    "config": (
        ("config_ok", True, 0, "none"),
        ("config_nonzero", False, 7, "caller"),
        ("config_stderr", False, 0, "stderr"),
        ("config_bytes", True, 0, "none"),
    ),
    "public": (
        ("public_ok", True, 0, "none"),
        ("public_cr", True, 0, "none"),
        ("public_crlf", True, 0, "none"),
        ("public_cr_multi", False, 0, "caller"),
        ("public_blank", False, 0, "caller"),
        ("public_cr_blank", False, 0, "caller"),
        ("public_empty", False, 0, "caller"),
        ("public_multi", False, 0, "caller"),
        ("public_nonzero", False, 7, "caller"),
        ("public_stderr", True, 0, "none"),
        ("public_utf8", False, 0, "decode"),
    ),
    "argv": (
        ("argv_ok", True, 0, "none"),
        ("argv_nul", False, None, "argument"),
        ("argv_none", False, None, "argument"),
        ("argv_count", False, None, "argument"),
        ("argv_single", False, None, "argument"),
        ("argv_total", False, None, "argument"),
    ),
}
SHARED_ARGV = ["", "two words", 'inside"quote', "one\\", "two\\\\", '\\"', "Ω", "last\\"]
ELEMENT_CHECKPOINTS.update(
    "shared_" + mode + "_" + state
    for cases in SHARED_NATIVE_CASES.values()
    for mode, *_ in cases
    for state in ("requested", "passed")
)

SHARED_NATIVE_SCRIPT = r"""
param([string]$Start, [string]$StartHash, [string]$Python, [string]$Fixture,
      [string]$ConfigPath, [string]$Boundary, [string]$Initial)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
__ELEMENT_CHILD_DIAGNOSTICS__
$fixturePhase = 'utility_manifest'
try {
__ELEMENT_UTILITY_IMPORT__
    $fixturePhase = 'source_select'
    $stream = [IO.File]::OpenRead($Start)
    $bytes = [byte[]]::new(262145)
    $total = 0
    try {
        while ($total -lt $bytes.Length) {
            $count = $stream.Read($bytes,$total,$bytes.Length - $total)
            if ($count -eq 0) { break }
            $total += $count
        }
    } finally { $stream.Dispose() }
    if ($total -gt 262144) { throw 'fixture_identity' }
    $hasher = [Security.Cryptography.SHA256]::Create()
    try {
        $actualHash = [BitConverter]::ToString(
            $hasher.ComputeHash($bytes,0,$total)).Replace('-','').ToLowerInvariant()
        if ($actualHash -cne $StartHash) { throw 'fixture_identity' }
    } finally { $hasher.Dispose() }
    $source = [Text.UTF8Encoding]::new($false,$true).GetString($bytes,0,$total)
    $tokens = $null; $errors = $null
    $ast = [Management.Automation.Language.Parser]::ParseInput($source,[ref]$tokens,[ref]$errors)
    if ($errors.Count -ne 0) { throw 'fixture_identity' }
    $helpers = @($ast.FindAll({param($node)
        $node -is [Management.Automation.Language.FunctionDefinitionAst] -and
        $node.Name -ceq 'Invoke-K5NativeProbe'
    },$true))
    if ($helpers.Count -ne 1) { throw 'fixture_identity' }
    $helper = $helpers[0].Extent.Text.Replace("`r`n","`n") + "`n"
    $signature = 'function Invoke-K5NativeProbe {'
    if (($helper.Split(@($signature),[StringSplitOptions]::None)).Count -ne 2) {
        throw 'fixture_identity'
    }
    $helper = $helper.Replace($signature,'function Invoke-K5NativeProbeActual {')
__SHARED_OBSERVATIONS__
    . ([scriptblock]::Create($helper))
    $pin = @($ast.EndBlock.Statements | Where-Object {
        $_ -is [Management.Automation.Language.AssignmentStatementAst] -and
        $_.Left.Extent.Text -ceq '$MediaMtxVersion'
    })
    if ($pin.Count -ne 1 -or $pin[0].Right.Extent.Text -cne '"1.21.1"') {
        throw 'fixture_identity'
    }
    . ([scriptblock]::Create($pin[0].Extent.Text))
    $names = @{version='mediaMtxVersionOutput';config='validation';public='resolved'}
    if ($Boundary -cne 'argv') {
        if (-not $names.ContainsKey($Boundary)) { throw 'fixture_identity' }
        $wanted = $names[$Boundary]
        $assignments = @($ast.FindAll({param($node)
            $node -is [Management.Automation.Language.AssignmentStatementAst] -and
            $node.Left -is [Management.Automation.Language.VariableExpressionAst] -and
            $node.Left.VariablePath.UserPath -ceq $wanted -and
            $node.Right.Extent.Text.StartsWith('Invoke-K5NativeProbe -Executable ')
        },$true))
        if ($assignments.Count -ne 1) { throw 'fixture_identity' }
        $parent = $assignments[0].Parent
        if ($parent -isnot [Management.Automation.Language.NamedBlockAst] -and
            $parent -isnot [Management.Automation.Language.StatementBlockAst]) {
            throw 'fixture_identity'
        }
        $statements = @($parent.Statements)
        $first = [Array]::IndexOf($statements,$assignments[0])
        $last = $first + 1
        if ($Boundary -ceq 'public') {
            $ends = @($statements | Where-Object {
                $_ -is [Management.Automation.Language.IfStatementAst] -and
                $_.Extent.Text.Contains('[string]::IsNullOrWhiteSpace($publicSourceIp)')
            })
            if ($ends.Count -ne 1) { throw 'fixture_identity' }
            $last = [Array]::IndexOf($statements,$ends[0])
        }
        if ($first -lt 0 -or $last -le $first -or $last -ge $statements.Count -or
            $last - $first -gt 12 -or
            $statements[$last] -isnot [Management.Automation.Language.IfStatementAst]) {
            throw 'fixture_identity'
        }
        $body = ''
        for ($index=$first; $index -le $last; $index++) {
            $body += $statements[$index].Extent.Text + "`n"
        }
        . ([scriptblock]::Create("function Invoke-K5CallerOnly {`n" + $body + "`n}"))
    }
    $resolveCode = '__RESOLVE_CODE__'
    $PublicRtspSource = 'rtsp://example.invalid:8554/owned%20path'
    $mediaMtx = $Python
    $argv = ConvertFrom-Json '__ARGV__'
    $cases = ConvertFrom-Json '__CASES__'
    if ($null -ne (Get-Variable LASTEXITCODE -Scope Global -ErrorAction SilentlyContinue)) {
        throw 'fixture_not_fresh'
    }
    if ($Initial -ceq 'stale_zero') { $global:LASTEXITCODE = 0 }
    elseif ($Initial -ceq 'stale_nonzero') { $global:LASTEXITCODE = 9 }
    elseif ($Initial -cne 'absent') { throw 'fixture_invalid' }
    # Only the executable/fixture prefix is substituted. The original caller's
    # exact argument vector, capture policy and result validator execute below.
    function Invoke-K5NativeProbe {
        param($Executable,[string[]]$Arguments,[switch]$CaptureOutput,[switch]$DiscardStderr)
        $expected = switch ($Boundary) {
            'version' { ,@('--version') }
            'config' { ,@('--validate-conf',$ConfigPath) }
            'public' { ,@('-I','-B','-c',$resolveCode,$PublicRtspSource) }
        }
        if ($Executable -cne $Python -or $Arguments.Count -ne $expected.Count -or
            [bool]$CaptureOutput -ne ($Boundary -cne 'config') -or
            [bool]$DiscardStderr -ne ($Boundary -ceq 'public')) { throw 'fixture_policy' }
        for ($index=0; $index -lt $expected.Count; $index++) {
            if ($Arguments[$index] -cne $expected[$index]) { throw 'fixture_arguments' }
        }
        return Invoke-K5NativeProbeActual -Executable $Python `
            -Arguments (@('-I','-B','-S',$Fixture,$case.mode) + $Arguments) `
            -CaptureOutput:$CaptureOutput -DiscardStderr:$DiscardStderr
    }
    $callerMessages = @{
        version='Pinned MediaMTX executable failed its version probe.'
        config='Local synthetic RTSP MediaMTX configuration is invalid.'
        public='Public RTSP alpha source failed validation.'
    }
    $fixturePhase = 'probe_invoke'
    foreach ($case in $cases) {
        [Console]::Out.WriteLine('K5_ELEMENT_CHECKPOINT=shared_' + $case.mode + '_requested')
        [Console]::Out.Flush()
        $script:fixtureActualExit = $null; $script:fixtureProcessCleaned = $false
        $accepted = $true; $known = $false
        try {
            if ($Boundary -ceq 'argv') {
                $arguments = @('-I','-B','-S',$Fixture,$case.mode) + $argv
                if ($case.mode -ceq 'argv_nul') { $arguments[-1] = [string][char]0 }
                elseif ($case.mode -ceq 'argv_none') { $arguments = @() }
                elseif ($case.mode -ceq 'argv_count') { $arguments = @('x') * 17 }
                elseif ($case.mode -ceq 'argv_single') { $arguments = @('x' * 8193) }
                elseif ($case.mode -ceq 'argv_total') { $arguments = @('x' * 6000) * 3 }
                $result = Invoke-K5NativeProbeActual -Executable $Python `
                    -Arguments $arguments -CaptureOutput
                if ($result.ExitCode -ne 0 -or $result.Stdout -cne "argv-ok`n") {
                    throw 'fixture_argv'
                }
            } else { Invoke-K5CallerOnly }
        } catch {
            $accepted = $false
            if ($case.error -ceq 'caller' -and $callerMessages.ContainsKey($Boundary) -and
                $_.Exception.Message -ceq $callerMessages[$Boundary]) { $known = $true }
            $exception = $_.Exception
            for ($depth=0; $depth -lt 4 -and $null -ne $exception; $depth++) {
                if ($case.error -ceq 'stderr' -and $exception -is [IO.InvalidDataException] -and
                    $exception.Message -ceq 'K5 native stderr refused.') { $known = $true }
                if ($case.error -ceq 'decode' -and $exception -is [Text.DecoderFallbackException]) {
                    $known = $true
                }
                if ($case.error -ceq 'argument' -and $exception -is [ArgumentException] -and
                    $exception.Message -ceq 'K5 native arguments invalid.') { $known = $true }
                $exception = $exception.InnerException
            }
            if (-not $known) { throw }
        }
        $ambient = Get-Variable LASTEXITCODE -Scope Global -ErrorAction SilentlyContinue
        $ambientValue = $null
        if ($null -ne $ambient) { $ambientValue = $ambient.Value }
        $expectedAmbient = if ($Initial -ceq 'absent') { $null }
            elseif ($Initial -ceq 'stale_zero') { 0 } else { 9 }
        if ($accepted -ne $case.accept -or $script:fixtureActualExit -ne $case.code -or
            -not $script:fixtureProcessCleaned -or $ambientValue -ne $expectedAmbient -or
            $MediaMtxVersion -cne '1.21.1') { throw 'fixture_result' }
        [Console]::Out.WriteLine('K5_ELEMENT_CHECKPOINT=shared_' + $case.mode + '_passed')
        [Console]::Out.Flush()
    }
    exit 0
} catch { Write-K5ElementChildFailure $_ $fixturePhase 'primary'; exit 1 }
"""


def _shared_native_fixture(boundary, config_path):
    resolve = re.findall(r'\$resolveCode = "([^"]+)"', START.read_text())
    if len(resolve) != 1:
        raise ValueError("Invalid source resolver selection")
    expected = {
        "version": ["--version"],
        "config": ["--validate-conf", str(config_path)],
        "public": ["-I", "-B", "-c", resolve[0], "rtsp://example.invalid:8554/owned%20path"],
        "argv": SHARED_ARGV,
    }
    python = (
        "import os,sys\nEXPECTED = "
        + repr(expected)
        + r"""
try:
    if not (sys.flags.isolated and sys.flags.no_site and sys.flags.dont_write_bytecode):
        os._exit(42)
    mode = sys.argv[1]
    boundary = mode.split('_')[0]
    if sys.argv[2:] != EXPECTED[boundary]:
        os._exit(41)
    if boundary == 'version':
        output = {'version_wrong':b'v1.21.10\n','version_multi':b'v1.21.1\nextra\n',
                  'version_empty':b'','version_utf8':b'\xff'}.get(mode,b'v1.21.1\n')
    elif boundary == 'config':
        output = b'\xff' if mode == 'config_bytes' else (
            'configuration file: ' + EXPECTED['config'][1] + '\nconfiguration file is valid\n'
        ).encode()
    elif boundary == 'public':
        output = {'public_empty':b'','public_multi':b'8.8.8.8\n1.1.1.1\n',
                  'public_cr':b'8.8.8.8\r','public_crlf':b'8.8.8.8\r\n',
                  'public_cr_multi':b'8.8.8.8\r1.1.1.1',
                  'public_blank':b'8.8.8.8\n\n','public_cr_blank':b'8.8.8.8\r\r',
                  'public_utf8':b'\xff'}.get(mode,b'8.8.8.8\n')
    else:
        output = b'argv-ok\n'
    if output:
        os.write(1,output)
    if mode.endswith('_stderr'):
        os.write(2,b'PRIVATE_CAPTURED_STDERR')
    os._exit(7 if mode.endswith('_nonzero') else 0)
except BaseException:
    os._exit(43)
"""
    )
    cases = [
        dict(mode=m, accept=a, code=c, error=e) for m, a, c, e in SHARED_NATIVE_CASES[boundary]
    ]
    insertions = []
    for anchor, addition in ELEMENT_PROCESS_INSERTIONS.items():
        before = anchor.rstrip("\n").replace("'", "''")
        after = addition.rstrip("\n").replace("'", "''")
        insertions.append(
            "$anchor='" + before + '\' + "`n"\n'
            "if (($helper.Split(@($anchor),[StringSplitOptions]::None)).Count -ne 2) "
            "{ throw 'fixture_identity' }\n"
            "$helper=$helper.Replace($anchor,$anchor + '" + after + '\' + "`n")'
        )
    script = (
        SHARED_NATIVE_SCRIPT.replace("__ELEMENT_CHILD_DIAGNOSTICS__", ELEMENT_CHILD_DIAGNOSTICS)
        .replace("__ELEMENT_UTILITY_IMPORT__", ELEMENT_UTILITY_IMPORT)
        .replace("__SHARED_OBSERVATIONS__", "\n".join(insertions))
        .replace("__RESOLVE_CODE__", resolve[0].replace("'", "''"))
        .replace("__ARGV__", json.dumps(SHARED_ARGV).replace("'", "''"))
        .replace("__CASES__", json.dumps(cases).replace("'", "''"))
    )
    return script, python


@pytest.mark.skipif(
    sys.platform != "win32", reason="Requires actual Windows native caller boundaries"
)
@pytest.mark.parametrize("boundary", sorted(SHARED_NATIVE_CASES))
@pytest.mark.parametrize("initial", ["absent", "stale_zero", "stale_nonzero"])
def test_windows_shared_native_caller_boundaries_and_exact_argv(tmp_path, boundary, initial):
    import os

    common = None
    context = _element_context("probe", "ConsoleApplication", "zero", "process", initial)
    try:
        module = _startup_witness()
        common = module.common
        base = common.local_path(Path(sys._base_executable))
        binding = dict(
            K5_WITNESS_BASE_PYTHON=str(base), K5_WITNESS_BASE_PYTHON_SHA256=common.file_hash(base)
        )
        supplied = {key: os.environ.get(key) for key in common.GATE_RUNTIME_KEYS}
        if any(value is not None for value in supplied.values()) and supplied != binding:
            raise ValueError("Invalid admitted runtime")
        env = module.clean_environment(dict(os.environ), tmp_path)
        env.update(binding)
        for key in ("TEMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA"):
            Path(env[key]).mkdir(parents=True, exist_ok=True)
        shell = common.local_path(
            Path(env["SYSTEMROOT"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        )
        config_path = tmp_path / "owned config with spaces.yml"
        script, python = _shared_native_fixture(boundary, config_path)
        target = tmp_path / "shared-probe.ps1"
        target.write_text(
            _element_bind_utility(script, shell, common), encoding="ascii", newline="\n"
        )
        fixture = tmp_path / "owned script with spaces.py"
        fixture.write_text(python, encoding="utf-8", newline="\n")
        output = _capture_element_child(
            common,
            [
                str(shell),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-File",
                str(target),
                "-Start",
                str(START),
                "-StartHash",
                common.file_hash(START),
                "-Python",
                str(base),
                "-Fixture",
                str(fixture),
                "-ConfigPath",
                str(config_path),
                "-Boundary",
                boundary,
                "-Initial",
                initial,
            ],
            cwd=tmp_path,
            env=env,
            context=context,
        )
        lines = output.splitlines()
        expected = [
            ELEMENT_CHECKPOINT_PREFIX + ("shared_" + mode + "_" + state).encode()
            for mode, *_ in SHARED_NATIVE_CASES[boundary]
            for state in ("requested", "passed")
        ]
        prefix = [
            ELEMENT_CHECKPOINT_PREFIX + name.encode()
            for name in (
                "utility_manifest_requested",
                "utility_import_requested",
                "utility_import_returned",
            )
        ]
        if (
            lines[:3] != prefix
            or len(lines) != 5 + len(expected)
            or lines[4] != ELEMENT_CHECKPOINT_PREFIX + b"utility_binding_verified"
            or lines[5:] != expected
        ):
            raise ValueError("Incomplete native caller evidence")
        _element_require_utility_observation(lines[3])
    except Exception as error:
        try:
            _element_diagnostic(context, "failed", error, common=common)
        except Exception:
            pass
        pytest.fail("Owned shared native caller fixture failed", pytrace=False)


def test_shared_probe_preserves_qualified_loop_and_owned_cleanup():
    source = ELEMENT_PRODUCT_HELPER
    start = source.index("        $watch = [Diagnostics.Stopwatch]::StartNew()")
    end = source.index("        $exitCode = $child.ExitCode", start)
    loop = source[start:end].replace(
        " " * 20 + "if ($CaptureOutput) { $capturedStdout.Write($outBuffer, 0, $count) }\n",
        "",
    )
    assert hashlib.sha256(loop.encode()).hexdigest() == (
        "2519b1d0dd287a4d778f3c846924b9a15284c22dc06c9635d8c3730349d17ec7"
    )
    start = source.index("    } catch {\n        $primaryFailure = $_")
    end = source.index("        # The exact started Process", start)
    cleanup = source[start:end].replace(
        "@($stdoutStream, $stderrStream, $capturedStdout)", "@($stdoutStream, $stderrStream)"
    )
    assert hashlib.sha256(cleanup.encode()).hexdigest() == (
        "a936149e29a901ed1ab6f2de7758a85523c6de2f296ae6e533bdb68904ac3445"
    )
    assert "$capturedStdout = $null" in _element_process_cleanup_fixture()


def test_shared_native_fixture_is_bounded_source_bound_and_policy_specific():
    script, _ = _shared_native_fixture("version", Path("owned config.yml"))
    assert "ReadAllBytes" not in script and "[byte[]]::new(262145)" in script
    assert "$hasher.ComputeHash($bytes,0,$total)" in script
    assert "$stream.Dispose()" in script and "$total -gt 262144" in script
    assert "$_.Exception.Message -ceq $callerMessages[$Boundary]" in script
    assert "$assignments.Count -ne 1" in script and "$helpers.Count -ne 1" in script
    assert "$statements[$index].Extent.Text" in script
    assert "$script:fixtureActualExit -ne $case.code" in script
    assert "-not $script:fixtureProcessCleaned" in script
    assert "$ambientValue -ne $expectedAmbient" in script
    assert "[bool]$DiscardStderr -ne ($Boundary -ceq 'public')" in script
    assert "[bool]$CaptureOutput -ne ($Boundary -cne 'config')" in script
    assert "& $Start" not in script
    assert len(script.encode()) < 32768
    for cases in SHARED_NATIVE_CASES.values():
        for mode, *_ in cases:
            for state in ("requested", "passed"):
                assert "shared_" + mode + "_" + state in ELEMENT_CHECKPOINTS
    source = ELEMENT_PRODUCT_HELPER
    assert source.index("$argument.IndexOf([char]0)") < source.index("$child.Start()")
    for bound in (
        "$Arguments.Count -gt 16",
        "$argument.Length -gt 8192",
        "$info.Arguments.Length -gt 16384",
    ):
        assert source.index(bound) < source.index("$child.Start()")


@pytest.mark.parametrize("boundary", sorted(SHARED_NATIVE_CASES))
def test_shared_native_python_oracle_checks_exact_vectors_and_fixed_output(tmp_path, boundary):
    import ast

    _, python = _shared_native_fixture(boundary, tmp_path / "owned config 'with spaces'.yml")
    tree = ast.parse(python)
    expected = ast.literal_eval(
        next(node.value for node in tree.body if isinstance(node, ast.Assign))
    )
    target = tmp_path / "owned oracle.py"
    target.write_text(python, encoding="utf-8", newline="\n")
    for mode, _, code, _ in SHARED_NATIVE_CASES[boundary]:
        if code is None:
            continue  # These are pre-launch argument refusals in the real Windows helper.
        command = [sys.executable, "-I", "-B", "-S", str(target), mode, *expected[boundary]]
        result = subprocess.run(command, capture_output=True, timeout=5, check=False)
        assert result.returncode == code
        assert len(result.stdout) < 4096
        assert result.stderr == (b"PRIVATE_CAPTURED_STDERR" if mode.endswith("_stderr") else b"")
        if mode.endswith("_utf8") or mode == "config_bytes":
            assert result.stdout == b"\xff"
        elif mode == "argv_ok":
            assert result.stdout == b"argv-ok\n"
        elif mode in {
            "public_cr",
            "public_crlf",
            "public_cr_multi",
            "public_blank",
            "public_cr_blank",
        }:
            assert (
                result.stdout
                == {
                    "public_cr": b"8.8.8.8\r",
                    "public_crlf": b"8.8.8.8\r\n",
                    "public_cr_multi": b"8.8.8.8\r1.1.1.1",
                    "public_blank": b"8.8.8.8\n\n",
                    "public_cr_blank": b"8.8.8.8\r\r",
                }[mode]
            )
        elif mode.endswith("_ok") or mode.endswith("_nonzero") or mode.endswith("_stderr"):
            assert result.stdout.endswith(b"\n")
        wrong = subprocess.run(
            command + ["unexpected"], capture_output=True, timeout=5, check=False
        )
        assert wrong.returncode == 41 and not wrong.stdout and not wrong.stderr


@pytest.mark.parametrize("boundary", sorted(SHARED_NATIVE_CASES))
def test_shared_native_python_oracle_uses_utf8_under_cp1252_default(
    tmp_path, monkeypatch, boundary
):
    original_write_text = Path.write_text
    writes = []

    def write_with_legacy_default(path, data, encoding=None, errors=None, newline=None):
        writes.append((encoding, newline))
        return original_write_text(
            path,
            data,
            encoding="cp1252" if encoding is None else encoding,
            errors=errors,
            newline=newline,
        )

    monkeypatch.setattr(Path, "write_text", write_with_legacy_default)
    test_shared_native_python_oracle_checks_exact_vectors_and_fixed_output(tmp_path, boundary)
    assert writes == [("utf-8", "\n")]
    raw = (tmp_path / "owned oracle.py").read_bytes()
    assert "Ω".encode() in raw and b"\r" not in raw


def test_shared_probe_operation_anchors_preserve_specific_callers():
    module = _startup_witness()
    source = START.read_text().splitlines()
    positioned = []
    for anchor, operation in module.START_OPERATION_ANCHORS:
        hits = [index + 1 for index, line in enumerate(source) if line.strip() == anchor]
        assert len(hits) == 1
        positioned.append((hits[0], operation))
    assert positioned == sorted(positioned)
    for needle, expected in (
        ("$argument.IndexOf([char]0)", "unknown"),
        ("$mediaMtxVersionOutput.Stdout -cnotin", "mediamtx_version"),
        ("if ($validation.ExitCode -ne 0)", "mediamtx_config_validate"),
    ):
        line = next(index + 1 for index, text in enumerate(source) if needle in text)
        assert [operation for at, operation in positioned if at <= line][-1] == expected


@pytest.mark.skipif(
    sys.platform != "win32", reason="Requires Windows source-bound error projection"
)
@pytest.mark.parametrize("boundary", ["helper", "version", "config"])
def test_windows_shared_probe_errors_keep_truthful_source_operation(tmp_path, boundary):
    module = _startup_witness()
    valid = False
    try:
        source = START.read_text()
        functions = list(re.finditer(r"(?ms)^function [^\r\n]+\{\r?\n.*?^\}", source))
        if not functions:
            raise ValueError("Missing source functions")
        # Define the actual functions in place, but never execute the launcher's
        # top-level admission/session/media blocks in this source-line fixture.
        parts = [source[: functions[0].start()]]
        cursor = functions[0].start()
        for function in functions:
            parts += [
                "\nif ($false) {\n",
                source[cursor : function.start()],
                "\n}\n",
                function.group(),
            ]
            cursor = function.end()
        parts += ["\nif ($false) {\n", source[cursor:], "\n}\n"]
        if boundary == "helper":
            parts.append("Invoke-K5NativeProbe -Executable 'relative' -Arguments @('owned')\n")
        else:
            parts.append(
                r"""
$sessionRoot = $PSScriptRoot
function Test-K5GStreamerElement { return $true }
function Get-K5MediaMtx { return 'owned-test-double' }
function Invoke-K5NativeProbe { param($Executable,[string[]]$Arguments,
    [switch]$CaptureOutput,[switch]$DiscardStderr)
    if ($Arguments[0] -ceq '--version') {
        return [pscustomobject]@{ExitCode=0;Stdout='__VERSION__'}
    }
    return [pscustomobject]@{ExitCode=7;Stdout=''}
}
$null = Start-K5SyntheticSource
""".replace("__VERSION__", "wrong" if boundary == "version" else "v1.21.1")
            )
        fixture = "".join(parts)
        target = tmp_path / "source-bound-fixture.ps1"
        target.write_text(fixture, encoding="ascii", newline="\n")
        envelope = tmp_path / "envelope.ps1"
        envelope.write_text(
            module.bind_start_envelope(hashlib.sha256(fixture.encode()).hexdigest()),
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
        if result.returncode != 24 or result.stderr or len(result.stdout) > 2048:
            raise ValueError("Invalid bounded projection")
        records = [
            module.parse_start_error(line[len(module.START_ERROR_PREFIX) :])
            for line in result.stdout.splitlines()
            if line.startswith(module.START_ERROR_PREFIX)
        ]
        expected = {
            "helper": "unknown",
            "version": "mediamtx_version",
            "config": "mediamtx_config_validate",
        }
        if (
            len(records) != 1
            or records[0]["origin"] != "start"
            or records[0]["operation"] != expected[boundary]
        ):
            raise ValueError("Invalid source operation")
        valid = True
    except Exception:
        pass
    if not valid:
        pytest.fail("Bounded shared probe projection failed", pytrace=False)


def test_public_native_line_parser_matches_cr_lf_crlf_and_one_terminal_delimiter():
    source = START.read_text()
    pattern = re.search(r'\$resolvedLines = @\(\$resolved.Stdout -split "([^"]+)"\)', source).group(
        1
    )
    assert pattern == r"\r\n|\r|\n"
    assert '$resolved.Stdout.EndsWith("`r") -or $resolved.Stdout.EndsWith("`n")' in source
    for text, accepted in (
        ("8.8.8.8", True),
        ("8.8.8.8\n", True),
        ("8.8.8.8\r", True),
        ("8.8.8.8\r\n", True),
        ("8.8.8.8\r1.1.1.1", False),
        ("8.8.8.8\n1.1.1.1", False),
        ("8.8.8.8\n\n", False),
        ("8.8.8.8\r\r", False),
        ("8.8.8.8\r\n\r\n", False),
        ("", False),
        (" \r", False),
    ):
        lines = re.split(pattern, text)
        if text.endswith(("\r", "\n")):
            lines = lines[:-1]
        assert (len(lines) == 1 and bool(lines[0].strip())) is accepted


RUN_FACADE_PREFIX = b"K5_RUN_FACADE_RESULT="
RUN_FACADE_CASES = {
    "success": "returned",
    "child_throw": "child_failure",
    "cleanup_throw": "cleanup_failure",
    "child_exit_nonzero": "wrapper_failure",
}
RUN_FACADE_INITIALS = {"absent": None, "stale_zero": 0, "stale_nonzero": 9, "stale_seven": 7}
RUN_FACADE_SCENARIOS = [
    (initial, case)
    for case in sorted(RUN_FACADE_CASES)
    for initial in ("absent", "stale_nonzero", "stale_zero")
] + [("stale_seven", case) for case in ("success", "child_exit_nonzero")]
# Exact comparison from b3ddecf414ed25ed6f6a36048771122c3505bb5c Run lines 29-32.
# This is a policy projection, not a claim that the historical full Run completed.
RUN_FACADE_HISTORICAL_COMPARISON = (
    "    $nativeExitChanged = -not $nativeExitBeforePresent -or "
    "$nativeExitAfterValue -ne $nativeExitBeforeValue\n"
    "    if ($nativeExitChanged -and $nativeExitAfterValue -ne 0) {\n"
    '        throw "K5 Vision Alpha launcher failed."\n'
    "    }"
)
RUN_FACADE_CHECKPOINTS = (
    "facade_source_requested",
    "facade_source_selected",
    "facade_invoke_requested",
    "facade_invoke_returned",
)
RUN_FACADE_MANAGEMENT_CHECKPOINTS = (
    "management_manifest_requested",
    "management_import_requested",
    "management_import_returned",
    "management_binding_verified",
)
RUN_FACADE_MANAGEMENT_PREFIX = b"K5_ELEMENT_MANAGEMENT_BINDING="
# Reuse the qualified, bounded manifest import/metadata observer without changing
# it for existing probes. Only the exact PSHOME module and exported command differ.
RUN_FACADE_MANAGEMENT_IMPORT = (
    ELEMENT_UTILITY_IMPORT.replace("UTILITY", "MANAGEMENT")
    .replace("Utility", "Management")
    .replace("utility", "management")
    .replace("Write-Output", "Join-Path")
    .replace("WriteOutputCommand", "JoinPathCommand")
    .replace("-Name $managementManifest -PassThru", "-Name $managementManifest -Global -PassThru")
    # Keep the existing child-error phase vocabulary; checkpoints identify the module.
    .replace("$fixturePhase = 'management_", "$fixturePhase = 'utility_")
)
RUN_FACADE_MANAGEMENT_TEST_PATH = r"""
$managementTestPath = $managementModule.ExportedCmdlets['Test-Path']
if ($managementTestPath -isnot [Management.Automation.CmdletInfo] -or
    $managementTestPath.ImplementingType.FullName -cne
        'Microsoft.PowerShell.Commands.TestPathCommand') { throw 'fixture_identity' }
$managementTestAssembly = $managementTestPath.ImplementingType.Assembly.GetName()
$managementTestToken = $managementTestAssembly.GetPublicKeyToken()
if ($managementTestAssembly.Name -cne 'Microsoft.PowerShell.Commands.Management' -or
    $managementTestToken.Length -ne 8 -or $coreToken.Length -ne 8 -or
    [BitConverter]::ToString($managementTestToken) -cne [BitConverter]::ToString($coreToken)) {
    throw 'fixture_identity'
}
"""
_management_verified = (
    "[Console]::Out.WriteLine('K5_ELEMENT_CHECKPOINT=management_binding_verified')"
)
if RUN_FACADE_MANAGEMENT_IMPORT.count(_management_verified) != 1:
    raise ValueError("Invalid scoped Management verification anchor")
RUN_FACADE_MANAGEMENT_IMPORT = RUN_FACADE_MANAGEMENT_IMPORT.replace(
    _management_verified, RUN_FACADE_MANAGEMENT_TEST_PATH + _management_verified, 1
)
ELEMENT_CHECKPOINTS.update((*RUN_FACADE_MANAGEMENT_CHECKPOINTS, *RUN_FACADE_CHECKPOINTS))
RUN_FACADE_SCRIPT = (
    r"""
param([string]$Run, [string]$RunHash, [string]$StartSource, [string]$StartHash,
      [string]$InstallRoot, [string]$Initial, [string]$Case, [string]$SourceKind)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
__ELEMENT_CHILD_DIAGNOSTICS__
function Read-K5FacadeSource([string]$Path, [string]$Expected, [int]$Maximum) {
    $stream = [IO.File]::OpenRead($Path)
    $bytes = [byte[]]::new($Maximum + 1)
    $total = 0
    try {
        while ($total -lt $bytes.Length) {
            $count = $stream.Read($bytes, $total, $bytes.Length - $total)
            if ($count -eq 0) { break }
            $total += $count
        }
    } finally { $stream.Dispose() }
    if ($total -gt $Maximum) { throw 'fixture_identity' }
    $hasher = [Security.Cryptography.SHA256]::Create()
    try {
        $actual = [BitConverter]::ToString(
            $hasher.ComputeHash($bytes, 0, $total)).Replace('-', '').ToLowerInvariant()
        if ($actual -cne $Expected) { throw 'fixture_identity' }
    } finally { $hasher.Dispose() }
    return [Text.UTF8Encoding]::new($false, $true).GetString($bytes, 0, $total)
}
$fixturePhase = 'utility_manifest'
try {
__ELEMENT_UTILITY_IMPORT__
__FACADE_MANAGEMENT_IMPORT__
    $fixturePhase = 'source_select'
    [Console]::Out.WriteLine('K5_ELEMENT_CHECKPOINT=facade_source_requested')
    [Console]::Out.Flush()
    if ($Initial -cnotin @('absent','stale_zero','stale_nonzero','stale_seven') -or
        $Case -cnotin @('success','child_throw','cleanup_throw','child_exit_nonzero') -or
        $SourceKind -cnotin @('synthetic','public')) { throw 'fixture_arguments' }
    # Execute only the immutable historical comparison, with fixed scalar inputs.
    # The changed-code control must reject; the equal-code collision must not.
    # No automatic variable, child result, or actual Run source is altered here.
    $historicalControls = 0
    foreach ($comparison in @(@(0,7,$true), @(7,7,$false), @(7,0,$false))) {
        $nativeExitBeforePresent = $true
        $nativeExitBeforeValue = $comparison[0]
        $nativeExitAfterValue = $comparison[1]
        $historicalRejected = $false
        try {
__HISTORICAL_COMPARISON__
        } catch {
            if ($_.Exception.Message -cne 'K5 Vision Alpha launcher failed.') { throw }
            $historicalRejected = $true
        }
        if ($historicalRejected -ne $comparison[2]) { throw 'fixture_identity' }
        $historicalControls += 1
    }
    $runSource = Read-K5FacadeSource $Run $RunHash 16384
    $source = Read-K5FacadeSource $StartSource $StartHash 65536
    $tokens = $null; $errors = $null
    $ast = [Management.Automation.Language.Parser]::ParseInput(
        $source, [ref]$tokens, [ref]$errors)
    if ($errors.Count -ne 0 -or $null -eq $ast.ParamBlock) { throw 'fixture_identity' }
    $topTry = @()
    foreach ($statement in $ast.EndBlock.Statements) {
        if ($statement -is [Management.Automation.Language.TryStatementAst]) {
            $topTry += ,$statement
        }
    }
    if ($topTry.Count -ne 1) { throw 'fixture_identity' }
    $success = @()
    foreach ($statement in $topTry[0].Body.Statements) {
        if ($statement -is [Management.Automation.Language.IfStatementAst] -and
            $statement.Extent.Text.StartsWith('if ($ExitAfterPublicTest) {')) {
            $success += ,$statement
        }
    }
    if ($success.Count -ne 1) { throw 'fixture_identity' }
    if ($success[0].Clauses.Count -ne 1 -or $null -ne $success[0].ElseClause -or
        $success[0].Clauses[0].Item1.Extent.Text -cne '$ExitAfterPublicTest') {
        throw 'fixture_identity'
    }
    $statements = @($success[0].Clauses[0].Item2.Statements)
    if ($statements.Count -ne 2 -or
        $statements[0] -isnot [Management.Automation.Language.PipelineAst] -or
        $statements[1] -isnot [Management.Automation.Language.ReturnStatementAst] -or
        $statements[1].Extent.Text -cne 'return') { throw 'fixture_identity' }
    $commands = @($statements[0].PipelineElements)
    if ($commands.Count -ne 1 -or
        $commands[0] -isnot [Management.Automation.Language.CommandAst] -or
        $commands[0].GetCommandName() -cne 'Write-Host' -or
        $commands[0].CommandElements.Count -ne 2 -or
        $commands[0].CommandElements[1] -isnot
            [Management.Automation.Language.StringConstantExpressionAst] -or
        $commands[0].CommandElements[1].Value -cne
            'Exiting after one bounded alpha acceptance run.') { throw 'fixture_identity' }
    # Only the actual product parameter block and final success-return statement
    # are used. This is a nonmedia facade fixture, never a full Start execution.
    $body = @'
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
try {
    $global:K5FacadeEntered += 1
    if ($Port -ne 8017 -or -not $ExitAfterPublicTest -or $AnalyticsPreflightOnly -or
        $PublicRtspSource -cne $global:K5FacadeExpectedSource -or
        -not $PSBoundParameters.ContainsKey('Port') -or
        -not $PSBoundParameters.ContainsKey('ExitAfterPublicTest') -or
        $PSBoundParameters.ContainsKey('AnalyticsPreflightOnly') -or
        $PSBoundParameters.ContainsKey('PublicRtspSource') -ne
            $global:K5FacadePublicBound -or
        $PSBoundParameters.Count -ne (2 + [int]$global:K5FacadePublicBound)) {
        throw 'fixture_arguments'
    }
    $global:K5FacadeArgumentsValid = $true
    if ($global:K5FacadeCase -ceq 'child_throw') { throw 'K5 facade fixture child failure.' }
    # Synthetic compatibility control; the current product Start has no exit statement.
    if ($global:K5FacadeCase -ceq 'child_exit_nonzero') { exit 7 }
__PRODUCT_SUCCESS_RETURN__
    throw 'fixture_return_missing'
} finally {
    $global:K5FacadeCleanup += 1
    if ($global:K5FacadeCase -ceq 'cleanup_throw') { throw 'K5 facade fixture cleanup failure.' }
}
'@
    $fixture = $ast.ParamBlock.Extent.Text + [char]10 +
        $body.Replace('__PRODUCT_SUCCESS_RETURN__', $success[0].Extent.Text)
    [IO.File]::WriteAllText([IO.Path]::Combine($InstallRoot, 'Start-K5VisionAlpha.ps1'),
        $fixture, [Text.UTF8Encoding]::new($false))
    [Console]::Out.WriteLine('K5_ELEMENT_CHECKPOINT=facade_source_selected')
    [Console]::Out.Flush()
    $fixturePhase = 'command_admission'
    $global:K5FacadeCase = $Case
    $global:K5FacadeEntered = 0
    $global:K5FacadeCleanup = 0
    $global:K5FacadeArgumentsValid = $false
    $global:K5FacadeExpectedSource = if ($SourceKind -ceq 'public') {
        'rtsp://example.invalid:8554/owned%20path'
    } else { '' }
    $global:K5FacadePublicBound = $SourceKind -ceq 'public'
    if ($null -ne (Get-Variable LASTEXITCODE -Scope Global -ErrorAction SilentlyContinue)) {
        throw 'fixture_not_fresh'
    }
    if ($Initial -ceq 'stale_zero') { $global:LASTEXITCODE = 0 }
    elseif ($Initial -ceq 'stale_nonzero') { $global:LASTEXITCODE = 9 }
    elseif ($Initial -ceq 'stale_seven') { $global:LASTEXITCODE = 7 }
    $outcome = 'returned'
    $fixturePhase = 'probe_invoke'
    [Console]::Out.WriteLine('K5_ELEMENT_CHECKPOINT=facade_invoke_requested')
    [Console]::Out.Flush()
    try {
        & $Run -InstallRoot $InstallRoot -Port 8017 -ExitAfterPublicTest `
            -PublicRtspSource $global:K5FacadeExpectedSource
    } catch {
        if ($_.FullyQualifiedErrorId.Split(',')[0] -ceq 'VariableIsUndefined' -and
            [IO.Path]::GetFullPath($_.InvocationInfo.ScriptName) -ceq
                [IO.Path]::GetFullPath($Run) -and
            $_.InvocationInfo.Line.Contains('$LASTEXITCODE') -and
            $runSource.Contains($_.InvocationInfo.Line.Trim())) {
            $outcome = 'variable_undefined'
        } elseif ($_.Exception.Message -ceq 'K5 Vision Alpha launcher failed.') {
            $outcome = 'wrapper_failure'
        } elseif ($_.Exception.Message -ceq 'K5 facade fixture child failure.') {
            $outcome = 'child_failure'
        } elseif ($_.Exception.Message -ceq 'K5 facade fixture cleanup failure.') {
            $outcome = 'cleanup_failure'
        } else { $outcome = 'unexpected_error' }
    }
    [Console]::Out.WriteLine('K5_ELEMENT_CHECKPOINT=facade_invoke_returned')
    [Console]::Out.Flush()
    $fixturePhase = 'probe_record'
    $null = Read-K5FacadeSource $Run $RunHash 16384
    $ambient = Get-Variable LASTEXITCODE -Scope Global -ErrorAction SilentlyContinue
    $ambientJson = 'null'
    if ($null -ne $ambient) {
        if ($ambient.Value -isnot [int] -or $ambient.Value -notin @(0,7,9)) {
            throw 'fixture_ambient'
        }
        $ambientJson = [string]$ambient.Value
    }
    $json = '{"schema_version":"run-facade-return-v1","initial":"' + $Initial +
        '","case":"' + $Case + '","source_kind":"' + $SourceKind +
        '","outcome":"' + $outcome + '","entered":' +
        [string]$global:K5FacadeEntered + ',"cleanup":' +
        [string]$global:K5FacadeCleanup + ',"arguments_valid":' +
        $global:K5FacadeArgumentsValid.ToString().ToLowerInvariant() +
        ',"historical_collision":' + ($historicalControls -eq 3).ToString().ToLowerInvariant() +
        ',"ambient_after":' + $ambientJson + '}'
    [Console]::Out.WriteLine('K5_RUN_FACADE_RESULT=' + $json)
    [Console]::Out.Flush()
    exit 0
} catch {
    Write-K5ElementChildFailure $_ $fixturePhase 'primary'
    exit 1
}
""".replace("__ELEMENT_CHILD_DIAGNOSTICS__", ELEMENT_CHILD_DIAGNOSTICS)
    .replace("__ELEMENT_UTILITY_IMPORT__", ELEMENT_UTILITY_IMPORT)
    .replace("__HISTORICAL_COMPARISON__", RUN_FACADE_HISTORICAL_COMPARISON)
    .replace("__FACADE_MANAGEMENT_IMPORT__", RUN_FACADE_MANAGEMENT_IMPORT)
)


def _run_facade_bind_modules(script, powershell, common):
    script = _element_bind_utility(script, powershell, common)
    home = common.local_path(powershell).parent
    manifest = common.local_path(
        home / "Modules/Microsoft.PowerShell.Management/Microsoft.PowerShell.Management.psd1"
    )
    with manifest.open("rb") as stream:
        raw = stream.read(65537)
    if not raw or len(raw) > 65536:
        raise ValueError("Invalid scoped Management manifest")
    for marker, value in (
        ("__MANAGEMENT_HOME__", str(home).replace("'", "''")),
        ("__MANAGEMENT_SHA256__", hashlib.sha256(raw).hexdigest()),
    ):
        if script.count(marker) != 1:
            raise ValueError("Invalid scoped Management template")
        script = script.replace(marker, value, 1)
    return script


def _run_facade_management_as_utility(line):
    if type(line) is not bytes or not line.startswith(RUN_FACADE_MANAGEMENT_PREFIX):
        raise ValueError("Invalid scoped Management observation")
    normalized = ELEMENT_UTILITY_BINDING_PREFIX + line[len(RUN_FACADE_MANAGEMENT_PREFIX) :].replace(
        b"management", b"utility"
    )
    restored = RUN_FACADE_MANAGEMENT_PREFIX + normalized[
        len(ELEMENT_UTILITY_BINDING_PREFIX) :
    ].replace(b"utility", b"management")
    if restored != line:
        raise ValueError("Invalid scoped Management schema")
    return normalized


def _run_facade_management_binding_records(raw):
    if len(raw) > 4096:
        raise ValueError("Invalid scoped Management observations")
    records = []
    for line in raw.splitlines():
        if not line.startswith(RUN_FACADE_MANAGEMENT_PREFIX):
            continue
        normalized = _run_facade_management_as_utility(line)
        for value in _element_utility_binding_records(normalized + b"\n"):
            records.append(
                {
                    key.replace("utility", "management"): item.replace("utility", "management")
                    if type(item) is str
                    else item
                    for key, item in value.items()
                }
            )
    if len(records) > 1:
        raise ValueError("Duplicate scoped Management observation")
    return records


def _run_facade_record(output):
    if type(output) is not bytes or len(output) > 2048:
        raise ValueError("Invalid fixed facade observation")
    records = []
    success_messages = 0
    for line in output.splitlines():
        if line.startswith(RUN_FACADE_PREFIX):
            pairs = json.loads(line[len(RUN_FACADE_PREFIX) :], object_pairs_hook=list)
            if type(pairs) is not list or any(type(pair) is not tuple for pair in pairs):
                raise ValueError("Invalid fixed facade observation")
            value = dict(pairs)
            if len(value) != len(pairs):
                raise ValueError("Invalid fixed facade observation")
            records.append(value)
        elif line == b"Exiting after one bounded alpha acceptance run.":
            success_messages += 1
        else:
            raise ValueError("Invalid fixed facade observation")
    if len(records) != 1 or success_messages > 1:
        raise ValueError("Invalid fixed facade observation")
    value = records[0]
    if value.keys() != {
        "schema_version",
        "initial",
        "case",
        "source_kind",
        "outcome",
        "entered",
        "cleanup",
        "arguments_valid",
        "historical_collision",
        "ambient_after",
    }:
        raise ValueError("Invalid fixed facade observation")
    enums = {
        "schema_version": {"run-facade-return-v1"},
        "initial": set(RUN_FACADE_INITIALS),
        "case": set(RUN_FACADE_CASES),
        "source_kind": {"synthetic", "public"},
        "outcome": set(RUN_FACADE_CASES.values()) | {"variable_undefined", "unexpected_error"},
    }
    if any(
        type(value[key]) is not str or value[key] not in options for key, options in enums.items()
    ):
        raise ValueError("Invalid fixed facade observation")
    if any(
        type(value[key]) is not bool for key in ("arguments_valid", "historical_collision")
    ) or any(
        type(value[key]) is not int or not 0 <= value[key] <= 2 for key in ("entered", "cleanup")
    ):
        raise ValueError("Invalid fixed facade observation")
    if value["ambient_after"] is not None and (
        type(value["ambient_after"]) is not int or value["ambient_after"] not in (0, 7, 9)
    ):
        raise ValueError("Invalid fixed facade observation")
    return value


def _run_facade_child_record(output):
    complete, checkpoints, arguments = _element_complete_records(output)
    expected = [
        "utility_manifest_requested",
        "utility_import_requested",
        "utility_import_returned",
        "utility_binding_verified",
        *RUN_FACADE_MANAGEMENT_CHECKPOINTS,
        *RUN_FACADE_CHECKPOINTS,
    ]
    if complete != output or checkpoints != expected or arguments:
        raise ValueError("Incomplete fixed facade observations")
    lines = output.splitlines()
    if (
        len(lines) not in (15, 16)
        or lines[:3] != [ELEMENT_CHECKPOINT_PREFIX + name.encode() for name in expected[:3]]
        or lines[4] != ELEMENT_CHECKPOINT_PREFIX + b"utility_binding_verified"
    ):
        raise ValueError("Invalid scoped Utility observation")
    _element_require_utility_observation(lines[3])
    if (
        lines[5:8]
        != [
            ELEMENT_CHECKPOINT_PREFIX + name.encode()
            for name in RUN_FACADE_MANAGEMENT_CHECKPOINTS[:3]
        ]
        or lines[9] != ELEMENT_CHECKPOINT_PREFIX + b"management_binding_verified"
    ):
        raise ValueError("Invalid scoped Management observation order")
    _element_require_utility_observation(_run_facade_management_as_utility(lines[8]))
    if lines[10:13] != [
        ELEMENT_CHECKPOINT_PREFIX + name.encode() for name in RUN_FACADE_CHECKPOINTS[:3]
    ]:
        raise ValueError("Invalid fixed facade stage order")
    tail = lines[13:]
    retained = []
    if tail[0] == b"Exiting after one bounded alpha acceptance run.":
        retained.append(tail.pop(0))
    if len(tail) != 2 or tail[0] != ELEMENT_CHECKPOINT_PREFIX + b"facade_invoke_returned":
        raise ValueError("Invalid fixed facade result order")
    return _run_facade_record(b"\n".join([*retained, tail[1]]) + b"\n")


def _run_facade_capture_classes(common):
    class StrictStderrSummary(common.StderrSummary):
        def __init__(self, stream, *, gated, nonce=""):
            if gated and re.fullmatch(r"[0-9a-f]{32}", nonce) is None:
                raise ValueError("Invalid facade relay identity")
            protocol = b""
            if gated:
                marker = "K5_GATE_" + nonce + "_"
                protocol = "".join(
                    "\n" + marker + value + "\n"
                    for value in ("STATE=opened", "STATE=started", "EXIT=0")
                ).encode("ascii")
            self.facade_child_stderr_empty = False
            self._facade_expected = (protocol, protocol.replace(b"\n", b"\r\n"))
            self._facade_offset = 0
            super().__init__(stream, gated=gated, nonce=nonce)

        def _read(self, stream):
            summary = self

            class ObservedStream:
                def read1(self, size):
                    block = stream.read1(size)
                    if block and summary._facade_expected:
                        offset = summary._facade_offset
                        summary._facade_expected = tuple(
                            expected
                            for expected in summary._facade_expected
                            if expected[offset : offset + len(block)] == block
                        )
                        if summary._facade_expected:
                            summary._facade_offset += len(block)
                    return block

                def close(self):
                    stream.close()

            super()._read(ObservedStream())
            self.facade_child_stderr_empty = not self.read_failed and any(
                self._facade_offset == len(expected) for expected in self._facade_expected
            )

    class StrictOwnedProcess(common.OwnedProcess):
        def wait(self, seconds):
            super().wait(seconds)
            if self.stderr_summary.facade_child_stderr_empty is not True:
                raise common.WitnessError("output_invalid")

    return StrictStderrSummary, StrictOwnedProcess


@pytest.mark.skipif(sys.platform != "win32", reason="Requires actual Windows Run facade")
@pytest.mark.parametrize("source_kind", ["synthetic", "public"])
@pytest.mark.parametrize("initial,case", RUN_FACADE_SCENARIOS)
def test_windows_run_facade_uses_script_result_not_ambient_native_status(
    tmp_path, source_kind, initial, case
):
    import os

    common = None
    observed = None
    context = _element_context("probe", initial=initial)
    try:
        module = _startup_witness()
        common = module.common
        common.StderrSummary, common.OwnedProcess = _run_facade_capture_classes(common)
        base = common.local_path(Path(sys._base_executable))
        binding = dict(
            K5_WITNESS_BASE_PYTHON=str(base),
            K5_WITNESS_BASE_PYTHON_SHA256=common.file_hash(base),
        )
        supplied = {key: os.environ.get(key) for key in common.GATE_RUNTIME_KEYS}
        if any(value is not None for value in supplied.values()) and supplied != binding:
            raise ValueError("Invalid admitted facade runtime")
        env = module.clean_environment(dict(os.environ), tmp_path)
        env.update(binding)
        for key in ("TEMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA"):
            Path(env[key]).mkdir(parents=True, exist_ok=True)
        shell = common.local_path(
            Path(env["SYSTEMROOT"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        )
        installed = tmp_path / "owned facade with spaces"
        installed.mkdir()
        run_bytes = (ALPHA / "Run-K5VisionAlpha.ps1").read_bytes()
        run = installed / "Run-K5VisionAlpha.ps1"
        run.write_bytes(run_bytes)
        harness = tmp_path / "run-facade.ps1"
        harness.write_text(
            _run_facade_bind_modules(RUN_FACADE_SCRIPT, shell, common),
            encoding="ascii",
            newline="\n",
        )
        output = _capture_element_child(
            common,
            [
                str(shell),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-File",
                str(harness),
                "-Run",
                str(run),
                "-RunHash",
                hashlib.sha256(run_bytes).hexdigest(),
                "-StartSource",
                str(START),
                "-StartHash",
                hashlib.sha256(START.read_bytes()).hexdigest(),
                "-InstallRoot",
                str(installed),
                "-Initial",
                initial,
                "-Case",
                case,
                "-SourceKind",
                source_kind,
            ],
            cwd=tmp_path,
            env=env,
            context=context,
        )
        observed = _run_facade_child_record(output)
    except _ElementCaptureFailure:
        pass  # The qualified capture has already emitted only fixed diagnostics.
    except Exception as error:
        try:
            _element_diagnostic(context, "failed", error, common=common)
        except Exception:
            pass
    if observed is None:
        # Outside the handler: raw subprocess/path exception context must not be retained.
        pytest.fail("Source-bound nonmedia Run facade fixture failed", pytrace=False)
    matches = (
        observed["initial"] == initial
        and observed["case"] == case
        and observed["source_kind"] == source_kind
        and observed["entered"] == observed["cleanup"] == 1
        and observed["arguments_valid"]
        and observed["historical_collision"]
        and (
            case == "child_exit_nonzero"
            or observed["ambient_after"] == RUN_FACADE_INITIALS[initial]
        )
        and observed["outcome"] == RUN_FACADE_CASES[case]
    )
    if not matches:
        print(RUN_FACADE_PREFIX.decode() + json.dumps(observed, separators=(",", ":")))
        pytest.fail("Run facade violated the script-result contract", pytrace=False)


def test_run_facade_fixture_is_source_bound_nonmedia_and_does_not_seed_success():
    source = RUN_FACADE_SCRIPT
    assert "$bytes = [byte[]]::new($Maximum + 1)" in source
    assert "$hasher.ComputeHash($bytes, 0, $total)" in source
    assert "$stream.Dispose()" in source and "$hasher.Dispose()" in source
    assert "$ast.ParamBlock.Extent.Text" in source
    assert "$success[0].Extent.Text" in source
    assert "$statements.Count -ne 2" in source
    assert "$topTry[0].Body.Statements" in source
    assert "$success[0].Clauses.Count -ne 1" in source
    assert "$commands[0].GetCommandName() -cne 'Write-Host'" in source
    assert "InvocationInfo.Line.Contains('$LASTEXITCODE')" in source
    assert "$global:LASTEXITCODE = 0" in source  # Explicit stale-input control only.
    assert "$Initial -ceq 'stale_zero'" in source
    assert "& $Run -InstallRoot $InstallRoot" in source
    assert "& $StartSource" not in source and "Start-Process" not in source
    assert "[Diagnostics.Process]" not in source and "Invoke-RestMethod" not in source
    assert source.count("Read-K5FacadeSource $Run $RunHash 16384") == 2
    assert len(source.encode("ascii")) < 32768
    assert ELEMENT_UTILITY_IMPORT in source
    assert ELEMENT_CHILD_DIAGNOSTICS in source
    assert source.index(ELEMENT_UTILITY_IMPORT) < source.index("Get-Variable LASTEXITCODE")
    start = START.read_text()
    assert start.count("if ($ExitAfterPublicTest) {") == 1
    assert "Exiting after one bounded alpha acceptance run." in start


def test_run_facade_record_rejects_unbounded_private_or_untyped_observations():
    value = dict(
        schema_version="run-facade-return-v1",
        initial="absent",
        case="success",
        source_kind="synthetic",
        outcome="returned",
        entered=1,
        cleanup=1,
        arguments_valid=True,
        historical_collision=True,
        ambient_after=None,
    )

    def encode(item):
        return RUN_FACADE_PREFIX + json.dumps(item).encode() + b"\n"

    assert _run_facade_record(encode(value)) == value
    for invalid in (
        b"private path\n" + encode(value),
        encode(value) * 2,
        b"x" * 2049,
        encode({**value, "extra": "private"}),
        encode({**value, "entered": True}),
        encode({**value, "ambient_after": True}),
        encode({**value, "outcome": "private"}),
        RUN_FACADE_PREFIX
        + b'{"schema_version":"run-facade-return-v1","schema_version":"duplicate"}',
    ):
        with pytest.raises(ValueError):
            _run_facade_record(invalid)


@pytest.mark.parametrize("result_failure", [False, True])
def test_run_facade_failures_have_no_raw_exception_context(
    tmp_path, monkeypatch, capsys, result_failure
):
    from types import SimpleNamespace

    common = SimpleNamespace(
        GATE_RUNTIME_KEYS=(),
        local_path=lambda path: Path(path),
        file_hash=lambda path: "a" * 64,
        WitnessError=type("FixtureWitnessError", (Exception,), {}),
        StderrSummary=object,
        OwnedProcess=object,
    )
    env = {
        key: str(tmp_path / key)
        for key in (
            "SYSTEMROOT",
            "TEMP",
            "USERPROFILE",
            "APPDATA",
            "LOCALAPPDATA",
        )
    }
    namespace = test_windows_run_facade_uses_script_result_not_ambient_native_status.__globals__
    monkeypatch.setitem(
        namespace,
        "_startup_witness",
        lambda: SimpleNamespace(
            common=common,
            clean_environment=lambda *args: env.copy(),
        ),
    )
    monkeypatch.setitem(namespace, "_run_facade_bind_modules", lambda script, *args: script)

    calls = []

    def fail_capture(*args, **kwargs):
        calls.append(True)
        if result_failure:
            return b""
        raise subprocess.TimeoutExpired(
            ["PRIVATE_FACADE_COMMAND"],
            15,
            output=b"PRIVATE_FACADE_OUTPUT",
            stderr=b"PRIVATE_FACADE_STDERR",
        )

    monkeypatch.setitem(namespace, "_capture_element_child", fail_capture)
    monkeypatch.setitem(
        namespace,
        "_run_facade_child_record",
        lambda output: dict(
            schema_version="run-facade-return-v1",
            initial="absent",
            case="success",
            source_kind="synthetic",
            outcome="wrapper_failure",
            entered=1,
            cleanup=1,
            arguments_valid=True,
            historical_collision=True,
            ambient_after=None,
        ),
    )
    with pytest.raises(pytest.fail.Exception) as failure:
        test_windows_run_facade_uses_script_result_not_ambient_native_status(
            tmp_path, "synthetic", "absent", "success"
        )
    assert failure.value.__context__ is None
    assert calls == [True]
    capture = capsys.readouterr()
    assert "PRIVATE_FACADE" not in str(failure.value) + capture.out + capture.err
    if result_failure:
        assert RUN_FACADE_PREFIX.decode() in capture.out
        assert '"outcome":"wrapper_failure"' in capture.out


def test_run_facade_child_record_requires_both_qualified_module_prefixes():
    utility = [
        ELEMENT_CHECKPOINT_PREFIX + name.encode()
        for name in (
            "utility_manifest_requested",
            "utility_import_requested",
            "utility_import_returned",
        )
    ]
    utility += [_utility_observation_bytes().rstrip(b"\n")]
    utility += [ELEMENT_CHECKPOINT_PREFIX + b"utility_binding_verified"]
    utility += [
        ELEMENT_CHECKPOINT_PREFIX + name.encode() for name in RUN_FACADE_MANAGEMENT_CHECKPOINTS[:3]
    ]
    utility += [
        _utility_observation_bytes()
        .rstrip(b"\n")
        .replace(b"UTILITY", b"MANAGEMENT")
        .replace(b"utility", b"management")
    ]
    utility += [ELEMENT_CHECKPOINT_PREFIX + b"management_binding_verified"]
    stages = [ELEMENT_CHECKPOINT_PREFIX + name.encode() for name in RUN_FACADE_CHECKPOINTS]
    value = dict(
        schema_version="run-facade-return-v1",
        initial="absent",
        case="success",
        source_kind="synthetic",
        outcome="returned",
        entered=1,
        cleanup=1,
        arguments_valid=True,
        historical_collision=True,
        ambient_after=None,
    )
    record = RUN_FACADE_PREFIX + json.dumps(value).encode()
    output = b"\n".join([*utility, *stages, record]) + b"\n"
    assert _run_facade_child_record(output) == value
    for malformed in (
        b"\n".join([*utility[:3], *utility[4:], *stages, record]) + b"\n",
        b"\n".join([*utility, *stages[1:], record]) + b"\n",
        b"\n".join([*utility, *reversed(stages), record]) + b"\n",
        b"\n".join([*utility, *stages, stages[-1], record]) + b"\n",
        b"\n".join([record, *utility[:2], utility[3], utility[2], utility[4], *stages]) + b"\n",
        b"\n".join([*utility, *stages[:-1], record, stages[-1]]) + b"\n",
        output + b"PRIVATE_FACADE_OUTPUT\n",
        b"\n".join([*utility[:8], *utility[9:], *stages, record]) + b"\n",
        output.replace(b"management_binding_verified", b"management_import_returned"),
        output.replace(b"element-management-binding-v3", b"element-utility-binding-v3"),
        output[:-1],
    ):
        with pytest.raises(ValueError):
            _run_facade_child_record(malformed)


def test_run_facade_management_binding_reuses_exact_module_admission_before_run(tmp_path):
    from types import SimpleNamespace

    source = RUN_FACADE_MANAGEMENT_IMPORT
    assert "__MANAGEMENT_HOME__" in source and "__MANAGEMENT_SHA256__" in source
    assert "'Modules\\Microsoft.PowerShell.Management'" in source
    assert "'Microsoft.PowerShell.Management.psd1'" in source
    assert "[IO.FileAttributes]::ReparsePoint" in source
    assert "$manifestStream.Length -gt 65536" in source
    assert "$manifestStream.Dispose()" in source and "$manifestHasher.Dispose()" in source
    assert "-Name $managementManifest -Global -PassThru -ErrorAction Stop" in source
    assert "$managementModule.ModuleBase), $expectedHome" in source
    assert "$managementModule.Path), $managementManifest" in source
    for command, implementation in (
        ("Join-Path", "JoinPathCommand"),
        ("Test-Path", "TestPathCommand"),
    ):
        assert f".ExportedCmdlets['{command}']" in source
        assert f"'Microsoft.PowerShell.Commands.{implementation}'" in source
    assert "$managementTestToken.Length -ne 8 -or $coreToken.Length -ne 8" in source
    assert (
        "[BitConverter]::ToString($managementTestToken) -cne [BitConverter]::ToString($coreToken)"
        in source
    )
    assert "Parser]::Parse" not in source and "Get-Command" not in source
    assert source.index(RUN_FACADE_MANAGEMENT_TEST_PATH) < source.index(_management_verified)
    assert RUN_FACADE_SCRIPT.index(source) < RUN_FACADE_SCRIPT.index("& $Run -InstallRoot")
    common = SimpleNamespace(local_path=lambda path: Path(path))
    shell = tmp_path / "powershell.exe"
    for name in ("Utility", "Management"):
        manifest = (
            tmp_path / f"Modules/Microsoft.PowerShell.{name}/Microsoft.PowerShell.{name}.psd1"
        )
        manifest.parent.mkdir(parents=True)
        manifest.write_bytes(name.encode("ascii"))
    bound = _run_facade_bind_modules(RUN_FACADE_SCRIPT, shell, common)
    for name in ("UTILITY", "MANAGEMENT"):
        assert f"__{name}_HOME__" not in bound and f"__{name}_SHA256__" not in bound
        assert hashlib.sha256(name.title().encode("ascii")).hexdigest() in bound
    manifest.write_bytes(b"x" * 65537)
    with pytest.raises(ValueError, match="^Invalid scoped Management manifest$"):
        _run_facade_bind_modules(RUN_FACADE_SCRIPT, shell, common)


def test_run_facade_management_observation_is_fixed_typed_and_source_free():
    record = (
        _utility_observation_bytes()
        .replace(b"UTILITY", b"MANAGEMENT")
        .replace(b"utility", b"management")
    )
    observed = _run_facade_management_binding_records(record)
    assert len(observed) == 1
    assert observed[0]["schema_version"] == "element-management-binding-v3"
    assert observed[0]["module_base_kind"] == "exact_pshome"
    for malformed in (
        record * 2,
        record.replace(b"element-management-binding-v3", b"element-utility-binding-v3"),
        record.replace(b'"module_base_kind":"exact_pshome"', b'"module_base_kind":"PRIVATE_PATH"'),
        record.replace(b'"module_count_ok":true', b'"module_count_ok":1'),
        record.replace(b'"module_count_ok":true', b'"module_count_ok":true,"module_count_ok":true'),
        record + b"x" * 4097,
    ):
        with pytest.raises(ValueError):
            _run_facade_management_binding_records(malformed)


def test_run_facade_stderr_observer_accepts_only_exact_relay_protocol():
    import io

    common = _startup_witness().common
    summary_type, _ = _run_facade_capture_classes(common)
    nonce = "1" * 32
    marker = "K5_GATE_" + nonce + "_"
    protocol = "".join(
        "\n" + marker + item + "\n"
        for item in (
            "STATE=opened",
            "STATE=started",
            "EXIT=0",
        )
    ).encode()

    class Chunked(io.BytesIO):
        def read1(self, size=-1):
            return super().read1(min(size, 3))

    for wire, accepted in (
        (protocol, True),
        (protocol.replace(b"\n", b"\r\n"), True),
        (b"PRIVATE_STDERR" + protocol, False),
        (protocol + b"PRIVATE_STDERR", False),
        (protocol.replace(b"STATE=started", b"PRIVATE_STDERR"), False),
        (protocol.replace(b"EXIT=0", b"EXIT=7"), False),
        (protocol.replace(nonce.encode(), b"2" * 32), False),
        (protocol[:-1], False),
    ):
        summary = summary_type(Chunked(wire), gated=True, nonce=nonce)
        assert summary.finish(1)
        assert summary.facade_child_stderr_empty is accepted
        assert not hasattr(summary, "raw")
    for wire, accepted in ((b"", True), (b"PRIVATE_STDERR", False)):
        summary = summary_type(Chunked(wire), gated=False)
        assert summary.finish(1)
        assert summary.facade_child_stderr_empty is accepted


@pytest.mark.parametrize("child_stderr", [False, True])
def test_run_facade_owned_wait_preserves_child_stderr_refusal(child_stderr):
    import io

    common = _startup_witness().common
    waited = []

    class CompletedOwned:
        def __init__(self, summary):
            self.stderr_summary = summary

        def wait(self, seconds):
            waited.append(seconds)
            assert self.stderr_summary.finish(1)

    common.OwnedProcess = CompletedOwned
    summary_type, owned_type = _run_facade_capture_classes(common)
    summary = summary_type(io.BytesIO(b"PRIVATE_STDERR" if child_stderr else b""), gated=False)
    owned = owned_type(summary)
    if child_stderr:
        with pytest.raises(common.WitnessError, match="^output_invalid$"):
            owned.wait(5)
    else:
        owned.wait(5)
    assert waited == [5]


def test_run_facade_matrix_covers_equal_nonzero_success_and_failure_without_syntax_lock():
    assert len(RUN_FACADE_SCENARIOS) == len(set(RUN_FACADE_SCENARIOS)) == 14
    assert set(RUN_FACADE_SCENARIOS) == {
        (initial, case)
        for initial in ("absent", "stale_zero", "stale_nonzero")
        for case in RUN_FACADE_CASES
    } | {("stale_seven", "success"), ("stale_seven", "child_exit_nonzero")}
    assert RUN_FACADE_INITIALS["stale_seven"] == 7
    assert "elseif ($Initial -ceq 'stale_seven') { $global:LASTEXITCODE = 7 }" in RUN_FACADE_SCRIPT
    assert "if ($global:K5FacadeCase -ceq 'child_exit_nonzero') { exit 7 }" in RUN_FACADE_SCRIPT
    assert hashlib.sha256(RUN_FACADE_HISTORICAL_COMPARISON.encode("ascii")).hexdigest() == (
        "881ec07c4e790320e4af0e2fa8e582986e0f484096c658be4d694c7c8a114c96"
    )
    assert RUN_FACADE_HISTORICAL_COMPARISON in RUN_FACADE_SCRIPT
    assert "@(0,7,$true), @(7,7,$false), @(7,0,$false)" in RUN_FACADE_SCRIPT
    # Actual Windows observations enforce the contract, not a chosen source spelling.
    # Fixture preparation must not autoload path cmdlets before actual Run admission.
    preparation = RUN_FACADE_SCRIPT.split("__PRODUCT_SUCCESS_RETURN__", 2)[-1].split(
        "$global:K5FacadeCase = $Case", 1
    )[0]
    assert "[IO.Path]::Combine($InstallRoot, 'Start-K5VisionAlpha.ps1')" in preparation
    assert "Join-Path" not in preparation


TEST_STATUS_BLOCKS = {
    "gst_version": (
        "$output = @(& $gstLaunch --version 2>$null)\n"
        "$gstLaunchSucceeded = $?\n"
        'if (-not $gstLaunchSucceeded -or -not (($output -join "\`n").Contains("GStreamer $gstreamerVersion"))) {\n'
        '    throw "Reviewed GStreamer runtime version verification failed."\n'
        "}"
    ),
    "cli_version": (
        "& $python -I -B -m k5vision.cli --version\n"
        "$cliVersionSucceeded = $?\n"
        'if (-not $cliVersionSucceeded) { throw "Installed K5 CLI verification failed." }'
    ),
    "cli_help": (
        "& $python -I -B -m k5vision.cli --help *> $null\n"
        "$cliHelpSucceeded = $?\n"
        'if (-not $cliHelpSucceeded) { throw "Installed K5 CLI smoke test failed." }'
    ),
}


@pytest.mark.parametrize("boundary", sorted(TEST_STATUS_BLOCKS))
def test_test_facade_captures_each_native_status_immediately(boundary):
    source = TEST.read_text(encoding="utf8").replace("\r\n", "\n")
    block = TEST_STATUS_BLOCKS[boundary]
    assert source.count(block) == 1
    lines = block.splitlines()
    assert lines[1].endswith("Succeeded = $?")
    assert "$LASTEXITCODE" not in block


def test_test_facade_has_no_ambient_native_exit_reads():
    source = TEST.read_text(encoding="utf8")
    assert "$LASTEXITCODE" not in source
    assert source.count("Succeeded = $?") == 3

