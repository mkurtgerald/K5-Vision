[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

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

    $failure = "inventory_unavailable"
    $processes = @(Get-CimInstance -ClassName Win32_Process -OperationTimeoutSec 10 -ErrorAction Stop)
    $workers = @($processes | Where-Object { [string]$_.Name -ieq "Runner.Worker.exe" })
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
}

Write-Output "Stage One physical admission passed for this observation only."
