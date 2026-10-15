[CmdletBinding()]
param($DiagnosticContext = $null)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

# Diagnostic-only projection. It cannot grant admission or change a refusal.
function Write-K5NameObservation {
    try {
        if ($null -eq $DiagnosticContext) { return }
        if ($DiagnosticContext -isnot [hashtable] -or $DiagnosticContext.Count -ne 7) { throw "context" }
        foreach ($key in @("repository", "source_sha", "source_tree", "producer_sha256", "run_id", "run_attempt", "phase")) {
            if (-not $DiagnosticContext.ContainsKey($key) -or $DiagnosticContext[$key] -isnot [string]) { throw "context" }
        }
        if ($DiagnosticContext.repository -cne "mkurtgerald/K5-Vision" -or
            $DiagnosticContext.repository -cne $env:GITHUB_REPOSITORY -or
            $DiagnosticContext.source_sha -cnotmatch '\A[0-9a-f]{40}\z' -or
            $DiagnosticContext.source_sha -cne $env:K5_STAGE_ONE_REVISION -or
            $DiagnosticContext.source_sha -cne $env:GITHUB_SHA -or
            $DiagnosticContext.source_tree -cnotmatch '\A[0-9a-f]{40}\z' -or
            $DiagnosticContext.producer_sha256 -cnotmatch '\A[0-9a-f]{64}\z' -or
            $DiagnosticContext.run_id -cnotmatch '\A[1-9][0-9]{0,19}\z' -or
            $DiagnosticContext.run_id -cne $env:GITHUB_RUN_ID -or
            $DiagnosticContext.run_attempt -cnotmatch '\A[1-9][0-9]{0,19}\z' -or
            $DiagnosticContext.run_attempt -cne $env:GITHUB_RUN_ATTEMPT -or
            $DiagnosticContext.phase -cne "pre_storage") { throw "context" }
        if ($snapshotEnd -lt $snapshotStart) { throw "clock" }
        $namesComplete = $snapshotComplete -and $processes.Count -le 32768
        $count = 0
        for ($index = 0; $index -lt [Math]::Min($processes.Count, 32768); $index++) {
            try {
                $name = $processes[$index].Name
                if ($name -isnot [string] -or $name.Length -eq 0 -or $name.Length -gt 260 -or $name.IndexOf([char]0) -ge 0 -or $name -match '[\\/:\r\n]') {
                    $namesComplete = $false
                } elseif ($name -ieq "crashpad_handler.exe") {
                    $count++
                }
            } catch { $namesComplete = $false }
        }
        $presence = "unknown"
        $countKind = "unavailable"
        $reportedCount = $null
        if ($namesComplete) {
            $presence = "absent"
            $countKind = "exact"
            $reportedCount = $count
        }
        if ($count -gt 0) {
            $presence = "present"
            $reportedCount = $count
            if (-not $namesComplete) { $countKind = "lower_bound" }
        }
        $record = [ordered]@{
            schema_version = "shared-host-name-observation-v1"
            scope = "diagnostic-only-name-presence"
            repository = $DiagnosticContext.repository
            source_sha = $DiagnosticContext.source_sha
            source_tree = $DiagnosticContext.source_tree
            producer_sha256 = $DiagnosticContext.producer_sha256
            run_id = $DiagnosticContext.run_id
            run_attempt = $DiagnosticContext.run_attempt
            phase = "pre_storage"
            host = $(if ($hostVerified) { "VLR-CYZ4PK3" } else { "unknown" })
            runner = $(if ($hostVerified) { "K5-Physical" } else { "unknown" })
            worker_count = $diagnosticWorkerCount
            owned_ancestry = $diagnosticOwned
            provider_complete = $snapshotComplete
            names_complete = $namesComplete
            target_family = "crashpad_handler"
            presence = $presence
            count = $reportedCount
            count_kind = $countKind
            snapshot_start_utc = $snapshotStart.ToString("o")
            snapshot_end_utc = $snapshotEnd.ToString("o")
            clock_domain = "runner_utc_unverified"
            requires_exact_actions_step_binding = $true
            edge_admission = $false
            retry_authority = $false
        }
        Write-Host ("K5_HOST_NAME_OBSERVATION=" + ($record | ConvertTo-Json -Compress -Depth 4))
    } catch {
        Write-Host "K5_HOST_NAME_OBSERVATION_UNAVAILABLE"
    }
}

$processes = @()
$snapshotComplete = $false
$hostVerified = $false
$diagnosticWorkerCount = $null
$diagnosticOwned = $null
$snapshotStart = [DateTime]::UtcNow
$snapshotEnd = $snapshotStart

# This is a fresh occupancy observation, not a cross-repository lock. A separately
# serialized launch decision must keep other jobs off this shared physical host.
# Never signal an existing process or release a port to make qualification pass.
$failure = "inventory_unavailable"
try {
    $failure = "host_identity"
    if ($env:OS -cne "Windows_NT" -or
        [Environment]::MachineName -cne "VLR-CYZ4PK3" -or
        [string]$env:RUNNER_NAME -cne "K5-Physical") {
        throw "admission"
    }

    $hostVerified = $true
    $failure = "inventory_unavailable"
    $snapshotStart = [DateTime]::UtcNow
    try {
        $processes = @(Get-CimInstance -ClassName Win32_Process -OperationTimeoutSec 10 -ErrorAction Stop -Property Name, ProcessId, ParentProcessId, CreationDate)
        $snapshotComplete = $true
    } finally { $snapshotEnd = [DateTime]::UtcNow }
    $workers = @($processes | Where-Object { [string]$_.Name -ieq "Runner.Worker.exe" })
    if ($workers.Count -le 32768) { $diagnosticWorkerCount = $workers.Count }
    $failure = "worker_ownership"
    if ($workers.Count -ne 1) { throw "admission" }
    $byId = @{}
    foreach ($process in $processes) {
        $processId = [int]$process.ProcessId
        if ($processId -le 0) { continue }
        if ($byId.ContainsKey($processId)) { throw "admission" }
        $byId[$processId] = $process
    }
    $currentId = [int]$PID
    $seen = @{}
    $owned = $false
    for ($depth = 0; $depth -lt 32; $depth++) {
        if ($seen.ContainsKey($currentId) -or -not $byId.ContainsKey($currentId)) {
            throw "admission"
        }
        $seen[$currentId] = $true
        $current = $byId[$currentId]
        if ($null -eq $current.CreationDate) { throw "admission" }
        if ($currentId -eq [int]$workers[0].ProcessId) {
            $owned = $true
            break
        }
        $parentId = [int]$current.ParentProcessId
        if (-not $byId.ContainsKey($parentId)) { throw "admission" }
        $parent = $byId[$parentId]
        if ($null -eq $parent.CreationDate -or $parent.CreationDate -gt $current.CreationDate) {
            throw "admission"
        }
        $currentId = $parentId
    }
    $diagnosticOwned = $owned
    if (-not $owned) { throw "admission" }

    $failure = "runtime_occupied"
    $nativeNames = '^(EdgeVMS-Gate001|edgevms-deviceops|mediamtx|gst-launch-1\.0|ffmpeg|python|pythonw|k5-vision)\.exe$'
    if (@($processes | Where-Object { [string]$_.Name -match $nativeNames }).Count -ne 0) {
        throw "admission"
    }

    $failure = "inventory_unavailable"
    $tcp = @(Get-NetTCPConnection -State Listen -ErrorAction Stop)
    $udp = @(Get-NetUDPEndpoint -ErrorAction Stop)
    $failure = "endpoint_occupied"
    $tcpPorts = @(8000, 8554, 8780, 8781, 8888, 8889, 9996, 9997, 9998)
    if (@($tcp | Where-Object { [int]$_.LocalPort -in $tcpPorts }).Count -ne 0 -or
        @($udp | Where-Object { [int]$_.LocalPort -eq 8189 }).Count -ne 0) {
        throw "admission"
    }
} catch {
    throw "Stage One physical admission refused: $failure. Existing owners were preserved."
} finally {
    # Output failure must never mask the original guard outcome.
    try { Write-K5NameObservation } catch {}
}

Write-Output "Stage One physical admission passed for this observation only."
