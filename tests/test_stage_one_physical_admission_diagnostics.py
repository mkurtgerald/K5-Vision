"""Source-bound synthetic host diagnostics; never query or signal the local host."""

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/assert-stage-one-physical-admission.ps1"
# Exact c5b630be original, used only as an inert mocked decision/error oracle.
BASELINE = (
    '[CmdletBinding()]\nparam()\n\n$ErrorActionPreference = "Stop"\nS'
    "et-StrictMode -Version Latest\n\n# This is a fresh occupancy o"
    "bservation, not a cross-repository lock. A separately\n# seri"
    "alized launch decision must keep other jobs off this shared "
    "physical host.\n# Never signal an existing process or release"
    ' a port to make qualification pass.\n$failure = "inventory_un'
    'available"\ntry {\n    $failure = "host_identity"\n    if ($env'
    ':OS -cne "Windows_NT" -or\n        [Environment]::MachineName'
    ' -cne "VLR-CYZ4PK3" -or\n        [string]$env:RUNNER_NAME -cn'
    'e "K5-Physical") {\n        throw "admission"\n    }\n\n    $fai'
    'lure = "inventory_unavailable"\n    $processes = @(Get-CimIns'
    "tance -ClassName Win32_Process -OperationTimeoutSec 10 -Erro"
    "rAction Stop)\n    $workers = @($processes | Where-Object { ["
    'string]$_.Name -ieq "Runner.Worker.exe" })\n    $failure = "w'
    'orker_ownership"\n    if ($workers.Count -ne 1) { throw "admi'
    'ssion" }\n    $byId = @{}\n    foreach ($process in $processes'
    ") {\n        $processId = [int]$process.ProcessId\n        if "
    "($processId -le 0) { continue }\n        if ($byId.ContainsKe"
    'y($processId)) { throw "admission" }\n        $byId[$processI'
    "d] = $process\n    }\n    $currentId = [int]$PID\n    $seen = @"
    "{}\n    $owned = $false\n    for ($depth = 0; $depth -lt 32; $"
    "depth++) {\n        if ($seen.ContainsKey($currentId) -or -no"
    't $byId.ContainsKey($currentId)) {\n            throw "admiss'
    'ion"\n        }\n        $seen[$currentId] = $true\n        $cu'
    "rrent = $byId[$currentId]\n        if ($null -eq $current.Cre"
    'ationDate) { throw "admission" }\n        if ($currentId -eq '
    "[int]$workers[0].ProcessId) {\n            $owned = $true\n   "
    "         break\n        }\n        $parentId = [int]$current.P"
    "arentProcessId\n        if (-not $byId.ContainsKey($parentId)"
    ') { throw "admission" }\n        $parent = $byId[$parentId]\n '
    "       if ($null -eq $parent.CreationDate -or $parent.Creati"
    'onDate -gt $current.CreationDate) {\n            throw "admis'
    'sion"\n        }\n        $currentId = $parentId\n    }\n    if '
    '(-not $owned) { throw "admission" }\n\n    $failure = "runtime'
    "_occupied\"\n    $nativeNames = '^(EdgeVMS-Gate001|edgevms-dev"
    "iceops|mediamtx|gst-launch-1\\.0|ffmpeg|python|pythonw|k5-vis"
    "ion)\\.exe$'\n    if (@($processes | Where-Object { [string]$_"
    '.Name -match $nativeNames }).Count -ne 0) {\n        throw "a'
    'dmission"\n    }\n\n    $failure = "inventory_unavailable"\n    '
    "$tcp = @(Get-NetTCPConnection -State Listen -ErrorAction Sto"
    "p)\n    $udp = @(Get-NetUDPEndpoint -ErrorAction Stop)\n    $f"
    'ailure = "endpoint_occupied"\n    $tcpPorts = @(8000, 8554, 8'
    "780, 8781, 8888, 8889, 9996, 9997, 9998)\n    if (@($tcp | Wh"
    "ere-Object { [int]$_.LocalPort -in $tcpPorts }).Count -ne 0 "
    "-or\n        @($udp | Where-Object { [int]$_.LocalPort -eq 81"
    '89 }).Count -ne 0) {\n        throw "admission"\n    }\n} catch'
    ' {\n    throw "Stage One physical admission refused: $failure'
    '. Existing owners were preserved."\n}\n\nWrite-Output "Stage On'
    'e physical admission passed for this observation only."\n'
)
BASELINE_SHA256 = "b2ff8527b9ab93a7b8c73bae84b27744bf43a5a46145dce5eac0879c0d62efcd"


def test_original_guard_predicates_are_byte_preserved_and_projection_is_last():
    text = SCRIPT.read_text()
    assert hashlib.sha256(BASELINE.encode()).hexdigest() == BASELINE_SHA256
    # Strip exactly the diagnostic additions to independently compare all legacy
    # predicates, exception text, cleanup boundaries and their relative ordering.
    restored = (
        '[CmdletBinding()]\nparam()\n\n$ErrorActionPreference = "St'
        'op"\nSet-StrictMode -Version Latest\n\n'
    )
    restored += text[text.index("# This is a fresh occupancy") :]
    start = restored.index("    $hostVerified = $true\n")
    end = restored.index("    $workers = @(", start)
    restored = (
        restored[:start]
        + (
            '    $failure = "inventory_unavailable"\n    $processes ='
            " @(Get-CimInstance -ClassName Win32_Process -OperationT"
            "imeoutSec 10 -ErrorAction Stop)\n"
        )
        + restored[end:]
    )
    restored = restored.replace(
        "    if ($workers.Count -le 32768) { $diagnosticWorkerCount = $workers.Count }\n", ""
    )
    restored = restored.replace("    $diagnosticOwned = $owned\n", "")
    restored = restored.replace(
        (
            "} finally {\n    # Output failure must never mask the or"
            "iginal guard outcome.\n    try { Write-K5NameObservation"
            " } catch {}\n}"
        ),
        "}",
    )
    assert restored == BASELINE
    assert text.count("Get-CimInstance") == 1
    assert "-Property Name, ProcessId, ParentProcessId, CreationDate" in text
    assert text.index("try { Write-K5NameObservation }") > text.index("$_.LocalPort -eq 8189")
    for forbidden in (
        "ExecutablePath",
        "CommandLine",
        "MainModule",
        "Get-Process",
        "OpenProcess",
        "LookupAccount",
        "Stop-Process",
        "taskkill",
    ):
        assert forbidden not in text
    assert "runner_utc_unverified" in text
    assert "requires_exact_actions_step_binding = $true" in text
    assert "edge_admission = $false" in text and "retry_authority = $false" in text


def test_hosted_selection_adds_diagnostics_without_new_native_trigger():
    smoke = (ROOT / ".github/workflows/windows-alpha-script-smoke.yml").read_text()
    assert smoke.count('      - "scripts/assert-stage-one-physical-admission.ps1"') == 2
    assert smoke.count('      - "tests/test_stage_one_physical_admission_diagnostics.py"') == 2
    assert (
        "tests/test_installer_wheel_storage.py tests/test_stage_"
        "one_physical_admission_diagnostics.py"
    ) in smoke
    assert "branches: [main]" in smoke


@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell required")
@pytest.mark.parametrize(
    "case",
    [
        "zero",
        "present",
        "duplicate",
        "null",
        "path",
        "newline",
        "overbound",
        "positive-incomplete",
        "provider-error",
        "occupied",
        "worker",
        "port",
        "mutable",
        "throwing",
        "stale-sha",
        "stale-run",
        "stale-attempt",
        "wrong-phase",
        "bad-tree",
        "bad-producer",
        "clock-reversal",
    ],
    ids=str,
)
def test_mocked_powershell_guard_parity_one_snapshot_and_exact_zero(tmp_path, case):
    # Only the host-name environment seam is replaced; both exact predicate
    # bodies execute with the same mocked CIM/endpoint providers.
    candidate = SCRIPT.read_text().replace("[Environment]::MachineName", "$env:K5_TEST_MACHINE")
    if case == "clock-reversal":
        candidate = candidate.replace(
            "$snapshotEnd = [DateTime]::UtcNow }",
            "$snapshotEnd = [DateTime]::UtcNow.AddMinutes(-1) }",
        )
    original = BASELINE.replace("[Environment]::MachineName", "$env:K5_TEST_MACHINE")
    (tmp_path / "candidate.ps1").write_text(candidate)
    (tmp_path / "baseline.ps1").write_text(original)
    harness = r"""param([string]$Root, [string]$Case)
$ErrorActionPreference = 'Stop'
$env:OS = 'Windows_NT'; $env:K5_TEST_MACHINE = 'VLR-CYZ4PK3'; $env:RUNNER_NAME = 'K5-Physical'
$env:GITHUB_REPOSITORY = 'mkurtgerald/K5-Vision'
$env:GITHUB_SHA = 'a' * 40; $env:K5_STAGE_ONE_REVISION = $env:GITHUB_SHA
$env:GITHUB_RUN_ID = '100'; $env:GITHUB_RUN_ATTEMPT = '2'
$context = @{ repository=$env:GITHUB_REPOSITORY;
            source_sha=$env:GITHUB_SHA;
            source_tree=('b' * 40);
            producer_sha256=('c' * 64);
            run_id='100';
            run_attempt='2';
            phase='pre_storage' }
switch ($Case) {
    'stale-sha' { $context.source_sha = 'd' * 40 }
    'stale-run' { $context.run_id = '101' }
    'stale-attempt' { $context.run_attempt = '3' }
    'wrong-phase' { $context.phase = 'post_cleanup' }
    'bad-tree' { $context.source_tree = $true }
    'bad-producer' { $context.producer_sha256 = @('c' * 64) }
}
$results = @()
foreach ($version in @('baseline', 'candidate')) {
    # A called .ps1 has its own script scope. Mutate this shared object rather
    # than script-scoped counters, which would target the guard's child scope.
    $k5MockState = @{calls=0; names=0; properties=@(); tcpCalls=0; udpCalls=0}
    # Keep every synthetic ID positive, bounded and disjoint from the real PID.
    $fixtureBase = if ($PID -lt 100000) { 100000 } else { 1 }
    function Get-CimInstance {
        param($ClassName, $OperationTimeoutSec, $ErrorAction, $Property = @())
        $k5MockState.calls++; $k5MockState.properties = @($Property)
        if ($Case -eq 'provider-error') { throw 'PRIVATE_CANARY provider' }
        $born = [DateTime]::UtcNow.AddMinutes(-10)
        $rows = @(
            [pscustomobject]@{Name='Runner.Worker.exe';
            ProcessId=$fixtureBase;
            ParentProcessId=0;
            CreationDate=$born},
            [pscustomobject]@{Name='powershell.exe';
            ProcessId=$PID;
            ParentProcessId=$fixtureBase;
            CreationDate=$born.AddSeconds(1)}
        )
        if ($Case -eq 'worker') { $rows = @($rows[1]) }
        if ($Case -in @('present','duplicate','positive-incomplete')) {
            $rows += [pscustomobject]@{Name='CRASHPAD_HANDLER.EXE';
            ProcessId=($fixtureBase+1);
            ParentProcessId=0;
            CreationDate=$born}
        }
        if ($Case -eq 'duplicate') { $rows += [pscustomobject]@{Name='crashpad_handler.exe';
            ProcessId=($fixtureBase+2);
            ParentProcessId=0;
            CreationDate=$born} }
        if ($Case -in @('null','positive-incomplete','path','newline','occupied')) {
            $name = switch ($Case) { 'path' { 'C:\PRIVATE_CANARY\thing.exe' }
                'newline' { "PRIVATE_CANARY`n" }
                'occupied' { 'python.exe' }
                default { $null } }
            $rows += [pscustomobject]@{Name=$name;
            ProcessId=($fixtureBase+3);
            ParentProcessId=0;
            CreationDate=$born}
        }
        if ($Case -eq 'overbound') {
            $rows += @(for ($i=0; $i -lt 32767; $i++) {
                [pscustomobject]@{Name='idle.exe'; ProcessId=($fixtureBase+5+$i)
                    ParentProcessId=0; CreationDate=$born}
            })
        }
        if ($Case -in @('mutable','throwing')) {
            $row = [pscustomobject]@{ProcessId=($fixtureBase+4);
                ParentProcessId=0; CreationDate=$born}
            $row | Add-Member ScriptProperty Name {
                $k5MockState.names++
                if ($Case -eq 'throwing' -and $k5MockState.names -ge 3) {
                    throw 'PRIVATE_CANARY getter' }
                if ($k5MockState.names -ge 3) { return 'crashpad_handler.exe' }
                return 'idle.exe'
            }
            $rows += $row
        }
        return $rows
    }
    function Get-NetTCPConnection { param($State, $ErrorAction);
            $k5MockState.tcpCalls++;
            if ($Case -eq 'port') { return [pscustomobject]@{LocalPort=8000} };
            return @() }
    function Get-NetUDPEndpoint { param($ErrorAction); $k5MockState.udpCalls++; return @() }
    $accepted = $true; $errorCode = $null
    try {
        if ($version -eq 'candidate') {
            & (Join-Path $Root 'candidate.ps1') -DiagnosticContext $context }
        else { & (Join-Path $Root 'baseline.ps1') }
    } catch { $accepted=$false; $errorCode=[string]$_.Exception.Message }
    $results += @{version=$version;
            accepted=$accepted;
            error=$errorCode;
            queries=$k5MockState.calls;
            properties=$k5MockState.properties;
            names=$k5MockState.names;
            tcp_queries=$k5MockState.tcpCalls;
            udp_queries=$k5MockState.udpCalls}
}
Write-Output ('RESULT=' + ($results | ConvertTo-Json -Compress -Depth 4))"""
    (tmp_path / "harness.ps1").write_text(harness)
    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(tmp_path / "harness.ps1"),
            str(tmp_path),
            case,
        ],
        capture_output=True,
        text=True,
        timeout=90,
        check=True,
    )
    assert "PRIVATE_CANARY" not in result.stdout + result.stderr
    lines = result.stdout.splitlines()
    result_lines = [line[7:] for line in lines if line.startswith("RESULT=")]
    assert len(result_lines) == 1
    baseline, candidate_result = json.loads(result_lines[0])
    assert [baseline["version"], candidate_result["version"]] == ["baseline", "candidate"]
    expected_failure = {
        "provider-error": "inventory_unavailable",
        "worker": "worker_ownership",
        "occupied": "runtime_occupied",
        "port": "endpoint_occupied",
    }.get(case)
    expected_error = (
        f"Stage One physical admission refused: {expected_failure}. Existing owners were preserved."
        if expected_failure is not None
        else None
    )
    # Parity alone also passes when both versions fail inside a broken mock.
    for observed in (baseline, candidate_result):
        assert observed["accepted"] is (expected_failure is None), observed
        assert observed["error"] == expected_error, observed
    assert baseline["queries"] == candidate_result["queries"] == 1
    expected_endpoint_queries = int(case not in {"provider-error", "worker", "occupied"})
    for observed in (baseline, candidate_result):
        assert observed["tcp_queries"] == observed["udp_queries"] == expected_endpoint_queries
    assert baseline["properties"] == []
    assert candidate_result["properties"] == [
        "Name",
        "ProcessId",
        "ParentProcessId",
        "CreationDate",
    ]
    if case in {
        "stale-sha",
        "stale-run",
        "stale-attempt",
        "wrong-phase",
        "bad-tree",
        "bad-producer",
        "clock-reversal",
    }:
        assert lines.count("K5_HOST_NAME_OBSERVATION_UNAVAILABLE") == 1
        assert not any(line.startswith("K5_HOST_NAME_OBSERVATION=") for line in lines)
        return
    record_lines = [
        line.split("=", 1)[1] for line in lines if line.startswith("K5_HOST_NAME_OBSERVATION=")
    ]
    assert len(record_lines) == 1
    assert "K5_HOST_NAME_OBSERVATION_UNAVAILABLE" not in lines
    record = json.loads(record_lines[0])
    assert record["provider_complete"] is (case != "provider-error")
    assert record["clock_domain"] == "runner_utc_unverified"
    assert record["retry_authority"] is False and record["edge_admission"] is False
    assert "PRIVATE_CANARY" not in json.dumps(record)
    if case in {"null", "path", "newline", "overbound", "provider-error", "throwing"}:
        assert record["presence"] == "unknown" and record["count"] is None
        assert not record["names_complete"]
    elif case == "positive-incomplete":
        assert record["presence"] == "present" and record["count"] == 1
        assert record["count_kind"] == "lower_bound"
    elif case in {"present", "duplicate", "mutable"}:
        assert record["presence"] == "present"
        assert record["count"] == (2 if case == "duplicate" else 1)
        assert record["count_kind"] == "exact"
    else:
        assert record["presence"] == "absent" and record["count"] == 0
        assert record["names_complete"]
    if case in {"mutable", "throwing"}:
        assert baseline["names"] == 2 and candidate_result["names"] == 3
