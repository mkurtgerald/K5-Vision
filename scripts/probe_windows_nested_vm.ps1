# Diagnostic only: observe a running disposable guest interval, not a Windows
# installation or product boot qualification. The outer runner is never rebooted.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$VerbosePreference = 'SilentlyContinue'
$WarningPreference = 'SilentlyContinue'
$InformationPreference = 'SilentlyContinue'

$receipt = [ordered]@{
    schema_version = 1
    status = 'diagnostic_complete'
    scope = 'disposable_diskless_nested_guest_capacity'
    outcome = 'unavailable'
    os_build = [Environment]::OSVersion.Version.Build
    hyperv_module_available = $false
    hyperv_feature = 'unknown'
    vmms_service = 'unknown'
    machine_hypervisor_present = $null
    firmware_virtualization_enabled = $null
    guest_created = $false
    guest_vm_id_bound = $false
    guest_generation = 2
    guest_startup_memory_bytes = 536870912
    guest_network_adapters = $null
    guest_virtual_disks = $null
    start_completed = $false
    running_observed = $false
    uptime_delta_ms = 0
    positive_cpu_sample_observed = $false
    running_guest_interval_observed = $false
    cleanup_complete = $true
    failure_stage = 'none'
    failure_hresult = 0
    failure_native_code = 0
    cleanup_hresult = 0
    cleanup_native_code = 0
}

function Get-FiniteErrorCodes([Exception] $Exception) {
    $current = $Exception
    $native = 0
    for ($depth = 0; $null -ne $current -and $depth -lt 8; $depth++) {
        if ($current -is [System.ComponentModel.Win32Exception]) {
            $native = $current.NativeErrorCode
            break
        }
        $current = $current.InnerException
    }
    return @([int] $Exception.HResult, [int] $native)
}

function Complete-BoundedJob($Job, [int] $Seconds) {
    try {
        $finished = Wait-Job -Job $Job -Timeout $Seconds
        if ($null -eq $finished) {
            throw [TimeoutException]::new('Bounded diagnostic operation expired')
        }
        Receive-Job -Job $Job -ErrorAction Stop
    }
    finally {
        if ($Job.State -in @('Running', 'NotStarted', 'Blocked')) {
            Stop-Job -Job $Job -ErrorAction SilentlyContinue
        }
        Remove-Job -Job $Job -Force -ErrorAction SilentlyContinue
    }
}

function Get-OwnedVm([Guid] $Id, [string] $Name, [string] $Root) {
    $found = Get-VM -Id $Id -ErrorAction Stop
    $configuration = [IO.Path]::GetFullPath($found.ConfigurationLocation)
    $prefix = $Root.TrimEnd('\') + '\'
    if ($found.Id -ne $Id -or $found.Name -cne $Name -or
        -not ($configuration.Equals($Root, [StringComparison]::OrdinalIgnoreCase) -or
                  $configuration.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase))) {
        throw [InvalidOperationException]::new('Disposable VM identity changed')
    }
    return $found
}

function Remove-EmptyOwnedTree([IO.DirectoryInfo] $Directory) {
    # Remove-VM owns deletion of that exact VM's configuration files. Refuse to
    # follow links or delete any unexpected remaining file during cleanup.
    $Directory.Refresh()
    if (($Directory.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw [InvalidOperationException]::new('Disposable directory became linked')
    }
    foreach ($entry in $Directory.EnumerateFileSystemInfos()) {
        if (($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0 -or
            -not ($entry -is [IO.DirectoryInfo])) {
            throw [InvalidOperationException]::new('Unexpected disposable directory entry')
        }
        Remove-EmptyOwnedTree $entry
    }
    $Directory.Delete($false)
}

$phase = 'feature_discovery'
$vmId = $null
$ownedRoot = $null
$ownedRootCreated = $false
$vmName = 'K5Capability-' + [Guid]::NewGuid().ToString('N')
try {
    $receipt.hyperv_module_available = @(
        Get-Module -ListAvailable -Name Hyper-V
    ).Count -gt 0
    if (-not $receipt.hyperv_module_available) {
        throw [NotSupportedException]::new('Hyper-V module is unavailable')
    }
    Import-Module Hyper-V -ErrorAction Stop
    if (Get-Command Get-WindowsFeature -ErrorAction SilentlyContinue) {
        $feature = Get-WindowsFeature -Name Hyper-V
        $featureState = [string] $feature.InstallState
        if ($featureState -in @('Installed', 'Available', 'Removed')) {
            $receipt.hyperv_feature = $featureState.ToLowerInvariant()
        }
    }
    $vmms = Get-Service -Name vmms -ErrorAction SilentlyContinue
    if ($null -eq $vmms) {
        $receipt.vmms_service = 'absent'
    }
    elseif ([string] $vmms.Status -in @('Running', 'Stopped', 'StartPending', 'StopPending')) {
        $receipt.vmms_service = ([string] $vmms.Status).ToLowerInvariant()
    }
    try {
        $computer = Get-CimInstance -ClassName Win32_ComputerSystem -OperationTimeoutSec 3
        $receipt.machine_hypervisor_present = [bool] $computer.HypervisorPresent
        $processors = @(Get-CimInstance -ClassName Win32_Processor -OperationTimeoutSec 3)
        if ($processors.Count -gt 0) {
            $receipt.firmware_virtualization_enabled = @(
                $processors | Where-Object { $_.VirtualizationFirmwareEnabled }
            ).Count -eq $processors.Count
        }
    }
    catch {
        # Optional read-only host metadata never substitutes for starting a VM.
    }

    $phase = 'create'
    $ownedRoot = [IO.Path]::GetFullPath(
        [IO.Path]::Combine([IO.Path]::GetTempPath(), $vmName)
    )
    if (Test-Path -LiteralPath $ownedRoot) {
        throw [InvalidOperationException]::new('Disposable directory already exists')
    }
    $rootInfo = New-Item -ItemType Directory -Path $ownedRoot
    $ownedRootCreated = $true
    $vm = New-VM -Name $vmName -Generation 2 -MemoryStartupBytes 512MB -NoVHD -Path $ownedRoot
    $vmId = [Guid] $vm.Id
    $receipt.guest_created = $true
    $vm = Get-OwnedVm $vmId $vmName $ownedRoot
    $receipt.guest_vm_id_bound = $true

    $phase = 'isolate'
    # New-VM may create a disconnected adapter even without a SwitchName.
    Get-VMNetworkAdapter -VM $vm | Remove-VMNetworkAdapter -Confirm:$false
    $receipt.guest_network_adapters = @(Get-VMNetworkAdapter -VM $vm).Count
    $receipt.guest_virtual_disks = @(Get-VMHardDiskDrive -VM $vm).Count
    if ($receipt.guest_network_adapters -ne 0 -or $receipt.guest_virtual_disks -ne 0 -or
        $vm.Generation -ne 2 -or $vm.MemoryStartup -ne 536870912) {
        throw [InvalidOperationException]::new('Disposable VM isolation was refused')
    }

    $phase = 'start'
    $startJob = Start-VM -VM $vm -AsJob
    Complete-BoundedJob $startJob 20 | Out-Null
    $receipt.start_completed = $true

    $phase = 'observe'
    $watch = [Diagnostics.Stopwatch]::StartNew()
    $firstUptime = $null
    while ($watch.Elapsed.TotalSeconds -lt 8) {
        $observed = Get-OwnedVm $vmId $vmName $ownedRoot
        if ([string] $observed.State -eq 'Running') {
            $receipt.running_observed = $true
            $uptime = [long] $observed.Uptime.TotalMilliseconds
            if ($null -eq $firstUptime) { $firstUptime = $uptime }
            $receipt.uptime_delta_ms = [Math]::Max(0, $uptime - $firstUptime)
            if ($observed.CPUUsage -gt 0) { $receipt.positive_cpu_sample_observed = $true }
            if ($receipt.uptime_delta_ms -ge 1000) {
                $receipt.running_guest_interval_observed = $true
                $receipt.outcome = 'guest_running'
                break
            }
        }
        [Threading.Thread]::Sleep(200)
    }
    if (-not $receipt.running_guest_interval_observed) {
        throw [TimeoutException]::new('No bounded running guest interval observed')
    }
}
catch {
    $codes = Get-FiniteErrorCodes $_.Exception
    $receipt.failure_stage = $phase
    $receipt.failure_hresult = $codes[0]
    $receipt.failure_native_code = $codes[1]
}
finally {
    try {
        if ($null -ne $vmId) {
            $ownedVm = Get-OwnedVm $vmId $vmName $ownedRoot
            if ([string] $ownedVm.State -ne 'Off') {
                $stopJob = Stop-VM -VM $ownedVm -TurnOff -Force -AsJob -Confirm:$false
                Complete-BoundedJob $stopJob 15 | Out-Null
            }
            $ownedVm = Get-OwnedVm $vmId $vmName $ownedRoot
            if ([string] $ownedVm.State -ne 'Off') {
                throw [InvalidOperationException]::new('Disposable VM did not stop')
            }
            $removeJob = Remove-VM -VM $ownedVm -Force -AsJob -Confirm:$false
            Complete-BoundedJob $removeJob 15 | Out-Null
            if ($null -ne (Get-VM -Id $vmId -ErrorAction SilentlyContinue)) {
                throw [InvalidOperationException]::new('Disposable VM registration remains')
            }
        }
        if ($ownedRootCreated) {
            Remove-EmptyOwnedTree ([IO.DirectoryInfo]::new($ownedRoot))
        }
    }
    catch {
        $codes = Get-FiniteErrorCodes $_.Exception
        $receipt.cleanup_complete = $false
        $receipt.cleanup_hresult = $codes[0]
        $receipt.cleanup_native_code = $codes[1]
        $receipt.outcome = 'cleanup_failed'
    }
}

$receipt | ConvertTo-Json -Compress -Depth 4
if (-not $receipt.cleanup_complete) { exit 2 }
exit 0
