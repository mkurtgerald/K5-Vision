[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$BundleScripts
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# Source-only preparation; no workflow or shared-runner launch is authorized.
# The transport must verify these wrapper bytes before in-memory invocation.
# Compilation uses the existing Windows-inbox Desktop 5.1/.NET toolchain. It can
# create official compiler children and task-private files. It is bounded by a
# separately reviewed short Actions step timeout, not by this wrapper's timer.
# ObserveGuarded has its own 20-second current-step watchdog. Abrupt exit or an
# Actions timeout can bypass every finally below: a SEPARATE always-run original
# post-admission, compiler-idle and final compiler-artifact checks MUST pass
# after this PowerShell process exits before accepting any provisional result.
# This observation never confers storage, retry, exception or delivery authority.
$guardSha256 = 'd7a38b5278802d9ba768d9987b4582a219d490923b0cc4da0c297d29a250b45d'
# Exact frozen collector bytes; neither environment nor arguments can change this pin.
$collectorSha256 = 'e4a4e5826d1a341c3159f22910c569c7c5c1b3ae1f1fede00eb3718612e9ac6c'
$strictUtf8 = [Text.UTF8Encoding]::new($false, $true)

function Assert-K5OrdinaryPath([string]$Path, [bool]$Directory) {
    if ($Path.Length -gt 1024 -or $Path -cnotmatch '\A[A-Z]:\\' -or
        $Path -match '[\x00-\x1f"<>|?*]' -or $Path.Substring(2).Contains(':') -or
        [IO.Path]::GetFullPath($Path) -cne $Path) { throw 'path' }
    foreach ($part in $Path.Substring(3).Split('\')) {
        if ($part.Length -eq 0 -or $part -in @('.', '..') -or
            $part.EndsWith('.') -or $part.EndsWith(' ')) { throw 'path' }
    }
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if ($item.FullName -cne $Path -or [bool]$item.PSIsContainer -ne $Directory) { throw 'path' }
    $count = 0
    while ($null -ne $item) {
        $count++
        if ($count -gt 32 -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -or
            -not [string]::IsNullOrEmpty($item.LinkType)) { throw 'path' }
        $item = $(if ($item -is [IO.FileInfo]) { $item.Directory } else { $item.Parent })
    }
}

function Open-K5PinnedFile([string]$Path, [int]$Limit, [string]$Digest) {
    Assert-K5OrdinaryPath $Path $false
    $stream = $null
    $sha256 = $null
    try {
        # Hold read-only, deny-write/delete file handles through observation.
        $stream = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
        if ($stream.Length -le 0 -or $stream.Length -gt $Limit) { throw 'size' }
        $bytes = [byte[]]::new([int]$stream.Length)
        $offset = 0
        while ($offset -lt $bytes.Length) {
            $count = $stream.Read($bytes, $offset, $bytes.Length - $offset)
            if ($count -le 0) { throw 'read' }
            $offset += $count
        }
        if ($stream.ReadByte() -ne -1 -or $stream.Length -ne $bytes.Length) { throw 'size' }
        $sha256 = [Security.Cryptography.SHA256]::Create()
        $actual = ([BitConverter]::ToString($sha256.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant()
        if ($actual -cne $Digest) { throw 'hash' }
        Assert-K5OrdinaryPath $Path $false
        return @{ stream = $stream; bytes = $bytes }
    } catch {
        if ($null -ne $stream) { $stream.Dispose() }
        throw 'binding'
    } finally {
        if ($null -ne $sha256) { $sha256.Dispose() }
    }
}

function Assert-K5CompilerIdle {
    # Names only. No executable paths, command lines, owners, or PID signaling.
    $rows = @(Get-CimInstance -ClassName Win32_Process -Property Name -OperationTimeoutSec 10 -ErrorAction Stop)
    if ($rows.Count -lt 1 -or $rows.Count -gt 32768) { throw 'compiler_inventory' }
    foreach ($row in $rows) {
        if ($row.Name -isnot [string] -or $row.Name.Length -eq 0 -or $row.Name.Length -gt 260 -or
            $row.Name -match '[\x00\\/:\r\n]') { throw 'compiler_inventory' }
        if ($row.Name -iin @('csc.exe', 'cvtres.exe', 'vbc.exe', 'VBCSCompiler.exe')) { throw 'compiler_occupied' }
    }
}

function Assert-K5InboxCompiler {
    if ($PSVersionTable.PSEdition -cne 'Desktop' -or
        $PSVersionTable.PSVersion.Major -ne 5 -or $PSVersionTable.PSVersion.Minor -ne 1 -or
        -not [Environment]::Is64BitProcess -or -not [Environment]::Is64BitOperatingSystem) { throw 'runtime' }
    $runtime = [Runtime.InteropServices.RuntimeEnvironment]::GetRuntimeDirectory().TrimEnd('\')
    if ($runtime -cne 'C:\Windows\Microsoft.NET\Framework64\v4.0.30319') { throw 'runtime' }
    Assert-K5OrdinaryPath $runtime $true
    $compilerPath = Join-Path $runtime 'csc.exe'
    $compiler = Get-Item -LiteralPath $compilerPath -Force -ErrorAction Stop
    if ($compiler.FullName -cne $compilerPath -or $compiler.PSIsContainer -or
        ($compiler.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'runtime' }
    # Desktop 5.1 Add-Type does not reference System.Core by default. HashSet<T>
    # requires this fixed existing inbox assembly, with the same system-file checks.
    $systemCorePath = Join-Path $runtime 'System.Core.dll'
    $systemCore = Get-Item -LiteralPath $systemCorePath -Force -ErrorAction Stop
    if ($systemCore.FullName -cne $systemCorePath -or $systemCore.PSIsContainer -or
        ($systemCore.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'runtime' }
    if ($null -ne ('K5FixedGitObservation' -as [type])) { throw 'type_reuse' }
    # Existing inbox .NET/compiler trust is an explicit platform assumption.
    # No new runtime attestation or single-link policy for system binaries.
    return $systemCorePath
}

function New-K5CompilerDirectory([string]$Path) {
    $parent = [IO.Path]::GetDirectoryName($Path)
    Assert-K5OrdinaryPath $parent $true
    if ([IO.File]::Exists($Path) -or [IO.Directory]::Exists($Path) -or
        (Test-Path -LiteralPath $Path -ErrorAction Stop)) { throw 'compiler_temp_exists' }
    # No Force and no overwrite. An existing or aliased entry is a refusal.
    $created = New-Item -Path $Path -ItemType Directory -ErrorAction Stop
    if ($created.FullName -cne $Path) { throw 'compiler_temp' }
    Assert-K5OrdinaryPath $Path $true
}

function Assert-K5CompilerArtifacts([string]$Path) {
    # Bounded post-preparation acceptance, not a disk quota. Never recurse, read
    # file contents, delete files, or relax the boundary to make cleanup pass.
    Assert-K5OrdinaryPath $Path $true
    $entries = [IO.Directory]::EnumerateFileSystemEntries($Path).GetEnumerator()
    $count = 0
    [long]$bytes = 0
    try {
        while ($entries.MoveNext()) {
            $count++
            if ($count -gt 64) { throw 'compiler_artifacts' }
            $entry = [string]$entries.Current
            Assert-K5OrdinaryPath $entry $false
            $file = Get-Item -LiteralPath $entry -Force -ErrorAction Stop
            $bytes += $file.Length
            if ($file.Length -lt 0 -or $bytes -gt 16777216) { throw 'compiler_artifacts' }
        }
    } finally { $entries.Dispose() }
}

function Add-K5PinnedCollector([string]$Source, [string]$CompilerTemp, [string]$SystemCorePath) {
    Assert-K5OrdinaryPath $CompilerTemp $true
    $previousTemp = [Environment]::GetEnvironmentVariable('TEMP', 'Process')
    $previousTmp = [Environment]::GetEnvironmentVariable('TMP', 'Process')
    $compileError = $null
    $restoreError = $null
    try {
        [Environment]::SetEnvironmentVariable('TEMP', $CompilerTemp, 'Process')
        [Environment]::SetEnvironmentVariable('TMP', $CompilerTemp, 'Process')
        # Fixed verified in-memory text only. No script-path execution, compiler
        # search, new runtime, installer, alternate toolchain or command shell.
        $types = @(Add-Type -TypeDefinition $Source -Language CSharp -PassThru -ReferencedAssemblies $SystemCorePath -ErrorAction Stop -WarningAction SilentlyContinue -Verbose:$false -Debug:$false)
        if (@($types | Where-Object { $_.FullName -ceq 'K5FixedGitObservation' }).Count -ne 1) { throw 'compiled_type' }
    } catch {
        $compileError = $_
        throw
    } finally {
        # Keep the original ErrorRecord and exception private. A restoration
        # failure must not replace a pending compile failure or skip either restore.
        try { [Environment]::SetEnvironmentVariable('TEMP', $previousTemp, 'Process') } catch { if ($null -eq $restoreError) { $restoreError = $_ } }
        try { [Environment]::SetEnvironmentVariable('TMP', $previousTmp, 'Process') } catch { if ($null -eq $restoreError) { $restoreError = $_ } }
        if ($null -ne $restoreError -and $null -eq $compileError) {
            throw [Management.Automation.RuntimeException]::new('compiler_environment', $restoreError.Exception)
        }
    }
}

function Get-K5FailureProjection([Management.Automation.ErrorRecord]$FailureError) {
    $reason = 'unknown'
    $compilerCode = 'unknown'
    if ($null -ne $FailureError) {
        # Select only fixed validation literals; never return exception text.
        $knownReasons = @('context', 'path', 'size', 'read', 'hash', 'binding',
            'compiler_inventory', 'compiler_occupied', 'runtime', 'type_reuse',
            'compiler_temp_exists', 'compiler_temp', 'compiler_artifacts',
            'compiled_type', 'compiler_environment', 'record')
        foreach ($knownReason in $knownReasons) {
            if ($FailureError.Exception.Message -ceq $knownReason) {
                $reason = $knownReason
                break
            }
        }
        # Read only the typed CodeDom error number, never the target's contents,
        # filename, compiler output, error text, invocation or exception details.
        if ($FailureError.TargetObject -is [System.CodeDom.Compiler.CompilerError]) {
            $number = $FailureError.TargetObject.ErrorNumber
            if ($number -is [string] -and $number -cmatch '\ACS[0-9]{4}\z') {
                $compilerCode = $number
            }
        }
    }
    return @{ reason = $reason; compiler_code = $compilerCode }
}

function Assert-K5RecordKeys($Record, [string[]]$Expected) {
    if ($Record -isnot [Collections.Generic.Dictionary[string,object]]) { throw 'record' }
    if ($Record.Count -ne $Expected.Count) { throw 'record' }
    foreach ($key in $Record.Keys) {
        if ($key -isnot [string] -or $key -cnotin $Expected) { throw 'record' }
    }
}

function Assert-K5Integer($Value, [long]$Minimum, [long]$Maximum) {
    if (($Value -isnot [int] -and $Value -isnot [long] -and
        $Value -isnot [uint32] -and $Value -isnot [uint64]) -or
        $Value -lt $Minimum -or $Value -gt $Maximum) { throw 'record' }
}

# Inserted into the hash-pinned observation wrapper, not invoked as another file.
# Local CIM reads only. No service control, account changes, or permissions writes.
function Get-K5ConfiguredRunnerIdentity([string]$ServiceName, [string]$TokenSid) {
    $result = [ordered]@{
        status = 'refused'; code = 'service_name'; service_name = $null
        configured_sid = $null; process_owner_sid = $null; service_pid = $null
        current_token_matches = $false; owned_ancestry_verified = $false
    }
    $phase = 'service_name'
    try {
        if ($ServiceName -cnotmatch '\Aactions\.runner\.[A-Za-z0-9_.-]{1,241}\z' -or
            $TokenSid -cnotmatch '\AS-1-[0-9-]{1,180}\z') { throw 'identity' }
        $result.service_name = $ServiceName
        $filter = "Name='$ServiceName'"
        $expectedPath = 'C:\K5PhysicalRunner\bin\RunnerService.exe'
        $previous = $null
        # Reobserve config, live process owner and complete ancestry. PID alone is
        # never identity; creation dates and ancestry edges must match both times.
        for ($pass = 0; $pass -lt 2; $pass++) {
            $phase = 'service_query'
            $services = @(Get-CimInstance -ClassName Win32_Service -Filter $filter -Property Name,StartName,State,ProcessId,PathName,ServiceType -OperationTimeoutSec 5 -ErrorAction Stop)
            if ($services.Count -ne 1) { $phase = 'service_absent_or_ambiguous'; throw 'identity' }
            $service = $services[0]
            $phase = 'service_scope'
            if ($service.Name -cne $ServiceName -or $service.State -cne 'Running' -or
                $service.ServiceType -cne 'Own Process' -or
                ($service.PathName -cne $expectedPath -and $service.PathName -cne ('"' + $expectedPath + '"')) -or
                [uint32]$service.ProcessId -le 0) { throw 'identity' }
            $phase = 'configured_account'
            $startName = [string]$service.StartName
            $configuredSid = $null
            switch -CaseSensitive ($startName) {
                'LocalSystem' { $configuredSid = 'S-1-5-18' }
                'NT AUTHORITY\SYSTEM' { $configuredSid = 'S-1-5-18' }
                'NT AUTHORITY\LocalService' { $configuredSid = 'S-1-5-19' }
                'NT AUTHORITY\LOCAL SERVICE' { $configuredSid = 'S-1-5-19' }
                'NT AUTHORITY\NetworkService' { $configuredSid = 'S-1-5-20' }
                'NT AUTHORITY\NETWORK SERVICE' { $configuredSid = 'S-1-5-20' }
            }
            if ($null -eq $configuredSid) {
                # Deliberately no domain/remote account lookup or implicit fallback.
                if ($startName -cnotmatch '\A(?:\.|VLR-CYZ4PK3)\\([A-Za-z0-9_.-]{1,64})\z') { throw 'identity' }
                $accountName = $Matches[1]
                $accounts = @(Get-CimInstance -ClassName Win32_UserAccount -Filter "LocalAccount=True AND Domain='VLR-CYZ4PK3' AND Name='$accountName'" -Property Name,Domain,LocalAccount,SID,Disabled -OperationTimeoutSec 5 -ErrorAction Stop)
                if ($accounts.Count -ne 1 -or -not $accounts[0].LocalAccount -or $accounts[0].Disabled -or
                    $accounts[0].Name -cne $accountName -or $accounts[0].Domain -cne 'VLR-CYZ4PK3' -or
                    $accounts[0].SID -cnotmatch '\AS-1-5-21-[0-9-]{1,160}\z') { throw 'identity' }
                $configuredSid = [string]$accounts[0].SID
            }
            $result.configured_sid = $configuredSid
            $phase = 'process_query'
            $processes = @(Get-CimInstance -ClassName Win32_Process -Property Name,ProcessId,ParentProcessId,CreationDate,ExecutablePath -OperationTimeoutSec 5 -ErrorAction Stop)
            if ($processes.Count -eq 0 -or $processes.Count -gt 32768) { throw 'identity' }
            $byId = @{}
            foreach ($process in $processes) {
                $processId = [uint32]$process.ProcessId
                if ($byId.ContainsKey($processId)) { throw 'identity' }
                $byId[$processId] = $process
            }
            $phase = 'service_process'
            $serviceId = [uint32]$service.ProcessId
            if (-not $byId.ContainsKey($serviceId)) { throw 'identity' }
            $result.service_pid = $serviceId
            $serviceProcess = $byId[$serviceId]
            if ($serviceProcess.Name -cne 'RunnerService.exe' -or
                $serviceProcess.ExecutablePath -cne $expectedPath -or
                $null -eq $serviceProcess.CreationDate) { throw 'identity' }
            $phase = 'process_owner'
            # GetOwnerSid is read-only. No other CIM method is called.
            $owner = Invoke-CimMethod -InputObject $serviceProcess -MethodName GetOwnerSid -OperationTimeoutSec 5 -ErrorAction Stop
            if ($owner.ReturnValue -ne 0 -or $owner.Sid -cnotmatch '\AS-1-[0-9-]{1,180}\z') { throw 'identity' }
            $result.process_owner_sid = [string]$owner.Sid
            $result.current_token_matches = $TokenSid -ceq $configuredSid
            $phase = 'account_mismatch'
            if ($owner.Sid -cne $configuredSid -or $TokenSid -cne $configuredSid) { throw 'identity' }
            $phase = 'owned_ancestry'
            $cursor = [uint32]$PID
            $seen = @{}
            $chain = [Collections.Generic.List[string]]::new()
            $found = $false
            $workerCount = 0
            for ($depth = 0; $depth -lt 32; $depth++) {
                if ($seen.ContainsKey($cursor) -or -not $byId.ContainsKey($cursor)) { throw 'identity' }
                $seen[$cursor] = $true
                $node = $byId[$cursor]
                if ($null -eq $node.CreationDate) { throw 'identity' }
                $chain.Add(([string]$cursor + ':' + $node.CreationDate.ToUniversalTime().Ticks))
                if ($node.Name -ceq 'Runner.Worker.exe') { $workerCount++ }
                if ($cursor -eq $serviceId) { $found = $true; break }
                $parentId = [uint32]$node.ParentProcessId
                if (-not $byId.ContainsKey($parentId) -or $null -eq $byId[$parentId].CreationDate -or
                    $byId[$parentId].CreationDate -gt $node.CreationDate) { throw 'identity' }
                $cursor = $parentId
            }
            if (-not $found -or $workerCount -ne 1) { throw 'identity' }
            $fingerprint = @($service.Name,$startName,$service.PathName,$service.State,$service.ServiceType,
                $configuredSid,[string]$owner.Sid,($chain -join ',')) -join '|'
            $phase = 'identity_changed'
            if ($null -ne $previous -and $fingerprint -cne $previous) { throw 'identity' }
            $previous = $fingerprint
        }
        $phase = 'current_token_changed'
        $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
        try {
            if ($null -eq $identity.User -or $identity.User.Value -cne $TokenSid) { throw 'identity' }
        } finally { $identity.Dispose() }
        $result.status = 'verified'
        $result.code = 'none'
        $result.configured_sid = $configuredSid
        $result.process_owner_sid = [string]$owner.Sid
        $result.service_pid = $serviceId
        $result.current_token_matches = $true
        $result.owned_ancestry_verified = $true
    } catch {
        # Emit only a closed reason vocabulary, never raw account, provider,
        # exception, executable or command-line text.
        $result.code = $phase
    }
    return $result
}

function Assert-K5DiagnosticRecord($Record, [hashtable]$Context) {
    $keys = @('schema_version','scope','target','status','code','closure_complete',
        'exception_authority','storage_admission','retry_authority','bytes_read','elapsed_ms')
    if ($Record['status'] -ceq 'observed') {
        $keys += @('current_token_sid','service_identity','runner_service_name','group_metadata','retention_nodes','directories')
    }
    Assert-K5RecordKeys $Record $keys
    if ($Record['schema_version'] -cne 'fixed-runner-acl-prerequisite-v1' -or
        $Record['scope'] -cne 'fixed-runner-readonly-acl' -or
        $Record['target'] -cne 'runner-root-chain' -or
        $Record['status'] -cnotin @('observed','refused')) { throw 'record' }
    foreach ($key in @('exception_authority','storage_admission','retry_authority')) {
        if ($Record[$key] -isnot [bool] -or $Record[$key]) { throw 'record' }
    }
    Assert-K5Integer $Record['bytes_read'] 0 1024
    Assert-K5Integer $Record['elapsed_ms'] 0 19999
    if ($Record['closure_complete'] -isnot [bool]) { throw 'record' }
    if ($Record['status'] -ceq 'observed') {
        if (-not $Record['closure_complete'] -or $Record['code'] -cne 'none' -or
            $Record['service_identity'] -cne 'unresolved' -or $Record['group_metadata'] -cne 'not_queried' -or
            $Record['retention_nodes'] -cne 'not_queried' -or
            $Record['runner_service_name'] -cnotmatch '\Aactions\.runner\.[A-Za-z0-9_.-]{1,241}\z' -or
            $Record['current_token_sid'] -cnotmatch '\AS-1-[0-9-]{1,180}\z') { throw 'record' }
        Assert-K5Integer $Record['elapsed_ms'] 0 8000
        $roles = @('volume_root','runner_root','work_root','repository_parent','workspace','temp_root')
        $directories = $Record['directories']
        if ($directories -isnot [Collections.IList] -or $directories.Count -ne 6) { throw 'record' }
        for ($i=0; $i -lt 6; $i++) {
            $item = $directories[$i]
            Assert-K5RecordKeys $item @('owner_sid','control','acl_sha256','owner_dacl_base64','aces','role','ntfs_volume_serial','ntfs_file_id')
            if ($item['role'] -cne $roles[$i] -or $item['owner_sid'] -cnotmatch '\AS-1-[0-9-]{1,180}\z' -or
                $item['acl_sha256'] -cnotmatch '\A[0-9a-f]{64}\z' -or
                $item['ntfs_volume_serial'] -cnotmatch '\A[0-9a-f]{8}\z' -or
                $item['ntfs_file_id'] -cnotmatch '\A[0-9a-f]{16}\z' -or
                $item['owner_dacl_base64'] -isnot [string] -or $item['owner_dacl_base64'].Length -gt 87384 -or
                $item['owner_dacl_base64'] -cnotmatch '\A[A-Za-z0-9+/]+={0,2}\z') { throw 'record' }
            Assert-K5Integer $item['control'] 0 65535
            if ($item['aces'] -isnot [Collections.IList] -or $item['aces'].Count -gt 128) { throw 'record' }
            foreach ($ace in $item['aces']) {
                Assert-K5RecordKeys $ace @('type','flags','mask','sid')
                Assert-K5Integer $ace['type'] 0 255
                Assert-K5Integer $ace['flags'] 0 255
                Assert-K5Integer $ace['mask'] 0 4294967295
                if ($ace['sid'] -cnotmatch '\AS-1-[0-9-]{1,180}\z') { throw 'record' }
            }
        }
    } else {
        if ($Record['closure_complete'] -or $Record['code'] -cnotin @('platform','abi','anchor_open','relative_open',
            'metadata','filesystem','reparse','path_type','acl_unavailable','acl_null','identity_mismatch',
            'path_shape','name_bound','time_bound','changed','handle_bound','cleanup_failed','internal','enumeration','file_size','read_failed','bytes_bound','alias_count','service_metadata_absent','service_metadata')) { throw 'record' }
    }
    $sanitized = [ordered]@{}
    foreach ($key in $keys) { $sanitized[$key] = $Record[$key] }
    $serviceStatus = 'not_queried'
    if ($Record['status'] -ceq 'observed') {
        $configured = Get-K5ConfiguredRunnerIdentity $Record['runner_service_name'] $Record['current_token_sid']
        $sanitized['configured_runner_identity'] = $configured
        $serviceStatus = $configured.status
        $sanitized['service_identity'] = $(if ($serviceStatus -ceq 'verified') { 'verified' } else { 'unresolved' })
    }
    # These prerequisites are NOT established by a fixed six-directory sample.
    $sanitized['repair_ready'] = $false
    $sanitized['required_writers'] = 'unresolved'
    $sanitized['affected_subtree_inventory'] = 'not_collected'
    $sanitized['rollback_backup'] = 'not_created'
    $sanitized['source_sha'] = $Context.source_sha
    $sanitized['run_id'] = $Context.run_id
    $sanitized['run_attempt'] = $Context.run_attempt
    $sanitized['requires_separate_post_admission'] = $true
    $sanitized['compiler_artifacts_retained'] = $true
    $text = ConvertTo-Json -InputObject $sanitized -Compress -Depth 8
    if ($strictUtf8.GetByteCount($text) -gt 65536 -or $text -match '[\r\n]') { throw 'record' }
    return @{text=$text;status=$Record['status'];service_status=$serviceStatus}
}

$guard = $null
$guardFile = $null
$collectorFile = $null
$compilerTemp = $null
$compilerTempCreated = $false
$result = $null
$failure = 'context_binding'
$failureError = $null
$failed = $false
$postPassed = $false
try {
    if ($env:OS -cne 'Windows_NT' -or [Environment]::MachineName -cne 'VLR-CYZ4PK3' -or
        $env:RUNNER_NAME -cne 'K5-Physical' -or
        $env:GITHUB_REPOSITORY -cne 'mkurtgerald/K5-Vision' -or
        $env:GITHUB_SHA -cnotmatch '\A[0-9a-f]{40}\z' -or
        $env:K5_DIAGNOSTIC_SHA -cne $env:GITHUB_SHA -or
        $env:GITHUB_RUN_ID -cnotmatch '\A[1-9][0-9]{0,19}\z' -or
        $env:GITHUB_RUN_ATTEMPT -cnotmatch '\A[1-9][0-9]{0,19}\z') { throw 'context' }
    $context = @{
        repository = 'mkurtgerald/K5-Vision'; source_sha = $env:GITHUB_SHA
        run_id = $env:GITHUB_RUN_ID; run_attempt = $env:GITHUB_RUN_ATTEMPT
    }
    $failure = 'path_binding'
    $runner = [IO.DirectoryInfo]::new($env:RUNNER_WORKSPACE)
    $workspace = [IO.DirectoryInfo]::new($env:GITHUB_WORKSPACE)
    $temporary = [IO.DirectoryInfo]::new($env:RUNNER_TEMP)
    $bundleRoot = Join-Path $temporary.FullName ('k5-git-link-observation-' + $context.run_id + '-' + $context.run_attempt)
    $expectedBundleScripts = Join-Path $bundleRoot 'scripts'
    if ($runner.Name -cne 'K5-Vision' -or $workspace.Name -cne 'K5-Vision' -or
        $workspace.Parent.FullName -cne $runner.FullName -or
        $temporary.Name -cne '_temp' -or $temporary.Parent.FullName -cne $runner.Parent.FullName -or
        $BundleScripts -cne $expectedBundleScripts) { throw 'path' }
    Assert-K5OrdinaryPath $env:RUNNER_WORKSPACE $true
    Assert-K5OrdinaryPath $env:GITHUB_WORKSPACE $true
    Assert-K5OrdinaryPath $env:RUNNER_TEMP $true
    Assert-K5OrdinaryPath $BundleScripts $true
    $guardPath = Join-Path $BundleScripts 'assert-stage-one-physical-admission.ps1'
    $collectorPath = Join-Path $BundleScripts 'observe_installed_git_links.cs'
    $compilerTemp = Join-Path $bundleRoot 'compiler-temp'
    $failure = 'guard_binding'
    $guardFile = Open-K5PinnedFile $guardPath 131072 $guardSha256
    $guard = [scriptblock]::Create($strictUtf8.GetString($guardFile.bytes))
    $failure = 'pre_admission'
    & $guard > $null
    $failure = 'compiler_idle_before'
    Assert-K5CompilerIdle
    $failure = 'collector_binding'
    $collectorFile = Open-K5PinnedFile $collectorPath 65536 $collectorSha256
    $collectorText = $strictUtf8.GetString($collectorFile.bytes)
    $failure = 'runtime_binding'
    $systemCorePath = Assert-K5InboxCompiler
    $failure = 'compiler_temp'
    New-K5CompilerDirectory $compilerTemp
    $compilerTempCreated = $true
    $failure = 'collector_compile'
    Add-K5PinnedCollector $collectorText $compilerTemp $systemCorePath
    $failure = 'compiler_idle_after'
    Assert-K5CompilerIdle
    $failure = 'collector_observation'
    $record = [K5FixedGitObservation]::ObserveGuarded()
    $failure = 'record_validation'
    $result = Assert-K5DiagnosticRecord $record $context
} catch {
    # No exception text, paths, compiler output, environment or raw records.
    $failureError = $_
    $failed = $true
} finally {
    # Normal control flow only. A separate always-run post-admission is mandatory
    # after any outcome, including watchdog exit or the external step timeout.
    if ($null -ne $guard) {
        try {
            & $guard > $null
            Assert-K5CompilerIdle
            $postPassed = $true
        } catch {
            $failed = $true
            if ($null -eq $failureError) {
                $failureError = $_
                $failure = 'post_admission'
            }
        }
    }
    foreach ($file in @($collectorFile, $guardFile)) {
        if ($null -ne $file) {
            try { $file.stream.Dispose() } catch {
                $failed = $true
                if ($null -eq $failureError) {
                    $failureError = $_
                    $failure = 'file_cleanup'
                }
            }
        }
    }
    # Preserve compiler artifacts. Never recursively remove unknown files, kill
    # unknown compiler processes, or treat an uncertain cleanup as successful.
}

if ($failed -or -not $postPassed -or $null -eq $result) {
    $projection = Get-K5FailureProjection $failureError
    $record = [ordered]@{
        schema_version = 'fixed-runner-acl-prerequisite-wrapper-v1'
        scope = 'fixed-runner-readonly-acl'; status = 'refused'; code = $failure
        reason = $projection.reason; compiler_code = $projection.compiler_code
        storage_admission = $false; retry_authority = $false; exception_authority = $false
        requires_separate_post_admission = $true; compiler_artifacts_retained = $true
    }
    Write-Host ('K5_RUNNER_ACL_PREREQUISITE=' + ($record | ConvertTo-Json -Compress -Depth 4))
    throw 'Runner ACL observation refused. Existing owners were preserved.'
}
Write-Host ('K5_RUNNER_ACL_PREREQUISITE=' + $result.text)
if ($result.status -cne 'observed' -or $result.service_status -cne 'verified') {
    throw 'Runner ACL observation refused. Existing owners were preserved.'
}
