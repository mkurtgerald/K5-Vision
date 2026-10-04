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
