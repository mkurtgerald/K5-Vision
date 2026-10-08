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
$collectorSha256 = 'eeb76812034a45604fbe43171cecbffcc413af8c31fca611e0f2a75bf202059a'
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

function Assert-K5DiagnosticRecord($Record, [hashtable]$Context) {
    if ($Record -isnot [Collections.Generic.Dictionary[string,object]] -or
        -not $Record.ContainsKey('status') -or $Record['status'] -isnot [string] -or
        $Record['status'] -cnotin @('observed', 'refused')) { throw 'record' }
    $keys = @('schema_version', 'scope', 'target', 'status', 'code', 'closure_complete',
        'exception_authority', 'storage_admission', 'retry_authority', 'bytes_read', 'elapsed_ms')
    if ($Record['status'] -ceq 'observed') {
        $keys += @('link_count', 'primary_sha256', 'aliases')
    }
    Assert-K5RecordKeys $Record $keys
    foreach ($key in @('schema_version', 'scope', 'target', 'status', 'code')) {
        if ($Record[$key] -isnot [string]) { throw 'record' }
    }
    if ($Record['schema_version'] -cne 'fixed-git-hardlink-observation-v2' -or
        $Record['scope'] -cne 'fixed-git-readonly-metadata' -or
        $Record['target'] -cne 'cmd/git.exe') { throw 'record' }
    foreach ($key in @('exception_authority', 'storage_admission', 'retry_authority')) {
        if ($Record[$key] -isnot [bool] -or $Record[$key] -ne $false) { throw 'record' }
    }
    if ($Record['closure_complete'] -isnot [bool]) { throw 'record' }
    Assert-K5Integer $Record['bytes_read'] 0 134217728
    Assert-K5Integer $Record['elapsed_ms'] 0 19999
    if ($Record['status'] -ceq 'refused') {
        $codes = @('platform', 'abi', 'anchor_open', 'relative_open', 'metadata', 'filesystem',
            'reparse', 'path_type', 'acl_unavailable', 'acl_null', 'alias_outside_root',
            'alias_duplicate', 'alias_missing_primary', 'alias_count', 'identity_mismatch',
            'path_shape', 'name_bound', 'file_size', 'bytes_bound', 'time_bound', 'enumeration',
            'changed', 'read_failed', 'handle_bound', 'cleanup_failed', 'internal')
        if ($Record['closure_complete'] -ne $false -or $Record['code'] -cnotin $codes) { throw 'record' }
    } else {
        if ($Record['closure_complete'] -ne $true -or $Record['code'] -cne 'none' -or
            $Record['primary_sha256'] -isnot [string] -or
            $Record['primary_sha256'] -cnotmatch '\A[0-9a-f]{64}\z') { throw 'record' }
        Assert-K5Integer $Record['link_count'] 1 16
        Assert-K5Integer $Record['bytes_read'] 1 134217728
        Assert-K5Integer $Record['elapsed_ms'] 0 8000
        $aliases = $Record['aliases']
        if ($aliases -isnot [Collections.IList] -or $aliases.Count -ne $Record['link_count']) { throw 'record' }
        $seen = @{}
        $first = $null
        foreach ($alias in $aliases) {
            Assert-K5RecordKeys $alias @('relative_name', 'ntfs_volume_serial', 'ntfs_file_id',
                'link_count', 'size_bytes', 'sha256', 'acl_sha256')
            foreach ($key in @('relative_name', 'ntfs_volume_serial', 'ntfs_file_id', 'sha256', 'acl_sha256')) {
                if ($alias[$key] -isnot [string]) { throw 'record' }
            }
            if ($alias['relative_name'].Length -gt 1024 -or
                $alias['relative_name'] -cnotmatch '\A[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\z' -or
                $alias['ntfs_volume_serial'] -cnotmatch '\A[0-9a-f]{8}\z' -or
                $alias['ntfs_file_id'] -cnotmatch '\A[0-9a-f]{16}\z' -or
                $alias['sha256'] -cne $Record['primary_sha256'] -or
                $alias['acl_sha256'] -cnotmatch '\A[0-9a-f]{64}\z') { throw 'record' }
            foreach ($part in $alias['relative_name'].Split('/')) {
                if ($part -in @('.', '..') -or $part.EndsWith('.')) { throw 'record' }
            }
            if ($seen.ContainsKey($alias['relative_name'])) { throw 'record' }
            $seen[$alias['relative_name']] = $true
            Assert-K5Integer $alias['link_count'] 1 16
            Assert-K5Integer $alias['size_bytes'] 1 33554432
            if ($alias['link_count'] -ne $Record['link_count']) { throw 'record' }
            if ($null -eq $first) { $first = $alias }
            foreach ($key in @('ntfs_volume_serial', 'ntfs_file_id', 'acl_sha256', 'size_bytes')) {
                if ($alias[$key] -cne $first[$key]) { throw 'record' }
            }
        }
        if (-not $seen.ContainsKey('cmd/git.exe') -or
            $Record['bytes_read'] -ne ($first['size_bytes'] * ($Record['link_count'] + 2))) { throw 'record' }
    }
    # A readable owner/DACL fingerprint is evidence only, never ACL admission.
    $sanitized = [ordered]@{}
    foreach ($key in $keys) { $sanitized[$key] = $Record[$key] }
    $sanitized['source_sha'] = $Context.source_sha
    $sanitized['run_id'] = $Context.run_id
    $sanitized['run_attempt'] = $Context.run_attempt
    $sanitized['requires_separate_post_admission'] = $true
    $sanitized['compiler_artifacts_retained'] = $true
    $text = ConvertTo-Json -InputObject $sanitized -Compress -Depth 5
    if ($strictUtf8.GetByteCount($text) -gt 65536 -or $text -match '[\r\n]') { throw 'record' }
    return @{ text = $text; status = $Record['status'] }
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
        schema_version = 'fixed-git-hardlink-observation-wrapper-v2'
        scope = 'fixed-git-readonly-metadata'; status = 'refused'; code = $failure
        reason = $projection.reason; compiler_code = $projection.compiler_code
        storage_admission = $false; retry_authority = $false; exception_authority = $false
        requires_separate_post_admission = $true; compiler_artifacts_retained = $true
    }
    Write-Host ('K5_GIT_LINK_OBSERVATION=' + ($record | ConvertTo-Json -Compress -Depth 4))
    throw 'Installed Git link observation refused. Existing owners were preserved.'
}
Write-Host ('K5_GIT_LINK_OBSERVATION=' + $result.text)
if ($result.status -cne 'observed') {
    throw 'Installed Git link observation refused. Existing owners were preserved.'
}
