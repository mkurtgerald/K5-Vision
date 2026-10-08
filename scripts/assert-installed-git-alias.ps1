[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [byte[]]$SourceBytes,
    [Parameter(Mandatory = $true)]
    [ValidateNotNull()]
    [scriptblock]$AssertBoundary,
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$CompilerTemp
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
# Invoke these independently pinned helper bytes in memory once per own inbox
# PowerShell process. SourceBytes must arrive via independently verified transport,
# never through unadmitted Git. There is no user-selectable Git path or receipt.
# The caller supplies a trusted workflow boundary block running the original,
# pinned physical guard. The returned invoker opens a NEW held session per call.
# Its Operation is a trusted workflow block, containing exactly one synchronous
# direct Git call (or no call for the approved checkout before/after boundary).
# Operation owns its native deadline/exit checks and emits only bounded, sanitized
# success data. It must not detach children or emit host/progress/diagnostic text.
# The operating system/device map, inbox PowerShell/.NET/compiler/System.Core and
# this trusted workflow remain explicit trust assumptions. No process-image or
# continuous cross-step lease is asserted for actions/checkout.
# Existing compiler private-temp effects are retained without manual cleanup.
# REQUIRED: independently pinned same-host guard/compiler-idle/file-only artifact
# post-admission after this PowerShell process exits, even on failure or timeout.
$sourceSha256 = 'a751156ee06792f8f4625fa988c73a54c8e9a0a24937c06ab9264762f965d23f'

function Assert-K5AliasOrdinaryPath([string]$Path, [bool]$Directory) {
    if ([string]::IsNullOrEmpty($Path) -or $Path.Length -gt 1024 -or
        $Path -cnotmatch '\A[A-Z]:\\' -or $Path -match '[\x00-\x1f"<>|?*]' -or
        $Path.Substring(2).Contains(':') -or [IO.Path]::GetFullPath($Path) -cne $Path) { throw 'path' }
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

function Assert-K5AliasCompilerIdle {
    # Names only; no executable paths, command lines, owners, or PID signaling.
    $rows = @(Get-CimInstance -ClassName Win32_Process -Property Name -OperationTimeoutSec 10 -ErrorAction Stop)
    if ($rows.Count -lt 1 -or $rows.Count -gt 32768) { throw 'compiler_inventory' }
    foreach ($row in $rows) {
        if ($row.Name -isnot [string] -or $row.Name.Length -eq 0 -or $row.Name.Length -gt 260 -or
            $row.Name -match '[\x00\\/:\r\n]') { throw 'compiler_inventory' }
        if ($row.Name -iin @('csc.exe', 'cvtres.exe', 'vbc.exe', 'VBCSCompiler.exe')) { throw 'compiler_occupied' }
    }
}

function Assert-K5AliasInboxCompiler {
    if ($PSVersionTable.PSEdition -cne 'Desktop' -or
        $PSVersionTable.PSVersion.Major -ne 5 -or $PSVersionTable.PSVersion.Minor -ne 1 -or
        -not [Environment]::Is64BitProcess -or -not [Environment]::Is64BitOperatingSystem) { throw 'runtime' }
    $runtime = [Runtime.InteropServices.RuntimeEnvironment]::GetRuntimeDirectory().TrimEnd('\')
    if ($runtime -cne 'C:\Windows\Microsoft.NET\Framework64\v4.0.30319') { throw 'runtime' }
    Assert-K5AliasOrdinaryPath $runtime $true
    foreach ($name in @('csc.exe', 'System.Core.dll')) {
        $path = Join-Path $runtime $name
        $item = Get-Item -LiteralPath $path -Force -ErrorAction Stop
        if ($item.FullName -cne $path -or $item.PSIsContainer -or
            ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'runtime' }
    }
    # System files keep the already accepted inbox trust, including legitimate
    # Windows hardlinks. This is not a new system-binary attestation framework.
    if ($null -ne ('K5ExactGitAlias' -as [type])) { throw 'type_reuse' }
    return Join-Path $runtime 'System.Core.dll'
}

function Get-K5AliasNativeRefusal([object]$Value) {
    # Only this fixed projection can leave the private native failure boundary.
    # Inspect at most the known PowerShell method wrapper and exact exception
    # type emitted by Begin; do not walk arbitrary exception graphs or stringify.
    $candidate = $null
    try {
        if ($Value -is [string]) { $candidate = $Value }
        elseif ($Value -is [Management.Automation.ErrorRecord]) {
            $exception = $Value.Exception
            if ($null -ne $exception -and
                $exception.GetType() -eq [Management.Automation.MethodInvocationException]) {
                $exception = $exception.InnerException
            }
            if ($null -ne $exception -and $exception.GetType() -eq [InvalidOperationException] -and
                $null -eq $exception.InnerException) { $candidate = $exception.Message }
        }
        $codes = @(
            'none', 'platform', 'abi', 'anchor_open', 'relative_open', 'metadata',
            'filesystem', 'reparse', 'path_type', 'acl_unavailable', 'acl_null',
            'alias_outside_root', 'alias_duplicate', 'alias_missing_primary', 'alias_count',
            'identity_mismatch', 'path_shape', 'name_bound', 'file_size', 'bytes_bound',
            'time_bound', 'enumeration', 'changed', 'read_failed', 'handle_bound',
            'cleanup_failed', 'exact_pair', 'hash_mismatch', 'acl_mismatch', 'session_closed', 'internal'
        )
        if ($candidate -is [string] -and $candidate -cin $codes) { return $candidate }
    } catch { return 'unknown' }
    return 'unknown'
}

$failure = $null
$failureError = $null
$phase = 'source_binding'
$boundaryReached = $false
$aliasType = $null
$sha256 = $null
try {
    if ($SourceBytes.Length -ne 36598) { throw 'source_size' }
    # Clone the verified array before hashing and decoding; later caller mutation
    # cannot alter the bytes supplied to the compiler after this check.
    $privateBytes = [byte[]]$SourceBytes.Clone()
    $sha256 = [Security.Cryptography.SHA256]::Create()
    $actual = ([BitConverter]::ToString($sha256.ComputeHash($privateBytes))).Replace('-', '').ToLowerInvariant()
    if ($actual -cne $sourceSha256) { throw 'source_hash' }
    $sourceText = [Text.UTF8Encoding]::new($false, $true).GetString($privateBytes)
    $phase = 'pre_compile_boundary'
    $boundaryReached = $true
    & $AssertBoundary > $null
    Assert-K5AliasCompilerIdle
    $phase = 'compiler_binding'
    $systemCorePath = Assert-K5AliasInboxCompiler
    # The caller cannot select another compiler root or another action's folder.
    if ($env:GITHUB_RUN_ID -cnotmatch '\A[1-9][0-9]{0,19}\z' -or
        $env:GITHUB_RUN_ATTEMPT -cnotmatch '\A[1-9][0-9]{0,5}\z' -or
        $env:GITHUB_ACTION -cnotmatch '\A[A-Za-z_][A-Za-z0-9_-]{0,79}\z') { throw 'compiler_context' }
    Assert-K5AliasOrdinaryPath $env:RUNNER_TEMP $true
    Assert-K5AliasOrdinaryPath $env:RUNNER_WORKSPACE $true
    $temporary = [IO.DirectoryInfo]::new($env:RUNNER_TEMP)
    $workspace = [IO.DirectoryInfo]::new($env:RUNNER_WORKSPACE)
    if ($temporary.Name -cne '_temp' -or $workspace.Name -cne 'K5-Vision' -or
        $temporary.Parent.FullName -cne $workspace.Parent.FullName) { throw 'compiler_context' }
    $expectedTemp = Join-Path $temporary.FullName ('k5-installed-git-alias-' +
        $env:GITHUB_RUN_ID + '-' + $env:GITHUB_RUN_ATTEMPT + '-' + $env:GITHUB_ACTION)
    if ($CompilerTemp -cne $expectedTemp) { throw 'compiler_context' }
    Assert-K5AliasOrdinaryPath $CompilerTemp $true
    # The workflow creates this unique private directory without Force/overwrite.
    # Only prove it is initially empty. Final artifact admission MUST run later.
    $entries = [IO.Directory]::EnumerateFileSystemEntries($CompilerTemp).GetEnumerator()
    $entryError = $null
    try { if ($entries.MoveNext()) { throw 'compiler_temp_occupied' } }
    catch { $entryError = $_; throw }
    finally {
        try { $entries.Dispose() } catch { if ($null -eq $entryError) { throw } }
    }
    $phase = 'compile'
    $previousTemp = [Environment]::GetEnvironmentVariable('TEMP', 'Process')
    $previousTmp = [Environment]::GetEnvironmentVariable('TMP', 'Process')
    $compileError = $null
    $restoreError = $null
    try {
        [Environment]::SetEnvironmentVariable('TEMP', $CompilerTemp, 'Process')
        [Environment]::SetEnvironmentVariable('TMP', $CompilerTemp, 'Process')
        $types = @(Add-Type -TypeDefinition $sourceText -Language CSharp -PassThru -ReferencedAssemblies $systemCorePath -ErrorAction Stop -WarningAction SilentlyContinue -Verbose:$false -Debug:$false)
        $matches = @($types | Where-Object { $_.FullName -ceq 'K5ExactGitAlias' })
        if ($matches.Count -ne 1) { throw 'compiled_type' }
        $aliasType = $matches[0]
    } catch { $compileError = $_; throw }
    finally {
        # Preserve the original private failure and attempt both restorations.
        try { [Environment]::SetEnvironmentVariable('TEMP', $previousTemp, 'Process') } catch { $restoreError = $_ }
        try { [Environment]::SetEnvironmentVariable('TMP', $previousTmp, 'Process') } catch { if ($null -eq $restoreError) { $restoreError = $_ } }
        if ($null -ne $restoreError -and $null -eq $compileError) {
            throw [Management.Automation.RuntimeException]::new('compiler_environment', $restoreError.Exception)
        }
    }
} catch {
    $failureError = $_ # Keep the original ErrorRecord private; never emit it.
    $failure = $phase
}
finally {
    if ($null -ne $sha256) {
        try { $sha256.Dispose() }
        catch {
            if ($null -eq $failureError) { $failureError = $_ }
            if ($null -eq $failure) { $failure = 'hash_cleanup' }
        }
    }
    if ($boundaryReached) {
        try { & $AssertBoundary > $null; Assert-K5AliasCompilerIdle }
        catch {
            if ($null -eq $failureError) { $failureError = $_ }
            if ($null -eq $failure) { $failure = 'post_compile_boundary' }
        }
    }
}
if ($null -ne $failure) { throw ('fixed_git_alias_' + $failure) }

# Capture the verified Type, trusted guard and compiler check directly. No global
# type-name lookup, environment receipt or source path is consulted by a call.
$compilerIdle = ${function:Assert-K5AliasCompilerIdle}
$nativeProjection = ${function:Get-K5AliasNativeRefusal}
$invoker = {
    param(
        [Parameter(Mandatory = $true)]
        [ValidateNotNull()]
        [scriptblock]$Operation
    )
    $ErrorActionPreference = 'Stop'
    Set-StrictMode -Version Latest
    $session = $null
    $buffer = $null
    $failure = $null
    $failureError = $null
    $nativeCode = 'unknown'
    $phase = 'pre_operation_boundary'
    try {
        & $AssertBoundary > $null
        & $compilerIdle
        $phase = 'proof_begin'
        $session = $aliasType::Begin()
        $phase = 'operation'
        # Only success output may escape, and only after revalidation/cleanup.
        $buffer = @(& $Operation 'C:\Program Files\Git\cmd\git.exe' 2>$null 3>$null 4>$null 5>$null 6>$null)
    } catch {
        $failureError = $_ # Keep first underlying error private across all cleanup.
        $failure = $phase
        if ($phase -ceq 'proof_begin') { $nativeCode = & $nativeProjection $failureError }
    }
    finally {
        if ($null -ne $session) {
            try {
                $completion = $session.Complete()
                if ($completion -cne 'none' -and $null -eq $failure) {
                    $failure = 'proof_complete'
                    $nativeCode = & $nativeProjection $completion
                }
            } catch {
                if ($null -eq $failureError) { $failureError = $_ }
                if ($null -eq $failure) {
                    $failure = 'proof_complete'
                    $nativeCode = & $nativeProjection $failureError
                }
            }
        }
        # Always retry the trusted final checks, preserving the primary failure.
        try { & $AssertBoundary > $null; & $compilerIdle }
        catch {
            if ($null -eq $failureError) { $failureError = $_ }
            if ($null -eq $failure) { $failure = 'post_operation_boundary' }
        }
    }
    if ($null -ne $failure) {
        # These are fixed phase/refusal literals only. Private ErrorRecord details,
        # paths, compiler output, callback stderr and output never reach the caller.
        throw ('fixed_git_alias_' + $failure + ':' + $nativeCode)
    }
    foreach ($item in $buffer) { Write-Output -NoEnumerate $item }
}.GetNewClosure()
return $invoker
