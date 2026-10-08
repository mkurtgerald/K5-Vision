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
# The observation stage has one 150-second owned-process aggregate watchdog. Abrupt exit or an
# Actions timeout can bypass every finally below: a SEPARATE always-run original
# post-admission, compiler-idle and final compiler-artifact checks MUST pass
# after this PowerShell process exits before accepting any provisional result.
# This observation never confers storage, retry, exception or delivery authority.
$guardSha256 = 'd7a38b5278802d9ba768d9987b4582a219d490923b0cc4da0c297d29a250b45d'
# Exact frozen collector bytes; neither environment nor arguments can change this pin.
$collectorSha256 = '0e4553dd178cb8e08a43507e40c0e1a4db8b80086a64e54daa58fcb8c94a2eae'
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

function Get-K5ClosedCompilerCode([Management.Automation.ErrorRecord]$FailureRecord) {
    if ($null -eq $FailureRecord -or $FailureRecord.FullyQualifiedErrorId -cnotin @(
        'SOURCE_CODE_ERROR,Microsoft.PowerShell.Commands.AddTypeCommand',
        'COMPILER_ERRORS,Microsoft.PowerShell.Commands.AddTypeCommand'
    )) { return 'unknown' }
    if ($FailureRecord.TargetObject -is [System.CodeDom.Compiler.CompilerError] -and
        $FailureRecord.TargetObject.ErrorNumber -cmatch '\ACS[0-9]{4}\z') {
        return $FailureRecord.TargetObject.ErrorNumber
    }
    # Inspect bounded error messages privately. Never print source/paths, inspect
    # a string TargetObject, call ToString(), or open compiler files/output.
    $messages = @($FailureRecord.Exception.Message)
    if ($null -ne $FailureRecord.ErrorDetails) { $messages += $FailureRecord.ErrorDetails.Message }
    $codes = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    foreach ($message in $messages) {
        if ($null -eq $message) { continue }
        if ($message -isnot [string] -or $message.Length -gt 8192) { return 'unknown' }
        $matches = [regex]::Matches($message, '(?<![A-Za-z0-9_])CS[0-9]{4}(?![A-Za-z0-9_])')
        if ($matches.Count -gt 16) { return 'unknown' }
        foreach ($match in $matches) { $null = $codes.Add($match.Value) }
    }
    if ($codes.Count -ne 1) { return 'unknown' }
    foreach ($code in $codes) { return $code }
    return 'unknown'
}

function Get-K5FailureProjection([Management.Automation.ErrorRecord]$FailureError) {
    $reason = 'unknown'
    $compilerCode = 'unknown'
    if ($null -ne $FailureError) {
        # Select only fixed validation literals; never return exception text.
        $knownReasons = @('context', 'path', 'size', 'read', 'hash', 'binding',
            'compiler_inventory', 'compiler_occupied', 'runtime', 'type_reuse',
            'compiler_temp_exists', 'compiler_temp', 'compiler_artifacts',
            'compiled_type', 'compiler_environment', 'record', 'writer_identity', 'writer_membership',
            'runner_inventory', 'runner_occupied', 'runner_ancestry', 'runner_owner', 'runner_scope',
            'runner_changed', 'backup_bound', 'backup_verify')
        foreach ($knownReason in $knownReasons) {
            if ($FailureError.Exception.Message -ceq $knownReason) {
                $reason = $knownReason
                break
            }
        }
        if ($failure -ceq 'collector_compile') {
            $compilerCode = Get-K5ClosedCompilerCode $FailureError
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

# Read-only identity prerequisite. Preserve the observed runner startup mode.
# No .service file, service inference/control, account lookup or credential read.
function Assert-K5SameProfileRunner {
    $expectedSid = 'S-1-5-21-283315059-370827648-873861665-1000'
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    try {
        if ($null -eq $identity.User -or $identity.User.Value -cne $expectedSid) { throw 'writer_identity' }
        $principal = [Security.Principal.WindowsPrincipal]::new($identity)
        $au = [Security.Principal.SecurityIdentifier]::new('S-1-5-11')
        if (-not $principal.IsInRole($au)) { throw 'writer_membership' }
    } finally { $identity.Dispose() }
    $rows = @(Get-CimInstance -ClassName Win32_Process -Property Name,ProcessId,ParentProcessId,CreationDate,ExecutablePath -OperationTimeoutSec 5 -ErrorAction Stop)
    if ($rows.Count -lt 1 -or $rows.Count -gt 32768) { throw 'runner_inventory' }
    $byId = @{}
    $workers = 0
    foreach ($row in $rows) {
        $id = [uint32]$row.ProcessId
        if ($byId.ContainsKey($id)) { throw 'runner_inventory' }
        $byId[$id] = $row
        if ($row.Name -ceq 'Runner.Worker.exe') { $workers++ }
    }
    if ($workers -ne 1) { throw 'runner_occupied' }
    $cursor = [uint32]$PID
    $seen = @{}
    $chain = [Collections.Generic.List[string]]::new()
    $workerSeen = $false
    $listenerSeen = $false
    for ($depth = 0; $depth -lt 32; $depth++) {
        if ($seen.ContainsKey($cursor) -or -not $byId.ContainsKey($cursor)) { throw 'runner_ancestry' }
        $seen[$cursor] = $true
        $node = $byId[$cursor]
        if ($null -eq $node.CreationDate) { throw 'runner_ancestry' }
        $chain.Add(([string]$cursor + ':' + $node.CreationDate.ToUniversalTime().Ticks))
        if ($cursor -eq [uint32]$PID -or $node.Name -ceq 'Runner.Worker.exe' -or $node.Name -ceq 'Runner.Listener.exe') {
            $owner = Invoke-CimMethod -InputObject $node -MethodName GetOwnerSid -OperationTimeoutSec 5 -ErrorAction Stop
            if ($owner.ReturnValue -ne 0 -or $owner.Sid -cne $expectedSid) { throw 'runner_owner' }
        }
        if ($node.Name -ceq 'Runner.Worker.exe') {
            if ($workerSeen -or $node.ExecutablePath -cne 'C:\K5PhysicalRunner\bin\Runner.Worker.exe') { throw 'runner_scope' }
            $workerSeen = $true
        }
        if ($node.Name -ceq 'Runner.Listener.exe') {
            if (-not $workerSeen -or $node.ExecutablePath -cne 'C:\K5PhysicalRunner\bin\Runner.Listener.exe') { throw 'runner_scope' }
            $listenerSeen = $true
            break
        }
        $parentId = [uint32]$node.ParentProcessId
        if (-not $byId.ContainsKey($parentId) -or $null -eq $byId[$parentId].CreationDate -or
            $byId[$parentId].CreationDate -gt $node.CreationDate) { throw 'runner_ancestry' }
        $cursor = $parentId
    }
    if (-not $listenerSeen) { throw 'runner_ancestry' }
    $fingerprint = $chain -join ','
    return @{ writer_sid = $expectedSid; identity_fingerprint = $fingerprint;
    authenticated_users_member = $true; startup_mode_changed = $false }
}

function Save-K5DaclInventory($Record, [hashtable]$Context, [hashtable]$Writer) {
    $keys = @('schema_version','scope','target','status','code','closure_complete',
        'exception_authority','storage_admission','retry_authority','bytes_read','elapsed_ms')
    if ($Record['status'] -ceq 'observed') {
        $keys += @('records','object_count','dacl_bytes','path_units','repair_ready')
    }
    Assert-K5RecordKeys $Record $keys
    if ($Record['schema_version'] -cne 'fixed-runner-dacl-inventory-v1' -or
        $Record['scope'] -cne 'fixed-runner-readonly-dacl-inventory' -or
        $Record['target'] -cne 'runner-root-subtree' -or
        $Record['status'] -cnotin @('observed','refused')) { throw 'record' }
    foreach ($key in @('exception_authority','storage_admission','retry_authority')) {
        if ($Record[$key] -isnot [bool] -or $Record[$key]) { throw 'record' }
    }
    Assert-K5Integer $Record['bytes_read'] 0 0
    Assert-K5Integer $Record['elapsed_ms'] 0 90000
    if ($Record['closure_complete'] -isnot [bool]) { throw 'record' }
    if ($Record['status'] -cne 'observed') {
        if ($Record['closure_complete'] -or $Record['code'] -cnotin @('platform','abi','anchor_open','relative_open',
            'metadata','filesystem','reparse','path_type','acl_unavailable','acl_null','identity_mismatch',
            'path_shape','name_bound','time_bound','changed','handle_bound','cleanup_failed','internal',
            'enumeration','file_size','bytes_bound','alias_count','inventory_bound','inventory_changed')) { throw 'record' }
        return @{ status='refused'; code=$Record['code']; repair_ready=$false; backup_verified=$false }
    }
    if (-not $Record['closure_complete'] -or $Record['code'] -cne 'none' -or
        $Record['repair_ready'] -isnot [bool] -or $Record['repair_ready']) { throw 'record' }
    Assert-K5Integer $Record['elapsed_ms'] 0 75000
    Assert-K5Integer $Record['object_count'] 1 50000
    Assert-K5Integer $Record['dacl_bytes'] 0 67108864
    Assert-K5Integer $Record['path_units'] 0 8388608
    $records = $Record['records']
    if ($records -isnot [Collections.IList] -or $records.Count -ne $Record['object_count']) { throw 'record' }
    $seen = @{}
    [long]$totalBytes = 0
    [long]$totalPaths = 0
    $rootCount = 0
    foreach ($item in $records) {
        Assert-K5RecordKeys $item @('control','dacl_sha256','dacl_base64','dacl_bytes','ace_count',
            'path','object_id','parent_id','object_kind','link_count','attributes')
        $path = $item['path']
        if ($path -isnot [string] -or $path.Length -gt 4096 -or
            ($path -cne 'C:\K5PhysicalRunner' -and -not $path.StartsWith('C:\K5PhysicalRunner\', [StringComparison]::Ordinal)) -or
            $item['object_id'] -cnotmatch '\A[0-9a-f]{8}:[0-9a-f]{16}\z' -or
            $item['object_kind'] -cnotin @('file','directory') -or $seen.ContainsKey($item['object_id']) -or
            $item['dacl_sha256'] -cnotmatch '\A[0-9a-f]{64}\z') { throw 'record' }
        if ($path -ceq 'C:\K5PhysicalRunner') {
            $rootCount++
            if ($item['parent_id'] -cne 'outside-approved-root' -or $item['object_kind'] -cne 'directory') { throw 'record' }
        } elseif (-not $seen.ContainsKey($item['parent_id'])) { throw 'record' }
        $seen[$item['object_id']] = $true
        Assert-K5Integer $item['control'] 0 65535
        Assert-K5Integer $item['attributes'] 0 4294967295
        Assert-K5Integer $item['link_count'] 1 4294967295
        if ($item['object_kind'] -ceq 'file' -and $item['link_count'] -ne 1) { throw 'record' }
        Assert-K5Integer $item['ace_count'] 0 128
        Assert-K5Integer $item['dacl_bytes'] 20 65536
        if ($item['dacl_base64'] -isnot [string] -or $item['dacl_base64'].Length -gt 87384) { throw 'record' }
        $dacl = [Convert]::FromBase64String($item['dacl_base64'])
        if ($dacl.Length -ne $item['dacl_bytes'] -or [Convert]::ToBase64String($dacl) -cne $item['dacl_base64']) { throw 'record' }
        $hash = [Security.Cryptography.SHA256]::Create()
        try {
            $actual = ([BitConverter]::ToString($hash.ComputeHash($dacl))).Replace('-', '').ToLowerInvariant()
            if ($actual -cne $item['dacl_sha256']) { throw 'record' }
        } finally { $hash.Dispose() }
        $raw = [Security.AccessControl.RawSecurityDescriptor]::new($dacl, 0)
        if ($null -ne $raw.Owner -or $null -ne $raw.Group -or $null -ne $raw.SystemAcl -or
            $null -eq $raw.DiscretionaryAcl -or [int]$raw.ControlFlags -ne $item['control'] -or
            $raw.DiscretionaryAcl.Count -ne $item['ace_count']) { throw 'record' }
        $totalBytes += $dacl.Length
        $totalPaths += $path.Length
    }
    if ($rootCount -ne 1 -or $totalBytes -ne $Record['dacl_bytes'] -or $totalPaths -ne $Record['path_units']) { throw 'record' }
    # The second actual job/worker/listener identity observation follows inventory.
    $after = Assert-K5SameProfileRunner
    if ($after.identity_fingerprint -cne $Writer.identity_fingerprint -or
        $after.writer_sid -cne $Writer.writer_sid) { throw 'runner_changed' }
    $envelope = [ordered]@{
        schema_version='k5-runner-dacl-backup-v1'; scope_root='C:\K5PhysicalRunner'
        source_sha=$Context.source_sha; run_id=$Context.run_id; run_attempt=$Context.run_attempt
        writer=$Writer; inventory=$Record; repair_ready=$false
        rollback_status='not_implemented'; requires_fresh_prechange_revalidation=$true
        requires_fresh_backup_identity_hash_and_custody_revalidation=$true
    }
    $json = ConvertTo-Json -InputObject $envelope -Compress -Depth 12
    if ($strictUtf8.GetByteCount($json) -gt 268435456) { throw 'backup_bound' }
    $bytes = $strictUtf8.GetBytes($json)
    $name = 'K5RunnerDaclBackup-' + $Context.run_id + '-' + $Context.run_attempt + '.json'
    $localAppData = [Environment]::GetFolderPath([Environment+SpecialFolder]::LocalApplicationData)
    $script:backupAttempted = $true
    $backup = [K5FixedGitObservation]::SaveBackup($localAppData, $name, $bytes)
    if ($backup['status'] -cne 'observed') {
        return @{ status='refused'; code=$backup['code']; repair_ready=$false;
            backup_verified=$false; backup_may_exist=$true }
    }
    if (-not $backup['readback_verified'] -or $backup['repair_ready']) { throw 'backup_verify' }
    return @{ status='observed'; code='none'; repair_ready=$false; backup_verified=$true;
        backup=$backup; object_count=$records.Count; writer_sid=$Writer.writer_sid;
        inventory_elapsed_ms=$Record['elapsed_ms']; rollback_status='not_implemented' }
}

$deadline = $null
$script:backupAttempted = $false
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
    $failure = 'aggregate_deadline'
    $deadline = [K5FixedGitObservation]::StartObservationDeadline()
    $failure = 'runner_identity'
    $writer = Assert-K5SameProfileRunner
    $failure = 'collector_observation'
    $record = [K5FixedGitObservation]::ObserveGuarded()
    $failure = 'record_validation'
    $result = Save-K5DaclInventory $record $context $writer
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
    if ($null -ne $deadline) { $deadline.Dispose() }
    # Preserve compiler artifacts. Never recursively remove unknown files, kill
    # unknown compiler processes, or treat an uncertain cleanup as successful.
}

if ($failed -or -not $postPassed -or $null -eq $result) {
    $projection = Get-K5FailureProjection $failureError
    $record = [ordered]@{
        schema_version = 'fixed-runner-dacl-backup-wrapper-v1'
        scope = 'fixed-runner-dacl-backup'; status = 'refused'; code = $failure
        repair_ready = $false; backup_verified = $false; backup_may_exist = $script:backupAttempted
        reason = $projection.reason; compiler_code = $projection.compiler_code
        storage_admission = $false; retry_authority = $false; exception_authority = $false
        requires_separate_post_admission = $true; compiler_artifacts_retained = $true
    }
    Write-Host ('K5_RUNNER_DACL_BACKUP=' + ($record | ConvertTo-Json -Compress -Depth 4))
    throw 'Runner ACL observation refused. Existing owners were preserved.'
}
$result['source_sha'] = $context.source_sha
$result['run_id'] = $context.run_id
$result['run_attempt'] = $context.run_attempt
$result['requires_separate_post_admission'] = $true
$result['aggregate_observation_deadline_seconds'] = 150
Write-Host ('K5_RUNNER_DACL_BACKUP=' + ($result | ConvertTo-Json -Compress -Depth 6))
if ($result.status -cne 'observed' -or -not $result.backup_verified) {
    throw 'Runner ACL observation refused. Existing owners were preserved.'
}
