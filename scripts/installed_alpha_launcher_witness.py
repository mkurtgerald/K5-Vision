#!/usr/bin/env python3
"""Owned installed-layout/Start-script engineering witness, not installer acceptance.

The generated ball establishes launcher/provider continuity, never person boxes.
The original person-clip app command and acceptance gates remain separate.
"""

from __future__ import annotations

import argparse
import ctypes
import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "_installed_alpha_common", Path(__file__).with_name("installed_analytics_witness.py")
)
assert _SPEC is not None and _SPEC.loader is not None
common = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(common)

_BOUNDARY_SPEC = importlib.util.spec_from_file_location(
    "_installed_alpha_boundary", Path(__file__).with_name("windows_owned_preflight.py")
)
assert _BOUNDARY_SPEC is not None and _BOUNDARY_SPEC.loader is not None
boundary = importlib.util.module_from_spec(_BOUNDARY_SPEC)
_BOUNDARY_SPEC.loader.exec_module(boundary)

RECEIPT_NAME = "installed-alpha-start-script-witness.json"
EXPECTATIONS_NAME = "installed-alpha-start-script-expectations.json"
SCHEMA = "installed-alpha-start-script-v1"
SCOPE = "installed-layout-start-script-engineering-only"
GSTREAMER_VERSION = "1.28.7"
SCRIPT_NAMES = ("Start-K5VisionAlpha.ps1", "Test-K5VisionAlpha.ps1", "Run-K5VisionAlpha.ps1")
IDENTITIES = {
    "source_tree_sha256",
    "k5_payload_sha256",
    "k5_wheel_sha256",
    "analytics_manifest_sha256",
    "analytics_wheel_sha256",
    "runtime_identity_sha256",
    "model_identity_sha256",
    "seed_identity_sha256",
    "native_cache_sha256",
    "wheelhouse_sha256",
    "start_script_sha256",
    "test_script_sha256",
    "run_script_sha256",
}
# Installed RECORD digests can depend on the disposable venv path. Every other
# identity is admitted before installation; this one is produced by the isolated
# RECORD probe, checked again afterwards, and independently bound at validation.
INPUT_IDENTITIES = IDENTITIES - {"runtime_identity_sha256"}
COUNTERS = {
    "delivered_frames",
    "presentations",
    "analytics_provider_submissions",
    "analytics_provider_completions",
    "analytics_failures",
}
RUN_BOOLEANS = {"completed", "analytics_enabled", "cleanup_complete"}
BOOLEANS = {
    "completed",
    "cleanup_complete",
    "invalid_config_refused",
    "invalid_config_no_session",
    "invalid_config_no_media",
    "person_box_acceptance",
}
STAGES = {
    "admission",
    "build",
    "install",
    "probe",
    "invalid_config",
    "launch_1",
    "launch_2",
    "verify",
    "cleanup",
    "complete",
}
FIELDS = (
    IDENTITIES
    | BOOLEANS
    | {
        "schema_version",
        "revision",
        "analytics_revision",
        "run_nonce",
        "acceptance_scope",
        "fixture",
        "execution_context",
        "stage",
        "failure_code",
    }
    | {f"run_{attempt}_{name}" for attempt in (1, 2) for name in COUNTERS | RUN_BOOLEANS}
)
REFUSAL = b"K5_ALPHA_EXPECTED_CONFIG_REFUSAL"
MARKERS = {"admitted", "synthetic", "analytics", "operator", "exit", "refusal"}
DIAGNOSTIC_STAGES = STAGES | {"final_receipt"}
DIAGNOSTIC_CONTRACTS = {
    "input_schema",
    "input_identity",
    "receipt_schema",
    "receipt_write",
    "receipt_identity",
    "receipt_scalar",
    "receipt_state",
    "run_schema",
    "run_boolean",
    "run_counter",
    "frame_count",
    "presentation_count",
    "analytics_activity",
    "analytics_failures",
    "invalid_output_limit",
    "invalid_markers",
    "invalid_exit",
    "invalid_job_total",  # Historical coarse-gate diagnostics remain interpretable.
    "invalid_observation_setup",
    "invalid_initialization",
    "invalid_observation_cleanup",
    "invalid_milestones",
    "invalid_session_change",
    "valid_output_limit",
    "valid_markers",
    "job_active",
    "sessions_empty",
    "child_process",
    "owned_process_cleanup",
    "collector_cleanup",
    "startup_diagnostic_projection",
    "directory_guard_cleanup",
    "directory_guard_setup",
    "driver_contract",
    "owned_layout_cleanup",
}
DIAGNOSTIC_BOOLEANS = {
    "collector_finished",
    "output_invalid",
    "sessions_empty",
    "session_unchanged",
    "health_confirmed",
    "operator_request_observed",
    "timed_out",
    "launcher_requested",
    "launcher_returned",
}
DIAGNOSTIC_COUNTERS = (
    {
        "job_total",
        "job_active",
        "expected_job_total",
        "expected_frames",
        "minimum_presentations",
        "minimum_submissions",
        "minimum_completions",
        "expected_failures",
    }
    | {f"marker_{key}" for key in MARKERS}
    | {f"expected_marker_{key}" for key in MARKERS}
    | {f"counter_{key}" for key in COUNTERS}
)
DIAGNOSTIC_EXITS = {"child_exit_code", "relay_exit_code"}
DIAGNOSTIC_OBSERVATIONS = (
    DIAGNOSTIC_BOOLEANS | DIAGNOSTIC_COUNTERS | DIAGNOSTIC_EXITS | {"gate_state"}
)
ALPHA_DIAGNOSTIC_FIELDS = DIAGNOSTIC_OBSERVATIONS | {
    "schema_version",
    "stage",
    "contract",
    "field",
    "failure_code",
}


def validate_alpha_diagnostic(value: object) -> None:
    if type(value) is not dict or value.keys() != ALPHA_DIAGNOSTIC_FIELDS:
        raise ValueError("invalid Alpha diagnostic")
    if (
        type(value["schema_version"]) is not str
        or value["schema_version"] != "alpha-launcher-diagnostic-v1"
    ):
        raise ValueError("invalid Alpha diagnostic")
    if value["gate_state"] is not None and (
        type(value["gate_state"]) is not str or value["gate_state"] not in common.GATE_STATES
    ):
        raise ValueError("invalid Alpha diagnostic")
    for key, allowed in (
        ("stage", DIAGNOSTIC_STAGES),
        ("contract", DIAGNOSTIC_CONTRACTS),
        ("field", FIELDS | COUNTERS | RUN_BOOLEANS | {"none"}),
        ("failure_code", common.FAILURES - {"none"}),
    ):
        if type(value[key]) is not str or value[key] not in allowed:
            raise ValueError("invalid Alpha diagnostic")
    for key in DIAGNOSTIC_BOOLEANS:
        if value[key] is not None and type(value[key]) is not bool:
            raise ValueError("invalid Alpha diagnostic")
    for key in DIAGNOSTIC_COUNTERS | DIAGNOSTIC_EXITS:
        number = value[key]
        lower = -(2**31) if key in DIAGNOSTIC_EXITS else 0
        if number is not None and (type(number) is not int or not lower <= number < 2**32):
            raise ValueError("invalid Alpha diagnostic")


def alpha_diagnostic(
    stage: str,
    contract: str,
    *,
    field: str = "none",
    failure_code: str = "receipt_invalid",
    observations=None,
) -> dict[str, object]:
    observations = {} if observations is None else observations
    if type(observations) is not dict or observations.keys() - DIAGNOSTIC_OBSERVATIONS:
        raise ValueError("invalid Alpha diagnostic")
    result = {
        **dict.fromkeys(DIAGNOSTIC_OBSERVATIONS),
        "schema_version": "alpha-launcher-diagnostic-v1",
        "stage": stage,
        "contract": contract,
        "field": field,
        "failure_code": failure_code,
        **observations,
    }
    validate_alpha_diagnostic(result)
    return result


def emit_alpha_diagnostic(value: dict[str, object]) -> None:
    validate_alpha_diagnostic(value)
    print("K5_ALPHA_LAUNCHER_DIAGNOSTIC=" + common.canonical(value).decode("ascii"))


class AlphaWitnessError(common.WitnessError):
    def __init__(self, record: dict[str, object], detail=None) -> None:
        validate_alpha_diagnostic(record)
        super().__init__(record["failure_code"], detail)
        self.alpha_diagnostic = record


def contract_require(
    condition: bool,
    stage: str,
    contract: str,
    *,
    field="none",
    observations=None,
    code="receipt_invalid",
) -> None:
    if not condition:
        raise AlphaWitnessError(
            alpha_diagnostic(
                stage, contract, field=field, observations=observations, failure_code=code
            )
        )


def contextual_error(error: BaseException, stage: str, contract: str, observations=None):
    """Merge only allowlisted observations; never stringify an external exception."""
    if isinstance(error, AlphaWitnessError):
        record = {**error.alpha_diagnostic, **({} if observations is None else observations)}
    else:
        record = alpha_diagnostic(
            stage,
            contract,
            failure_code=str(error) if isinstance(error, common.WitnessError) else "unexpected",
            observations=observations,
        )
    detail = error.diagnostic if isinstance(error, common.WitnessError) else None
    if detail is not None:
        for key in ("gate_state", "child_exit_code", "relay_exit_code", "timed_out"):
            if record[key] is None:
                record[key] = detail[key]
        if detail["gate_state"] == "exited" and record["launcher_requested"] is True:
            record["launcher_returned"] = True
    return AlphaWitnessError(record, detail)


def observed_integer(value: object, *, signed=False) -> int | None:
    lower = -(2**31) if signed else 0
    return value if type(value) is int and lower <= value < 2**32 else None


# Source-line projection is derived from unique anchors in the admitted Start
# bytes, not absolute line numbers that could silently drift after a repair.
START_OPERATION_ANCHORS = (
    ("[CmdletBinding()]", "launcher_setup"),
    ("function Invoke-K5AnalyticsPreflight {", "analytics_preflight"),
    (
        "function Test-K5AlphaOperatorReceipt([object]$Receipt, [bool]$AnalyticsRequired) {",
        "receipt_check",
    ),
    ('$python = Join-Path $PSScriptRoot ".venv\\Scripts\\python.exe"', "runtime_admission"),
    (
        '$sessionRoot = Join-Path $env:TEMP ("K5VisionAlpha-" + [Guid]::NewGuid().ToString("N"))',
        "session_create",
    ),
    ("function New-K5Token {", "token_create"),
    ("function ConvertTo-K5Json([hashtable]$Value) {", "json_encode"),
    ("function Test-K5TcpListener([string]$HostName, [int]$TargetPort) {", "listener_probe"),
    ("function Test-K5GStreamerElement([string]$Name) {", "element_probe"),
    ("function Get-K5MediaMtx {", "mediamtx_cache"),
    ("function Start-K5SyntheticSource {", "element_probe"),
    ("$mediaMtx = Get-K5MediaMtx", "mediamtx_cache"),
    ('$configPath = Join-Path $sessionRoot "mediamtx.yml"', "mediamtx_config_write"),
    ("$mediaMtxVersionOutput = @(& $mediaMtx --version 2>&1)", "mediamtx_version"),
    ("$validation = @(& $mediaMtx --validate-conf $configPath 2>&1)", "mediamtx_config_validate"),
    ('throw "Local synthetic RTSP port 8554 is already in use."', "rtsp_port_admission"),
    ('Write-Host "Starting local MediaMTX RTSP server..."', "server_start"),
    ("$serverReady = $false", "server_readiness"),
    ('$source = "rtsp://127.0.0.1:8554/k5synthetic"', "publisher_start"),
    (
        'Write-Host "Synthetic RTSP publisher started; deferring media readback '
        'to the K5 native live-source probe."',
        "publisher_readiness",
    ),
    (
        'Write-Host "Local synthetic RTSP publisher PASS; K5 native media probe pending."',
        "synthetic_return",
    ),
    ("$startupFailure = $_", "synthetic_cleanup"),
    ("$writeToken = New-K5Token", "token_create"),
    ("$priorPath = [string]$env:PATH", "environment_setup"),
    ('if (Test-K5TcpListener "127.0.0.1" $Port) {', "control_port_admission"),
    ("$sourceUri = $null", "source_selection"),
    ("$synthetic = Start-K5SyntheticSource", "synthetic_result"),
    (
        '$env:K5_CONTROL_PLANE_SITE_ID = "alpha-" + [Guid]::NewGuid().ToString("N")',
        "application_environment",
    ),
    (
        'Write-Host "Recording is disabled. Test media and temporary K5 state are not retained."',
        "application_start",
    ),
    ('$baseUri = "http://127.0.0.1:$Port"', "application_health"),
    ('$adminHeaders = @{ Authorization = "Bearer $adminToken" }', "operator_authentication"),
    ('$writeHeaders = @{ Authorization = "Bearer $writeToken" }', "device_enrollment"),
    ('Write-Host "Launching the authenticated K5 Windows operator path..."', "operator_request"),
    (
        "if (-not (Test-K5AlphaOperatorReceipt -Receipt $receipt "
        "-AnalyticsRequired $analyticsRequired)) {",
        "receipt_check",
    ),
    ("finally {", "session_cleanup"),
)
START_FAILURE_MESSAGES = {
    "Windows is required.": "platform",
    "K5 analytics preflight failed. No alpha session was started.": "analytics_refusal",
    "Run Install-K5VisionAlpha.ps1 first.": "python_missing",
    "Installed GStreamer version record is missing.": "gst_record_missing",
    "Installed GStreamer version record is invalid.": "gst_record_invalid",
    "Reviewed GStreamer runtime is unavailable. Run Test-K5VisionAlpha.ps1.": "gst_unavailable",
    "MediaMTX archive failed pinned SHA-256 verification.": "archive_identity",
    "Pinned MediaMTX archive did not contain the Windows executable.": "archive_executable",
    "Pinned MediaMTX executable failed its version probe.": "mediamtx_version",
    "Local synthetic RTSP MediaMTX configuration is invalid.": "mediamtx_config",
    "Local synthetic RTSP port 8554 is already in use.": "rtsp_port_occupied",
    "Local synthetic RTSP server failed to start.": "server_unavailable",
    "Local synthetic RTSP source failed to remain available for K5 probing.": (
        "publisher_unavailable"
    ),
    "Local synthetic RTSP startup failed and owned process cleanup was incomplete.": (
        "synthetic_cleanup"
    ),
    "Public RTSP alpha source failed validation.": "public_source",
    "Public RTSP alpha source is invalid.": "public_source",
    "K5 Vision Alpha failed its local health check.": "application_health",
    "K5 alpha operator login failed.": "operator_login",
    "K5 alpha test device enrollment failed.": "device_enrollment",
    "K5 Windows operator alpha test did not complete the selected acceptance checks.": (
        "operator_receipt"
    ),
}
START_ERROR_PREFIX = b"K5_ALPHA_START_ERROR="
START_ERROR_FIELDS = {
    "schema_version",
    "phase",
    "origin",
    "source_line",
    "operation",
    "error_class",
    "failure",
}
START_ERROR_CLASSES = {
    "known_throw",
    "method_binding",
    "native_stderr",
    "access_denied",
    "missing_resource",
    "win32",
    "variable_undefined",
    "property_missing",
    "null_method",
    "unknown",
}
START_FAILURES = set(START_FAILURE_MESSAGES.values()) | {
    "element_missing",
    "control_port_occupied",
    "unknown",
}
START_OPERATIONS = {operation for _, operation in START_OPERATION_ANCHORS} | {"unknown"}
START_MILESTONES = {
    "provisioning": b"Provisioning pinned local RTSP test server...",
    "version_output": b"mediamtx-version: ",
    "config_output": b"mediamtx-validate: ",
    "server_requested": b"Starting local MediaMTX RTSP server...",
    "server_diagnostics": b"Synthetic RTSP server diagnostics: ",
    "publisher_started": (
        b"Synthetic RTSP publisher started; deferring media readback "
        b"to the K5 native live-source probe."
    ),
    "publisher_diagnostics": b"Synthetic RTSP diagnostics: ",
    "publisher_output": b"publisher: ",
    "publisher_ready": b"Local synthetic RTSP publisher PASS; K5 native media probe pending.",
}
START_PREFIX_MILESTONES = {
    "version_output",
    "config_output",
    "server_diagnostics",
    "publisher_diagnostics",
    "publisher_output",
}


def validate_start_error(value: object) -> None:
    if type(value) is not dict or value.keys() != START_ERROR_FIELDS:
        raise ValueError("invalid Start diagnostic")
    for key, allowed in (
        ("schema_version", {"alpha-start-error-v1"}),
        ("phase", {"primary", "cleanup"}),
        ("origin", {"start", "unknown"}),
        ("operation", START_OPERATIONS),
        ("error_class", START_ERROR_CLASSES),
        ("failure", START_FAILURES),
    ):
        if type(value[key]) is not str or value[key] not in allowed:
            raise ValueError("invalid Start diagnostic")
    line = value["source_line"]
    if value["origin"] == "start":
        if type(line) is not int or not 1 <= line <= 4096:
            raise ValueError("invalid Start diagnostic")
    elif line is not None or value["operation"] != "unknown":
        raise ValueError("invalid Start diagnostic")
    if (value["failure"] == "unknown") != (value["error_class"] != "known_throw"):
        raise ValueError("invalid Start diagnostic")


def parse_start_error(raw: bytes) -> dict:
    if len(raw) > 1024:
        raise ValueError("invalid Start diagnostic")
    pairs = json.loads(raw, object_pairs_hook=list)
    if type(pairs) is not list or any(type(pair) is not tuple or len(pair) != 2 for pair in pairs):
        raise ValueError("invalid Start diagnostic")
    value = dict(pairs)
    if len(pairs) != len(value):
        raise ValueError("invalid Start diagnostic")
    validate_start_error(value)
    return value


def validate_start_observation(value: object) -> None:
    if type(value) is not dict or value.keys() != set(START_MILESTONES) | {
        "schema_version",
        "stage",
        "diagnostic_valid",
        "error_records",
        "collector_finished",
    }:
        raise ValueError("invalid Start observation")
    if (
        type(value["schema_version"]) is not str
        or value["schema_version"] != "alpha-start-observation-v1"
        or type(value["stage"]) is not str
        or value["stage"] not in {"invalid_config", "launch_1", "launch_2"}
    ):
        raise ValueError("invalid Start observation")
    for name in ("diagnostic_valid", "collector_finished"):
        if type(value[name]) is not bool:
            raise ValueError("invalid Start observation")
    for name in set(START_MILESTONES) | {"error_records"}:
        if type(value[name]) is not int or not 0 <= value[name] <= (
            2 if name == "error_records" else 255
        ):
            raise ValueError("invalid Start observation")


# This envelope invokes exactly the admitted Start command. Error projection is
# observational: no raw message, path, stack, exception Data or child output leaves
# it. Missing/unrecognized metadata stays unknown; it never changes an exit gate.
ENVELOPE = r"""param([string]$Start, [int]$Port)
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$admittedStartSha256 = '__ADMITTED_START_SHA256__'
function Get-K5SourceFailure {
    param([System.Management.Automation.ErrorRecord]$Root)
    try {
        $queue = [Collections.Generic.Queue[object]]::new()
        $seen = [Collections.Generic.List[object]]::new()
        $candidates = [Collections.Generic.List[object]]::new()
        $queue.Enqueue([pscustomobject]@{ Value = $Root; Depth = 0; Path = @() })
        $records = 0
        while ($queue.Count -gt 0) {
            if ($seen.Count -ge 8) { return $null }
            $entry = $queue.Dequeue()
            $node = $entry.Value
            if ($entry.Depth -gt 4) { return $null }
            foreach ($prior in $seen) {
                if ([object]::ReferenceEquals($node, $prior)) { return $null }
            }
            $seen.Add($node)
            $path = @($entry.Path) + @($node)
            if ($node -is [System.Management.Automation.ErrorRecord]) {
                $records++
                if ($records -gt 4) { return $null }
                $info = $node.InvocationInfo
                if ($null -ne $info -and -not [string]::IsNullOrEmpty($info.ScriptName)) {
                    if ($info.ScriptLineNumber -lt 1 -or $info.ScriptLineNumber -gt 4096) {
                        return $null
                    }
                    # A descendant replaces its caller, even if the descendant
                    # is foreign. Independent source-bearing branches are never
                    # resolved by depth or by preferring an owned pathname.
                    for ($index = $candidates.Count - 1; $index -ge 0; $index--) {
                        foreach ($ancestor in $entry.Path) {
                            if ([object]::ReferenceEquals($candidates[$index], $ancestor)) {
                                $candidates.RemoveAt($index)
                                break
                            }
                        }
                    }
                    $candidates.Add($node)
                } elseif ($entry.Depth -gt 0 -and
                          $node.Exception -isnot
                            [System.Management.Automation.ParentContainsErrorRecordException]) {
                    return $null
                }
                # A no-provenance ParentContains placeholder is not a competing
                # origin and cannot erase a qualified caller's useful metadata.
                $queue.Enqueue([pscustomobject]@{
                    Value = $node.Exception; Depth = $entry.Depth + 1; Path = $path
                })
            } elseif ($node -is [Exception]) {
                if ($node -is [System.Management.Automation.RuntimeException]) {
                    $linked = $node.ErrorRecord
                    if ($linked -is [System.Management.Automation.ErrorRecord]) {
                        $queue.Enqueue([pscustomobject]@{
                            Value = $linked; Depth = $entry.Depth + 1; Path = $path
                        })
                    }
                }
                if ($null -ne $node.InnerException) {
                    $queue.Enqueue([pscustomobject]@{
                        Value = $node.InnerException; Depth = $entry.Depth + 1; Path = $path
                    })
                }
            } else { return $null }
        }
        if ($candidates.Count -gt 1) { return $null }
        if ($candidates.Count -eq 1) { return $candidates[0] }
        return $Root
    } catch { return $null }
}
function Write-K5StartError {
    param([System.Management.Automation.ErrorRecord]$Failure, [string]$Phase)
    $record = [ordered]@{
        schema_version = 'alpha-start-error-v1'
        phase = $Phase
        origin = 'unknown'
        source_line = $null
        operation = 'unknown'
        error_class = 'unknown'
        failure = 'unknown'
    }
    $originalFailure = $Failure
    $Failure = Get-K5SourceFailure -Root $Failure
    if ($null -eq $Failure) {
        Write-Output ('K5_ALPHA_START_ERROR=' + ($record | ConvertTo-Json -Compress))
        return
    }
    try {
        $message = $Failure.Exception.Message
        $known = ConvertFrom-Json '__KNOWN_MESSAGES__'
        foreach ($item in $known) {
            if ($message -ceq $item[0]) { $record.failure = $item[1]; break }
        }
        $elementPattern = '^Reviewed GStreamer runtime is missing required synthetic test element: '
        $elementPattern += '(videotestsrc|videoconvert|x264enc|h264parse|rtspclientsink|'
        $elementPattern += 'rtspsrc|queue|identity|fakesink)$'
        if ($message -cmatch $elementPattern) {
            $record.failure = 'element_missing'
        }
        $portPattern = '^K5 Vision Alpha control-plane port [0-9]{4,5} is already in use\. '
        $portPattern += 'Close any previous K5 Vision Alpha window or reinstall '
        $portPattern += 'to clean the stale runtime\.$'
        if ($message -cmatch $portPattern) {
            $record.failure = 'control_port_occupied'
        }
        $runtimeId = ''
        if ($Failure.FullyQualifiedErrorId.Length -le 512) {
            $runtimeId = $Failure.FullyQualifiedErrorId.Split(',')[0]
        }
        if ($record.failure -cne 'unknown') { $record.error_class = 'known_throw' }
        elseif ($runtimeId -ceq 'VariableIsUndefined') {
            $record.error_class = 'variable_undefined'
        } elseif ($runtimeId -ceq 'PropertyNotFoundStrict') {
            $record.error_class = 'property_missing'
        } elseif ($runtimeId -ceq 'InvokeMethodOnNull') {
            $record.error_class = 'null_method'
        }
        elseif ($Failure.FullyQualifiedErrorId -cin
                @('NativeCommandError', 'NativeCommandErrorMessage')) {
            $record.error_class = 'native_stderr'
        } else {
            $exception = $originalFailure.Exception
            for ($depth = 0; $depth -lt 4 -and $null -ne $exception; $depth++) {
                if ($exception -is [System.Management.Automation.MethodException] -or
                    $exception -is [System.Management.Automation.MethodInvocationException]) {
                    $record.error_class = 'method_binding'
                } elseif ($exception -is [UnauthorizedAccessException]) {
                    $record.error_class = 'access_denied'; break
                } elseif ($exception -is [IO.FileNotFoundException] -or
                          $exception -is [IO.DirectoryNotFoundException] -or
                          $exception -is [System.Management.Automation.ItemNotFoundException]) {
                    $record.error_class = 'missing_resource'; break
                } elseif ($exception -is [ComponentModel.Win32Exception]) {
                    $record.error_class = 'win32'; break
                }
                $exception = $exception.InnerException
            }
        }
    } catch { $record.error_class = 'unknown'; $record.failure = 'unknown' }
    try {
        $info = $Failure.InvocationInfo
        if ($null -ne $info -and $info.ScriptLineNumber -ge 1 -and
            [string]::Equals([IO.Path]::GetFullPath($info.ScriptName),
                             [IO.Path]::GetFullPath($Start),
                             [StringComparison]::OrdinalIgnoreCase)) {
            # Bound the read itself, including a replacement/growth race.
            $stream = [IO.File]::OpenRead($Start)
            try {
                if ($stream.Length -gt 65536) { throw 'unknown' }
                $buffer = New-Object byte[] 65537
                $count = 0
                while ($count -lt $buffer.Length) {
                    $read = $stream.Read($buffer, $count, $buffer.Length - $count)
                    if ($read -eq 0) { break }
                    $count += $read
                }
                if ($count -gt 65536) { throw 'unknown' }
            } finally { $stream.Dispose() }
            if ($admittedStartSha256 -cnotmatch '^[0-9a-f]{64}$' -or
                $admittedStartSha256 -ceq ('0' * 64)) { throw 'unknown' }
            $hasher = [Security.Cryptography.SHA256]::Create()
            try { $digest = $hasher.ComputeHash($buffer, 0, $count) }
            finally { $hasher.Dispose() }
            $actualHash = [BitConverter]::ToString($digest).Replace('-', '').ToLowerInvariant()
            if ($actualHash -cne $admittedStartSha256) { throw 'unknown' }
            $utf8 = New-Object System.Text.UTF8Encoding($false, $true)
            $lines = $utf8.GetString($buffer, 0, $count).Replace("`r`n", "`n").Split("`n")
            if ($lines.Count -gt 4096 -or $info.ScriptLineNumber -gt $lines.Count) {
                throw 'unknown'
            }
            $record.origin = 'start'
            $record.source_line = $info.ScriptLineNumber
            $anchors = ConvertFrom-Json '__OPERATION_ANCHORS__'
            $previous = 0
            $operation = 'unknown'
            foreach ($anchor in $anchors) {
                $matchesAt = @(for ($index = 0; $index -lt $lines.Count; $index++) {
                    if ($lines[$index].Trim() -ceq $anchor[0]) { $index + 1 }
                })
                if ($matchesAt.Count -ne 1 -or $matchesAt[0] -le $previous) { throw 'unknown' }
                $previous = $matchesAt[0]
                if ($previous -le $record.source_line) { $operation = $anchor[1] }
            }
            $record.operation = $operation
        }
    } catch {
        $record.origin = 'unknown'; $record.source_line = $null; $record.operation = 'unknown'
    }
    Write-Output ('K5_ALPHA_START_ERROR=' + ($record | ConvertTo-Json -Compress))
}
try {
    & $Start -Port $Port -ExitAfterPublicTest
    exit 0
} catch {
    $fatal = $_
    $refusal = 'K5 analytics preflight failed. No alpha session was started.'
    if ($fatal.Exception.Message -ceq $refusal) {
        Write-Output "K5_ALPHA_EXPECTED_CONFIG_REFUSAL"
        exit 23
    }
    $primary = $null
    $exception = $fatal.Exception
    $cleanupMessage = 'Local synthetic RTSP startup failed and owned process cleanup '
    $cleanupMessage += 'was incomplete.'
    for ($depth = 0; $depth -lt 4 -and $null -ne $exception; $depth++) {
        if ($exception.Message -ceq $cleanupMessage -and
            $exception.Data.Contains('K5.StartupErrorRecord') -and
            $exception.Data['K5.StartupErrorRecord'] -is
                [System.Management.Automation.ErrorRecord]) {
            $primary = $exception.Data['K5.StartupErrorRecord']
            break
        }
        $exception = $exception.InnerException
    }
    if ($null -ne $primary) {
        Write-K5StartError -Failure $primary -Phase 'primary'
        Write-K5StartError -Failure $fatal -Phase 'cleanup'
    } else {
        Write-K5StartError -Failure $fatal -Phase 'primary'
    }
    Write-Output "K5_ALPHA_START_FAILED"
    exit 24
}
""".replace(
    "__KNOWN_MESSAGES__", json.dumps(list(START_FAILURE_MESSAGES.items())).replace("'", "''")
).replace("__OPERATION_ANCHORS__", json.dumps(START_OPERATION_ANCHORS).replace("'", "''"))


def bind_start_envelope(expected_sha256: str) -> str:
    """Bind the prior admitted Git/script identity, never derive trust from a reread."""
    if (
        type(expected_sha256) is not str
        or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None
        or expected_sha256 == "0" * 64
    ):
        raise ValueError("invalid admitted Start identity")
    return ENVELOPE.replace("__ADMITTED_START_SHA256__", expected_sha256)


def validate_expectations(
    value: object, *, installed: bool = True, stage: str = "admission"
) -> dict[str, str]:
    names = IDENTITIES if installed else INPUT_IDENTITIES
    contract_require(
        type(value) is dict and value.keys() == names | {"revision", "run_nonce"},
        stage,
        "input_schema",
    )
    for name, length in [("revision", 40), ("run_nonce", 32), *[(key, 64) for key in names]]:
        contract_require(
            type(value[name]) is str
            and re.fullmatch(r"[0-9a-f]{" + str(length) + "}", value[name]) is not None
            and value[name] != "0" * length,
            stage,
            "input_identity",
            field=name,
        )
    return value


def new_receipt(expected: dict[str, str]) -> dict[str, object]:
    return {
        "schema_version": SCHEMA,
        "revision": expected["revision"],
        "analytics_revision": common.ANALYTICS_REVISION,
        "run_nonce": expected["run_nonce"],
        "acceptance_scope": SCOPE,
        "fixture": "generated-ball",
        "execution_context": "owned-installed-start-script-windows-x64",
        "stage": "admission",
        "failure_code": "none",
        **dict.fromkeys(IDENTITIES, "0" * 64),
        **{key: expected[key] for key in IDENTITIES if key in expected},
        **dict.fromkeys(BOOLEANS, False),
        **{
            f"run_{attempt}_{key}": False if key in RUN_BOOLEANS else 0
            for attempt in (1, 2)
            for key in COUNTERS | RUN_BOOLEANS
        },
    }


def run_observations(value: dict[str, object]) -> dict[str, object]:
    return {
        "expected_frames": 225,
        "minimum_presentations": 225,
        "minimum_submissions": 1,
        "minimum_completions": 1,
        "expected_failures": 0,
        **{f"counter_{key}": observed_integer(value.get(key)) for key in COUNTERS},
    }


def validate_run(value: dict[str, object], *, stage="final_receipt") -> None:
    contract_require(value.keys() == COUNTERS | RUN_BOOLEANS, stage, "run_schema")
    observations = run_observations(value)
    for name in RUN_BOOLEANS:
        contract_require(
            value[name] is True, stage, "run_boolean", field=name, observations=observations
        )
    for name in COUNTERS:
        contract_require(
            type(value[name]) is int and 0 <= value[name] <= common.MAX_COUNTER,
            stage,
            "run_counter",
            field=name,
            observations=observations,
        )
    contract_require(
        value["delivered_frames"] == 225, stage, "frame_count", observations=observations
    )
    contract_require(
        value["presentations"] >= 225, stage, "presentation_count", observations=observations
    )
    contract_require(
        0 < value["analytics_provider_completions"] <= value["analytics_provider_submissions"],
        stage,
        "analytics_activity",
        observations=observations,
    )
    contract_require(
        value["analytics_failures"] == 0, stage, "analytics_failures", observations=observations
    )


def validate_receipt(value: object, expected: dict[str, str], *, success: bool = True) -> None:
    stage = "final_receipt"
    validate_expectations(expected, stage=stage)
    contract_require(type(value) is dict and value.keys() == FIELDS, stage, "receipt_schema")
    for name, exact in {
        "schema_version": SCHEMA,
        "analytics_revision": common.ANALYTICS_REVISION,
        "acceptance_scope": SCOPE,
        "fixture": "generated-ball",
        "execution_context": "owned-installed-start-script-windows-x64",
        **expected,
    }.items():
        contract_require(
            type(value[name]) is str and value[name] == exact, stage, "receipt_identity", field=name
        )
    contract_require(
        type(value["stage"]) is str
        and value["stage"] in STAGES
        and type(value["failure_code"]) is str
        and value["failure_code"] in common.FAILURES,
        stage,
        "receipt_state",
    )
    for name in BOOLEANS:
        contract_require(type(value[name]) is bool, stage, "receipt_scalar", field=name)
    contract_require(
        value["person_box_acceptance"] is False,
        stage,
        "receipt_state",
        field="person_box_acceptance",
    )
    for attempt in (1, 2):
        run = {name: value[f"run_{attempt}_{name}"] for name in COUNTERS | RUN_BOOLEANS}
        for name in RUN_BOOLEANS:
            contract_require(
                type(run[name]) is bool, stage, "receipt_scalar", field=f"run_{attempt}_{name}"
            )
        for name in COUNTERS:
            contract_require(
                type(run[name]) is int and 0 <= run[name] <= common.MAX_COUNTER,
                stage,
                "receipt_scalar",
                field=f"run_{attempt}_{name}",
                observations=run_observations(run),
            )
        if success:
            validate_run(run, stage=stage)
    if success:
        contract_require(
            all(value[key] is True for key in BOOLEANS - {"person_box_acceptance"})
            and value["stage"] == "complete"
            and value["failure_code"] == "none",
            stage,
            "receipt_state",
        )
    else:
        contract_require(
            value["completed"] is False and value["failure_code"] != "none", stage, "receipt_state"
        )


def tree_manifest(root: Path, *, maximum_files: int = 50_000) -> dict[str, str]:
    """No links/junctions; the full inventory (including extra files) is bound."""
    root = common.local_path(root, directory=True)
    result: dict[str, str] = {}
    total = 0
    for current, directories, files in os.walk(root, followlinks=False):
        for name in sorted(directories):
            common.local_path(Path(current) / name, directory=True)
        for name in sorted(files):
            path = common.local_path(Path(current) / name)
            total += path.stat().st_size
            common.require(len(result) < maximum_files and total <= 8 * 1024**3, "admission_failed")
            result[path.relative_to(root).as_posix()] = common.file_hash(path)
    common.require(bool(result), "admission_failed")
    return result


def native_roots(local_appdata: Path) -> tuple[Path, Path]:
    tools = local_appdata / "K5RunnerTools"
    return (
        tools / "k5-gstreamer" / GSTREAMER_VERSION / "msvc_x86_64",
        tools / "mediamtx" / "1.21.1",
    )


def cache_marker(path: Path) -> str:
    path = common.local_path(path)
    common.require(path.stat().st_size <= 128, "identity_mismatch")
    return path.read_bytes().decode("ascii").strip()


def native_manifest(local_appdata: Path) -> dict[str, object]:
    gst, mtx = native_roots(local_appdata)
    inventories = {"gstreamer": tree_manifest(gst), "mediamtx": tree_manifest(mtx)}
    common.require(cache_marker(gst / "k5-installer.sha256") == common.GSTREAMER_INSTALLER)
    common.require(cache_marker(mtx / "k5-archive.sha256") == common.MEDIA_MTX_ARCHIVE)
    common.require(cache_marker(mtx / "k5-exe.sha256") == common.file_hash(mtx / "mediamtx.exe"))
    for name in ("gst-launch-1.0.exe", "gst-inspect-1.0.exe"):
        common.local_path(gst / "bin" / name)
    common.require(
        any(
            (gst / "bin" / name).is_file()
            for name in ("gstreamer-1.0-0.dll", "libgstreamer-1.0-0.dll")
        )
    )
    return inventories


def copy_native_cache(original: Path, destination: Path, expected: str) -> None:
    common.require(common.digest(native_manifest(original)) == expected)
    for source, target in zip(native_roots(original), native_roots(destination), strict=True):
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, target)
    common.require(common.digest(native_manifest(destination)) == expected)
    # Detect a shared input changing during the copy without repairing/deleting it.
    common.require(common.digest(native_manifest(original)) == expected)


def source_payload(source: Path) -> dict[str, str]:
    return {
        "k5vision/" + name: value for name, value in tree_manifest(source / "src/k5vision").items()
    }


def install_scripts(source: Path, installed: Path, revision: str) -> dict[str, str]:
    identities = {}
    for name, key in zip(SCRIPT_NAMES, ("start", "test", "run"), strict=True):
        origin, target = source / "scripts/windows-alpha" / name, installed / name
        shutil.copyfile(common.local_path(origin), target)
        common.require(target.read_bytes() == origin.read_bytes())
        identities[f"{key}_script_sha256"] = common.file_hash(target)
    (installed / "k5-revision.txt").write_bytes(revision.encode("ascii"))
    (installed / "gstreamer-version.txt").write_bytes(GSTREAMER_VERSION.encode("ascii"))
    return identities


def verify_layout(installed: Path, expected: dict[str, str]) -> None:
    for name, key in zip(SCRIPT_NAMES, ("start", "test", "run"), strict=True):
        common.require(
            common.file_hash(common.local_path(installed / name))
            == expected[f"{key}_script_sha256"]
        )
    common.require((installed / "k5-revision.txt").read_bytes() == expected["revision"].encode())
    common.require((installed / "gstreamer-version.txt").read_bytes() == GSTREAMER_VERSION.encode())


def clean_environment(base: dict[str, str], work: Path) -> dict[str, str]:
    allowed = {
        "SYSTEMROOT",
        "WINDIR",
        "SYSTEMDRIVE",
        "COMSPEC",
        "OS",
        "NUMBER_OF_PROCESSORS",
        "PROCESSOR_ARCHITECTURE",
        "PROCESSOR_IDENTIFIER",
    }
    env = {key.upper(): value for key, value in base.items() if key.upper() in allowed}
    system = Path(env.get("SYSTEMROOT", "/missing"))
    env.update(common.clean_environment({}))
    env.update({key: base[key] for key in common.GATE_RUNTIME_KEYS if key in base})
    env.update(
        PATH=str(system / "System32") + os.pathsep + str(system),
        USERPROFILE=str(work / "profile"),
        APPDATA=str(work / "profile/roaming"),
        LOCALAPPDATA=str(work / "profile/local"),
        TEMP=str(work / "sessions"),
        TMP=str(work / "sessions"),
        RUNNER_TEMP=str(work),
        PIP_CACHE_DIR=str(work / "pip-cache"),
        PIP_NO_INDEX="1",
        PIP_NO_INPUT="1",
        PIP_NO_COMPILE="1",
    )
    return env


def start_command(powershell: Path, envelope: Path, installed: Path, port: int) -> list[str]:
    common.require(type(port) is int and 1024 <= port <= 65535, "admission_failed")
    return [
        str(powershell),
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-File",
        str(envelope),
        "-Start",
        str(installed / SCRIPT_NAMES[0]),
        "-Port",
        str(port),
    ]


class LaunchSummary:
    """Bounded streaming stdout; retain counters/flags only, never raw output."""

    def __init__(self, stream) -> None:
        self.counts = dict.fromkeys(
            ("admitted", "synthetic", "analytics", "operator", "exit", "refusal"), 0
        )
        self.scalars: dict[str, int] = {}
        self.invalid = False
        self.start_errors: list[dict] = []
        self.start_diagnostic_invalid = False
        self.start_milestones = dict.fromkeys(START_MILESTONES, 0)
        self.health_confirmed = False
        self.operator_request_observed = False
        self.thread = threading.Thread(target=self._read, args=(stream,), daemon=True)
        self.thread.start()

    def _line(self, line: bytes) -> None:
        line = line.rstrip(b"\r")
        if line.startswith(START_ERROR_PREFIX):
            try:
                value = parse_start_error(line[len(START_ERROR_PREFIX) :])
                if len(self.start_errors) >= 2 or value["phase"] != (
                    "cleanup" if self.start_errors else "primary"
                ):
                    raise ValueError("invalid Start diagnostic")
                self.start_errors.append(value)
            except (ValueError, TypeError, RecursionError):
                self.start_diagnostic_invalid = True
        for key, pattern in START_MILESTONES.items():
            if line.startswith(pattern) if key in START_PREFIX_MILESTONES else line == pattern:
                self.start_milestones[key] = min(255, self.start_milestones[key] + 1)
        # Diagnostic milestones for media runs; invalid preflight refuses either observation.
        if line == b"K5 Vision Alpha health check PASS.":
            self.health_confirmed = True
        if line == b"Launching the authenticated K5 Windows operator path...":
            self.operator_request_observed = True
        markers = {
            "admitted": (
                b"K5 analytics configuration admitted; live provider acceptance is pending."
            ),
            "exit": b"Exiting after one bounded alpha acceptance run.",
            "refusal": REFUSAL,
        }
        for name, exact in markers.items():
            if line == exact:
                self.counts[name] += 1
        if re.fullmatch(
            rb"Starting K5 Vision Alpha local synthetic operator test on http://127\.0\.0\.1:[0-9]{4,5}",
            line,
        ):
            self.counts["synthetic"] += 1
        analytics = re.fullmatch(
            rb"K5 analytics PASS: submissions=([0-9]{1,7}), "
            rb"completions=([0-9]{1,7}), failures=([0-9]{1,7})",
            line,
        )
        if analytics:
            self.counts["analytics"] += 1
            for name, raw in zip(
                (
                    "analytics_provider_submissions",
                    "analytics_provider_completions",
                    "analytics_failures",
                ),
                analytics.groups(),
                strict=True,
            ):
                self.scalars[name] = int(raw)
        operator = re.fullmatch(
            rb"K5 operator PASS: frames=([0-9]{1,7}), presentations=([0-9]{1,7})", line
        )
        if operator:
            self.counts["operator"] += 1
            self.scalars.update(delivered_frames=int(operator[1]), presentations=int(operator[2]))

    def _read(self, stream) -> None:
        total, pending = 0, b""
        try:
            while block := stream.read1(4096):
                total += len(block)
                if total > 65_536:
                    self.invalid = True
                    pending = b""
                    continue  # Drain/discard a flood; never deadlock child shutdown.
                pending += block
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    if len(line) > 2048:
                        self.invalid = True
                    else:
                        self._line(line)
                if len(pending) > 2048:
                    self.invalid = True
                    pending = b""
            if pending:
                self._line(pending)
        except (OSError, ValueError):
            self.invalid = True
        finally:
            stream.close()

    def finish(self) -> None:
        self.thread.join(5)
        common.require(not self.thread.is_alive(), "cleanup_incomplete")

    def observations(self, *, invalid=False) -> dict[str, object]:
        expected = {key: int(key == "refusal" if invalid else key != "refusal") for key in MARKERS}
        return {
            "collector_finished": not self.thread.is_alive(),
            "output_invalid": self.invalid,
            "health_confirmed": self.health_confirmed,
            "operator_request_observed": self.operator_request_observed,
            **{f"marker_{key}": observed_integer(self.counts[key]) for key in MARKERS},
            **{f"expected_marker_{key}": number for key, number in expected.items()},
            **run_observations(self.scalars),
        }

    def emit_start_diagnostics(self, stage: str) -> None:
        for value in self.start_errors:
            validate_start_error(value)
            print(START_ERROR_PREFIX.decode("ascii") + common.canonical(value).decode("ascii"))
        value = {
            "schema_version": "alpha-start-observation-v1",
            "stage": stage,
            "diagnostic_valid": not self.start_diagnostic_invalid,
            "error_records": len(self.start_errors),
            "collector_finished": not self.thread.is_alive(),
            **self.start_milestones,
        }
        validate_start_observation(value)
        print("K5_ALPHA_START_OBSERVATION=" + common.canonical(value).decode("ascii"))

    def result(self, *, stage="launch_1") -> dict[str, object]:
        observations = self.observations()
        contract_require(not self.invalid, stage, "valid_output_limit", observations=observations)
        contract_require(
            self.counts
            == {
                "admitted": 1,
                "synthetic": 1,
                "analytics": 1,
                "operator": 1,
                "exit": 1,
                "refusal": 0,
            },
            stage,
            "valid_markers",
            observations=observations,
        )
        result = {**self.scalars, **dict.fromkeys(RUN_BOOLEANS, True)}
        validate_run(result, stage=stage)
        return result


class DirectoryChangeGuard:
    """Windows kernel notification remembers even a create-then-delete session."""

    def __init__(self, directory: Path) -> None:
        from ctypes import wintypes

        common.require(os.name == "nt", "admission_failed")
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.FindFirstChangeNotificationW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        self.api.FindFirstChangeNotificationW.restype = wintypes.HANDLE
        self.api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.api.WaitForSingleObject.restype = wintypes.DWORD
        self.api.FindCloseChangeNotification.argtypes = [wintypes.HANDLE]
        # File name, directory name, size, and last-write changes; entire subtree.
        self.handle = self.api.FindFirstChangeNotificationW(str(directory), True, 0x1B)
        common.require(self.handle not in (None, ctypes.c_void_p(-1).value), "admission_failed")

    def unchanged(self) -> bool:
        status = self.api.WaitForSingleObject(self.handle, 0)
        common.require(status in (0, 258), "cleanup_incomplete")
        return status == 258

    def close(self) -> None:
        common.require(
            bool(self.api.FindCloseChangeNotification(self.handle)), "cleanup_incomplete"
        )


def job_accounting(owned) -> tuple[int, int]:
    common.require(owned.job is not None, "admission_failed")
    counters = owned.job.accounting()
    return counters.total_processes, counters.active_processes


def emit_observation_failure(error, phase: str):
    # These are helper-owned enum values, never text from an OS/child exception.
    code = str(error) if isinstance(error, boundary.ObservationFailure) else "native_error"
    value = {
        "schema_version": "owned-preflight-failure-v1",
        "stage": "invalid_config",
        "phase": phase,
        "error": code,
    }
    validate_observation_failure(value)
    print("K5_OWNED_PREFLIGHT_FAILURE=" + common.canonical(value).decode("ascii"))


def validate_observation_failure(value):
    common.require(
        type(value) is dict and value.keys() == {"schema_version", "stage", "phase", "error"}
    )
    for key, allowed in (
        ("schema_version", {"owned-preflight-failure-v1"}),
        ("stage", {"invalid_config"}),
        ("phase", {"setup", "finish", "validate", "cleanup"}),
        ("error", boundary.ERRORS - {"none"}),
    ):
        common.require(type(value[key]) is str and value[key] in allowed)


def invoke_start(
    command: list[str],
    *,
    work: Path,
    env: dict[str, str],
    operation: str,
    invalid: bool = False,
    admitted_images=None,
    owned_observations=None,
) -> dict[str, object]:
    stage = "invalid_config" if invalid else operation
    sessions = Path(env["TEMP"])
    state = {
        "expected_job_total": 5 if invalid else None,
        "launcher_requested": False,
        "launcher_returned": False,
    }
    contract_require(
        not any(sessions.iterdir()),
        stage,
        "sessions_empty",
        observations={**state, "sessions_empty": False},
        code="cleanup_incomplete",
    )
    observation, initialization = None, None
    owned, summary, original, child_detail = None, None, None, None

    def snapshot():
        result = {**({} if summary is None else summary.observations(invalid=invalid)), **state}
        process = None if owned is None else owned.process
        if process is not None:
            result["relay_exit_code"] = observed_integer(process.returncode, signed=True)
            if result["relay_exit_code"] is not None:
                state["relay_exit_code"] = result["relay_exit_code"]
        detail = child_detail
        if detail is None and owned is not None:
            stderr = getattr(owned, "stderr_summary", None)
            if stderr is not None:
                detail = {
                    "gate_state": stderr.gate_state,
                    "child_exit_code": stderr.child_exit_code,
                }
        if detail is not None:
            result["gate_state"] = detail.get("gate_state")
            result["child_exit_code"] = observed_integer(detail.get("child_exit_code"), signed=True)
            result["timed_out"] = detail.get("timed_out", False)
            if detail.get("gate_state") == "exited":
                result["launcher_returned"] = True
        return result

    try:
        if invalid:
            try:
                common.require(
                    type(admitted_images) is dict and type(owned_observations) is list,
                    "admission_failed",
                )
                observation = boundary.PreflightObservation(
                    common, temp_root=sessions, admitted_images=admitted_images
                )
                # Register before start: even a partially initialized observer must
                # prevent outer file cleanup until all its readers have stopped.
                owned_observations.append(observation)
                observation.start()
            except BaseException as error:
                emit_observation_failure(error, "setup")
                raise contextual_error(
                    error, stage, "invalid_observation_setup", snapshot()
                ) from None
        state["launcher_requested"] = True
        owned = common.OwnedProcess(
            command,
            cwd=work,
            env=env,
            operation=operation,
            stdout=subprocess.PIPE,
            **({"job_factory": observation.job_factory} if invalid else {}),
        )
        summary = LaunchSummary(owned.process.stdout)
        try:
            owned.wait(60 if invalid else 150)
        except common.WitnessError as error:
            child_detail = error.diagnostic or {}
            if not (
                invalid
                and str(error) == "child_failed"
                and child_detail.get("child_exit_code") == 23
                and child_detail.get("gate_state") == "exited"
            ):
                raise
        state["launcher_returned"] = True
        summary.finish()
        total, active = job_accounting(owned)
        state.update(job_total=observed_integer(total), job_active=observed_integer(active))
        contract_require(
            active == 0, stage, "job_active", observations=snapshot(), code="cleanup_incomplete"
        )
        state["sessions_empty"] = not any(sessions.iterdir())
        contract_require(
            state["sessions_empty"],
            stage,
            "sessions_empty",
            observations=snapshot(),
            code="cleanup_incomplete",
        )
        if invalid:
            contract_require(
                not summary.invalid, stage, "invalid_output_limit", observations=snapshot()
            )
            contract_require(
                summary.counts
                == {
                    "admitted": 0,
                    "synthetic": 0,
                    "analytics": 0,
                    "operator": 0,
                    "exit": 0,
                    "refusal": 1,
                },
                stage,
                "invalid_markers",
                observations=snapshot(),
            )
            contract_require(
                owned.process.returncode == 23, stage, "invalid_exit", observations=snapshot()
            )
            contract_require(
                child_detail.get("child_exit_code") == 23
                and child_detail.get("relay_exit_code") == 23
                and child_detail.get("gate_state") == "exited"
                and child_detail.get("timed_out") is False,
                stage,
                "invalid_exit",
                observations=snapshot(),
            )
            contract_require(
                not summary.health_confirmed and not summary.operator_request_observed,
                stage,
                "invalid_milestones",
                observations=snapshot(),
            )
            # Success is deferred until the owned Job closes and the complete
            # qualified process/TEMP observation drains and validates below.
        else:
            return summary.result(stage=stage)
    except BaseException as error:
        original = contextual_error(error, stage, "child_process", snapshot())
        raise original from None
    finally:
        cleanup_errors = []
        if owned is not None:
            try:
                common.close_after_failure(owned, original)
            except BaseException as error:
                cleanup_errors.append(
                    contextual_error(error, "cleanup", "owned_process_cleanup", snapshot())
                )
        if summary is not None:
            try:
                summary.finish()
            except BaseException as error:
                cleanup_errors.append(
                    contextual_error(error, "cleanup", "collector_cleanup", snapshot())
                )
        if summary is not None and not summary.thread.is_alive():
            try:
                summary.emit_start_diagnostics(stage)
            except BaseException as error:
                cleanup_errors.append(
                    contextual_error(error, "cleanup", "startup_diagnostic_projection", snapshot())
                )
        if observation is not None:
            try:
                if (
                    observation.started
                    and len(observation.jobs) == 1
                    and not observation.jobs[0].observation_open
                ):
                    initialization = observation.finish()
                    boundary.validate_summary(initialization)
                    print(
                        "K5_OWNED_PREFLIGHT_INITIALIZATION="
                        + common.canonical(initialization).decode("ascii")
                    )
            except BaseException as error:
                emit_observation_failure(error, "finish")
                cleanup_errors.append(
                    AlphaWitnessError(
                        alpha_diagnostic(stage, "invalid_initialization", observations=snapshot())
                    )
                )
            finally:
                try:
                    observation.close()
                except BaseException as error:
                    emit_observation_failure(error, "cleanup")
                    cleanup_errors.append(
                        AlphaWitnessError(
                            alpha_diagnostic(
                                "cleanup",
                                "invalid_observation_cleanup",
                                failure_code="cleanup_incomplete",
                                observations=snapshot(),
                            )
                        )
                    )
        if cleanup_errors:
            # Report each fixed failure once; cleanup never overwrites the cause.
            if original is not None:
                emit_alpha_diagnostic(original.alpha_diagnostic)
            for error in cleanup_errors[:-1]:
                emit_alpha_diagnostic(error.alpha_diagnostic)
            raise cleanup_errors[-1] from None

    try:
        boundary.validate_invalid_initialization(initialization)
    except BaseException as error:
        emit_observation_failure(error, "validate")
        raise AlphaWitnessError(
            alpha_diagnostic(stage, "invalid_initialization", observations=snapshot())
        ) from None
    return {
        "invalid_config_refused": True,
        "invalid_config_no_session": True,
        "invalid_config_no_media": True,
    }


def admitted_preflight_images(env: dict[str, str], installed: Path, powershell: Path):
    paths = {
        "base_python": common.admitted_gate_python(env),
        "venv_python": installed / ".venv/Scripts/python.exe",
        "powershell": powershell,
        "console_host": Path(env["SYSTEMROOT"]) / "System32/conhost.exe",
    }
    return {
        kind: (common.local_path(path), common.file_hash(common.local_path(path)))
        for kind, path in paths.items()
    }


def require_ports_free(port: int) -> None:
    for number in (port, 8554):
        common.require_free_port(number)
    for number in (18000, 18001):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as listener:
            try:
                listener.bind(("127.0.0.1", number))
            except OSError:
                raise common.WitnessError("port_occupied") from None


def launch_sequence(
    *,
    work: Path,
    installed: Path,
    env: dict[str, str],
    powershell: Path,
    expected: dict[str, str],
    document: dict[str, object],
    owned_observations: list,
) -> None:
    envelope = work / "invoke-start.ps1"
    envelope.write_text(
        bind_start_envelope(expected["start_script_sha256"]), encoding="ascii", newline="\n"
    )
    invalid_config = work / "invalid-analytics.json"
    invalid_config.write_bytes(b'{"schema_version":1,"provider":"invalid-selected-provider"}')
    port = common.free_port()
    command = start_command(powershell, envelope, installed, port)
    document["stage"] = "invalid_config"
    verify_layout(installed, expected)
    document.update(
        invoke_start(
            command,
            work=work,
            env={**env, "K5_ANALYTICS_CONFIG": str(invalid_config)},
            operation="probe_admission",
            invalid=True,
            admitted_images=admitted_preflight_images(env, installed, powershell),
            owned_observations=owned_observations,
        )
    )
    for attempt in (1, 2):
        document["stage"] = f"launch_{attempt}"
        verify_layout(installed, expected)
        common.require(
            common.digest(native_manifest(Path(env["LOCALAPPDATA"])))
            == expected["native_cache_sha256"]
        )
        require_ports_free(port)
        result = invoke_start(command, work=work, env=env, operation=f"launch_{attempt}")
        document.update({f"run_{attempt}_{key}": value for key, value in result.items()})
        require_ports_free(port)


def verify_native(local: Path, work: Path, env: dict[str, str]) -> None:
    gst, mtx = native_roots(local)
    for command, version, operation in (
        (
            [str(gst / "bin/gst-inspect-1.0.exe"), "--version"],
            GSTREAMER_VERSION,
            "inspect_gstreamer_version",
        ),
        ([str(mtx / "mediamtx.exe"), "--version"], "1.21.1", "inspect_mediamtx_version"),
    ):
        raw = common.capture(command, cwd=work, env=env, operation=operation, limit=4096)
        common.require(
            re.search(
                rb"(?<![0-9.])" + version.encode().replace(b".", rb"\.") + rb"(?![0-9.])", raw
            )
            is not None
        )
    elements = {
        **common.GSTREAMER_ELEMENTS,
        "videotestsrc": ("videotestsrc", "gst-plugins-base", "LGPL"),
        "queue": ("coreelements", "gstreamer", "LGPL"),
        "fakesink": ("coreelements", "gstreamer", "LGPL"),
    }
    for element, expected in elements.items():
        raw = common.capture(
            [str(gst / "bin/gst-inspect-1.0.exe"), element],
            cwd=work,
            env=env,
            operation="probe_admission",
        )
        common.inspect_plugin(raw, expected, gst)


def prepare(args, work: Path, expected: dict[str, str], document: dict[str, object]):
    env = clean_environment(dict(os.environ), work)
    for key in ("TEMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA"):
        Path(env[key]).mkdir(parents=True, exist_ok=True)
    source, donor = work / "source", work / "analytics-source"
    document["stage"] = "build"
    for repository, revision, target, operation in (
        (args.repo, expected["revision"], source, "archive_candidate"),
        (args.analytics_source, common.ANALYTICS_REVISION, donor, "archive_analytics"),
    ):
        archive = work / (target.name + ".zip")
        command = common.archive_command(repository, archive, revision)
        command[0] = str(common.local_path(args.git.absolute()))
        common.run(command, cwd=work, env=env, operation=operation)
        common.extract_archive(archive, target)
    common.require(common.digest(tree_manifest(source)) == expected["source_tree_sha256"])
    # Controller and reused helper must themselves be exact candidate bytes.
    for name in (
        Path(__file__).name,
        "installed_analytics_witness.py",
        "windows_owned_preflight.py",
    ):
        common.require(
            (source / "scripts" / name).read_bytes() == Path(__file__).with_name(name).read_bytes()
        )
    payload = source_payload(source)
    common.require(common.digest(payload) == expected["k5_payload_sha256"])
    wheel = common.local_path(args.k5_wheel.absolute())
    common.require(
        common.file_hash(wheel) == expected["k5_wheel_sha256"]
        and common.wheel_payload(wheel) == payload
    )
    common.require(
        common.file_hash(source / "src/k5vision/data/analytics-runtime-manifest.json")
        == expected["analytics_manifest_sha256"]
    )
    wheelhouse = common.local_path(args.wheelhouse.absolute(), directory=True)
    common.require(
        common.digest(tree_manifest(wheelhouse, maximum_files=256)) == expected["wheelhouse_sha256"]
    )
    local = Path(env["LOCALAPPDATA"])
    copy_native_cache(
        common.local_path(args.local_appdata.absolute(), directory=True),
        local,
        expected["native_cache_sha256"],
    )
    gst, _ = native_roots(local)
    env.update(
        K5_GSTREAMER_ROOT=str(gst),
        GST_REGISTRY_1_0=str(work / "gst-registry.bin"),
        GST_PLUGIN_PATH_1_0="",
        GST_PLUGIN_PATH="",
        GST_PLUGIN_SYSTEM_PATH_1_0=str(gst / "lib/gstreamer-1.0"),
        GST_PLUGIN_SYSTEM_PATH=str(gst / "lib/gstreamer-1.0"),
        GIO_USE_PROXY_RESOLVER="dummy",
        GIO_MODULE_DIR=str(work / "gio-modules"),
        PATH=str(gst / "bin") + os.pathsep + env["PATH"],
    )
    (work / "gio-modules").mkdir()
    installed = work / "installed"
    installed.mkdir()
    identities = install_scripts(source, installed, expected["revision"])
    common.require(all(expected[name] == value for name, value in identities.items()))
    python = installed / ".venv/Scripts/python.exe"
    common.run(
        [sys.executable, "-I", "-B", "-m", "venv", str(installed / ".venv")],
        cwd=work,
        env=env,
        operation="create_venv",
    )
    wheels = work / "wheels"
    for output, operation in (
        (wheels, "build_analytics_wheel"),
        (work / "repeat", "rebuild_analytics_wheel"),
    ):
        common.run(
            [
                str(python),
                "-I",
                "-B",
                str(source / "scripts/build_analytics_runtime_wheel.py"),
                "--source-root",
                str(donor),
                "--output-dir",
                str(output),
            ],
            cwd=work,
            env=env,
            operation=operation,
        )
    analytics_wheels = list(wheels.glob("*.whl"))
    common.require(len(analytics_wheels) == 1)
    analytics_wheel = analytics_wheels[0]
    common.require(
        common.file_hash(analytics_wheel)
        == expected["analytics_wheel_sha256"]
        == common.file_hash(work / "repeat" / analytics_wheel.name)
    )
    versions = {
        **common.requirements(source / "scripts/windows-alpha/runtime-requirements.txt"),
        **common.RUNTIME_VERSIONS,
        **common.WINDOWS_RUNTIME_VERSIONS,
    }
    document["stage"] = "install"
    common.run(
        [
            str(python),
            "-I",
            "-B",
            "-m",
            "pip",
            "install",
            "--no-index",
            "--no-deps",
            "--only-binary=:all:",
            "--no-compile",
            "--find-links",
            str(wheelhouse),
            *[f"{name}=={version}" for name, version in sorted(versions.items())],
        ],
        cwd=work,
        env=env,
        seconds=300,
        operation="install_dependencies",
    )
    common.run(
        [
            str(python),
            "-I",
            "-B",
            "-m",
            "pip",
            "install",
            "--no-index",
            "--no-deps",
            "--no-compile",
            str(wheel),
            str(analytics_wheel),
        ],
        cwd=work,
        env=env,
        operation="install_local_wheels",
    )
    common.run(
        [str(python), "-I", "-B", "-m", "pip", "check"],
        cwd=work,
        env=env,
        operation="check_dependencies",
    )
    config = work / "analytics.json"
    root = common.local_path(args.evidence_root.absolute(), directory=True)
    config.write_bytes(
        common.canonical(
            {
                "schema_version": 1,
                "provider": "analytics-lab-omz-person-v1",
                "source_revision": common.ANALYTICS_REVISION,
                "artifact_root": str(root / "artifacts"),
            }
        )
    )
    env["K5_ANALYTICS_CONFIG"] = str(config)
    inputs = work / "probe-inputs.json"
    inputs.write_bytes(
        common.canonical(
            {"k5_payload": payload, "runtime_versions": versions, "evidence_root": str(root)}
        )
    )
    # Run installed admission from a stdlib-only helper outside the archive/cwd.
    driver = work / "installed_analytics_witness.py"
    shutil.copyfile(source / "scripts/installed_analytics_witness.py", driver)
    command = [
        str(python),
        "-I",
        "-B",
        str(driver),
        "--probe",
        "--inputs",
        str(inputs),
        "--output",
        str(work / "probe-output.json"),
    ]
    return installed, env, command


def probe(
    command: list[str], work: Path, env: dict[str, str], expected: dict[str, str], *, after=False
):
    output = work / "probe-output.json"
    output.unlink(missing_ok=True)
    common.run(
        command,
        cwd=work,
        env=env,
        operation="probe_after" if after else "probe_before",
        seconds=120,
    )
    value = common.read_json(output)
    common.require(
        value.keys() == {"runtime_identity_sha256", "model_identity_sha256", "seed_identity_sha256"}
    )
    for name, identity in value.items():
        common.require(
            type(identity) is str
            and re.fullmatch(r"[0-9a-f]{64}", identity) is not None
            and identity != "0" * 64
        )
        if name != "runtime_identity_sha256" or after:
            common.require(identity == expected[name])
    return value


def clear_output(path: Path, name: str) -> Path:
    output = path.absolute()
    common.require(output.name == name, "admission_failed")
    common.local_path(output.parent, directory=True)
    if output.exists() or output.is_symlink():
        common.local_path(output)
        output.unlink()
    return output


def admit_platform() -> None:
    # This controller deliberately starts from the admitted base runtime. The
    # shared helper also independently binds its no-site relay to that runtime.
    common.require(
        os.name == "nt"
        and sys.version_info[:2] == (3, 12)
        and sys.prefix == sys.base_prefix
        and sys.flags.isolated == 1
        and sys.flags.dont_write_bytecode == 1
        and os.environ.get("PROCESSOR_ARCHITECTURE", "").upper() == "AMD64",
        "admission_failed",
    )


def execute(args) -> int:
    output = clear_output(args.output, RECEIPT_NAME)
    admitted = clear_output(args.admitted_expectations, EXPECTATIONS_NAME)
    expected = validate_expectations(common.read_json(args.expectations), installed=False)
    document = new_receipt(expected)
    work = None
    detail = None
    alpha_detail = None
    owned_observations = []
    try:
        admit_platform()
        repo = common.local_path(args.repo.absolute(), directory=True)
        work = common.adopt_work_root(args.work_root, args.temp_root, repo, output)
        common.require(not admitted.is_relative_to(work), "admission_failed")
        installed, env, command = prepare(args, work, expected, document)
        document["stage"] = "probe"
        checked = probe(command, work, env, expected)
        expected = {**expected, "runtime_identity_sha256": checked["runtime_identity_sha256"]}
        document.update(checked)
        validate_expectations(expected)
        with admitted.open("xb") as stream:
            stream.write(common.canonical(expected) + b"\n")
        verify_native(Path(env["LOCALAPPDATA"]), work, env)
        powershell = common.local_path(
            Path(env["SYSTEMROOT"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        )
        launch_sequence(
            work=work,
            installed=installed,
            env=env,
            powershell=powershell,
            expected=expected,
            document=document,
            owned_observations=owned_observations,
        )
        document["stage"] = "verify"
        probe(command, work, env, expected, after=True)
        verify_layout(installed, expected)
        common.require(
            common.digest(native_manifest(Path(env["LOCALAPPDATA"])))
            == expected["native_cache_sha256"]
        )
        common.require(
            common.digest(tree_manifest(args.wheelhouse.absolute(), maximum_files=256))
            == expected["wheelhouse_sha256"]
        )
        document["completed"] = True
    except BaseException as error:
        failure = contextual_error(error, document["stage"], "driver_contract")
        alpha_detail = failure.alpha_diagnostic
        document["failure_code"] = (
            str(error) if isinstance(error, common.WitnessError) else "unexpected"
        )
        detail = error.diagnostic if isinstance(error, common.WitnessError) else None
        detail = detail or common.diagnostic("driver_admission", category=document["failure_code"])
    finally:
        cleanup = True
        if work is not None:
            try:
                if all(observation.quiescent() for observation in owned_observations):
                    shutil.rmtree(work)
                    cleanup = not work.exists()
                else:
                    cleanup = False
            except (OSError, boundary.ObservationFailure):
                cleanup = False
        document["cleanup_complete"] = cleanup
        if not cleanup:
            if alpha_detail is not None:
                emit_alpha_diagnostic(alpha_detail)
            document.update(completed=False, failure_code="cleanup_incomplete", stage="cleanup")
            alpha_detail = alpha_diagnostic(
                "cleanup", "owned_layout_cleanup", failure_code="cleanup_incomplete"
            )
            detail = common.diagnostic(
                "cleanup_owned", outcome="cleanup_failed", category="cleanup_incomplete"
            )
        if document["completed"]:
            document["stage"] = "complete"
            validate_receipt(document, expected)
            try:
                with output.open("xb") as stream:
                    stream.write(common.canonical(document) + b"\n")
            except BaseException as error:
                raise contextual_error(error, "final_receipt", "receipt_write") from None
        # Failure is a separate fixed diagnostic, never a partly populated success
        # file or an exception/path/native stream published as acceptance evidence.
        elif detail is not None:
            if alpha_detail is not None:
                emit_alpha_diagnostic(alpha_detail)
            common.emit_diagnostic(detail)
    print(
        "Installed Alpha Start-script witness passed"
        if document["completed"]
        else "Installed Alpha Start-script witness failed closed"
    )
    return 0 if document["completed"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--validate-receipt", action="store_true")
    parser.add_argument("--expectations", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--admitted-expectations", type=Path)
    for name in (
        "repo",
        "analytics-source",
        "k5-wheel",
        "wheelhouse",
        "evidence-root",
        "local-appdata",
        "git",
        "temp-root",
        "work-root",
    ):
        parser.add_argument("--" + name, type=Path)
    args = parser.parse_args()
    try:
        if args.validate_receipt:
            contract_require(args.output.name == RECEIPT_NAME, "final_receipt", "receipt_schema")
            validate_receipt(common.read_json(args.output), common.read_json(args.expectations))
            print("Installed Alpha Start-script receipt validation passed")
            return 0
        # Also remove stale success on malformed/missing execute inputs. Validation
        # mode never deletes either input. Refuse aliases before touching output.
        clear_output(args.output, RECEIPT_NAME)
        common.require(
            all(
                getattr(args, name) is not None
                for name in (
                    "repo",
                    "analytics_source",
                    "k5_wheel",
                    "wheelhouse",
                    "evidence_root",
                    "local_appdata",
                    "git",
                    "temp_root",
                    "work_root",
                    "admitted_expectations",
                )
            ),
            "admission_failed",
        )
        return execute(args)
    except BaseException as error:
        failure = contextual_error(
            error, "final_receipt" if args.validate_receipt else "admission", "driver_contract"
        )
        emit_alpha_diagnostic(failure.alpha_diagnostic)
        code = str(error) if isinstance(error, common.WitnessError) else "unexpected"
        common.emit_diagnostic(
            common.diagnostic(
                "receipt_validate" if args.validate_receipt else "driver_admission", category=code
            )
        )
        print("Installed Alpha Start-script witness failed closed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
