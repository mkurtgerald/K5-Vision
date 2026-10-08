[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('HostedQualify', 'HostedPost', 'PhysicalObserve', 'PhysicalPost')]
    [string]$Mode
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
# Invoked only as independently hash-verified, strict UTF-8 in-memory bytes.
# This fixed one-attempt transport is not installer or retry authority.
$sourceCommit = 'afd444750cec76512bdaa6ff566bdbca2fc62dce'
$repository = 'mkurtgerald/K5-Vision'
$branch = 'review/git-link-metadata-20261008'
$apiRoot = 'https://api.github.com/repos/mkurtgerald/K5-Vision/'
$utf8 = [Text.UTF8Encoding]::new($false, $true)
$pins = @(
    @{ name = 'assert-stage-one-physical-admission.ps1'; size = 7643; blob = '9faae324ffaf008a7dc389aaab2d70198c5f4ea1'; sha256 = 'd7a38b5278802d9ba768d9987b4582a219d490923b0cc4da0c297d29a250b45d' },
    @{ name = 'observe_installed_git_links.cs'; size = 34207; blob = '1e5bc986d255210f5cd82770bb472027c590f8bb'; sha256 = 'eeb76812034a45604fbe43171cecbffcc413af8c31fca611e0f2a75bf202059a' },
    @{ name = 'observe-installed-git-links.ps1'; size = 20889; blob = 'cb919a3bdf6eb7dc85cab9ebc0420603b972f1c8'; sha256 = 'dcea700b6055401dafbbb0e7895b36dc1b7b750b125f282f1f8c5020092ab7e9' }
)

function Assert-OrdinaryPath([string]$Path, [bool]$Directory) {
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

function Read-BoundedStream($Stream, [int]$Limit) {
    $memory = [IO.MemoryStream]::new()
    try {
        $buffer = [byte[]]::new(4096)
        while ($true) {
            $remaining = $Limit - [int]$memory.Length
            if ($remaining -le 0) { throw 'response_size' }
            $read = $Stream.Read($buffer, 0, [Math]::Min($buffer.Length, $remaining))
            if ($read -eq 0) { break }
            $memory.Write($buffer, 0, $read)
        }
        if ($memory.Length -eq 0) { throw 'response_empty' }
        return ,$memory.ToArray()
    } finally { $memory.Dispose() }
}

function Read-FixedApi([string]$Endpoint) {
    $allowed = @('git/ref/heads/' + $branch)
    if ($env:GITHUB_SHA -cmatch '\A[0-9a-f]{40}\z') { $allowed += 'git/commits/' + $env:GITHUB_SHA }
    foreach ($pin in $pins) { $allowed += 'contents/scripts/' + $pin.name + '?ref=' + $sourceCommit }
    if ($Endpoint -cnotin $allowed -or [string]::IsNullOrEmpty($env:K5_GITHUB_TOKEN) -or
        $env:K5_GITHUB_TOKEN.Length -gt 1024 -or $env:K5_GITHUB_TOKEN -match '[\s\x00-\x1f]') { throw 'api_request' }
    $uri = $apiRoot + $Endpoint
    $response = $null
    $stream = $null
    try {
        $request = [Net.HttpWebRequest]::Create($uri)
        $request.Method = 'GET'
        $request.AllowAutoRedirect = $false
        $request.Timeout = 15000
        $request.ReadWriteTimeout = 15000
        $request.MaximumResponseHeadersLength = 16
        $request.AutomaticDecompression = [Net.DecompressionMethods]::None
        $request.Accept = 'application/vnd.github+json'
        $request.UserAgent = 'K5-fixed-git-metadata-source/1'
        $request.Headers['X-GitHub-Api-Version'] = '2022-11-28'
        $request.Headers['Authorization'] = 'Bearer ' + $env:K5_GITHUB_TOKEN
        $response = $request.GetResponse()
        if ([int]$response.StatusCode -ne 200 -or $response.ResponseUri.AbsoluteUri -cne $uri -or
            $response.ContentType -cnotmatch '\Aapplication/json(?:;\s*charset=utf-8)?\z' -or
            -not [string]::IsNullOrEmpty($response.ContentEncoding) -or
            $response.ContentLength -gt 131072) { throw 'api_response' }
        $stream = $response.GetResponseStream()
        $bytes = Read-BoundedStream $stream 131072
        return ConvertFrom-Json -InputObject ($utf8.GetString($bytes)) -ErrorAction Stop
    } finally {
        if ($null -ne $stream) { $stream.Dispose() }
        if ($null -ne $response) { $response.Close() }
    }
}

function Assert-SourceBytes([byte[]]$Bytes, [hashtable]$Pin) {
    if ($Bytes.Length -ne $Pin.size) { throw 'source_size' }
    $sha256 = [Security.Cryptography.SHA256]::Create()
    $sha1 = [Security.Cryptography.SHA1]::Create()
    try {
        $digest = ([BitConverter]::ToString($sha256.ComputeHash($Bytes))).Replace('-', '').ToLowerInvariant()
        if ($digest -cne $Pin.sha256) { throw 'source_hash' }
        $header = [Text.Encoding]::ASCII.GetBytes('blob ' + $Bytes.Length + [char]0)
        $gitBytes = [byte[]]::new($header.Length + $Bytes.Length)
        [Array]::Copy($header, 0, $gitBytes, 0, $header.Length)
        [Array]::Copy($Bytes, 0, $gitBytes, $header.Length, $Bytes.Length)
        $blob = ([BitConverter]::ToString($sha1.ComputeHash($gitBytes))).Replace('-', '').ToLowerInvariant()
        if ($blob -cne $Pin.blob) { throw 'source_blob' }
        $null = $utf8.GetString($Bytes)
    } finally { $sha256.Dispose(); $sha1.Dispose() }
}

function Get-PinnedSource([hashtable]$Pin) {
    $path = 'scripts/' + $Pin.name
    $source = Read-FixedApi ('contents/' + $path + '?ref=' + $sourceCommit)
    if ($source -isnot [pscustomobject] -or $source.type -cne 'file' -or
        $source.path -cne $path -or $source.name -cne $Pin.name -or
        $source.sha -cne $Pin.blob -or
        ($source.size -isnot [int] -and $source.size -isnot [long]) -or $source.size -ne $Pin.size -or
        $source.encoding -cne 'base64' -or $source.content -isnot [string] -or
        $source.content.Length -gt 65536) { throw 'source_metadata' }
    $encoded = $source.content.Replace("`r", '').Replace("`n", '')
    if ($encoded -cnotmatch '\A(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?\z') { throw 'source_encoding' }
    $bytes = [Convert]::FromBase64String($encoded)
    if ([Convert]::ToBase64String($bytes) -cne $encoded) { throw 'source_encoding' }
    Assert-SourceBytes $bytes $Pin
    return ,$bytes
}

function Assert-Context {
    if ($env:OS -cne 'Windows_NT' -or $env:GITHUB_REPOSITORY -cne $repository -or
        $env:GITHUB_REPOSITORY_OWNER -cne 'mkurtgerald' -or $env:GITHUB_EVENT_NAME -cne 'push' -or
        $env:GITHUB_REF -cne ('refs/heads/' + $branch) -or
        $env:GITHUB_ACTOR -cne 'mkurtgerald' -or $env:GITHUB_TRIGGERING_ACTOR -cne 'mkurtgerald' -or
        $env:GITHUB_RUN_ATTEMPT -cne '1' -or $env:GITHUB_RUN_ID -cnotmatch '\A[1-9][0-9]{0,19}\z' -or
        $env:GITHUB_SHA -cnotmatch '\A[0-9a-f]{40}\z' -or $env:K5_DIAGNOSTIC_SHA -cne $env:GITHUB_SHA) { throw 'context' }
    if ($Mode -cin @('HostedQualify', 'HostedPost')) {
        if ($env:GITHUB_JOB -cne 'hosted_qualify' -or $env:RUNNER_ENVIRONMENT -cne 'github-hosted') { throw 'hosted_context' }
    } else {
        if ($env:GITHUB_JOB -cne 'physical_observe' -or $env:RUNNER_ENVIRONMENT -cne 'self-hosted' -or
            $env:RUNNER_NAME -cne 'K5-Physical' -or [Environment]::MachineName -cne 'VLR-CYZ4PK3') { throw 'physical_context' }
    }
    Assert-OrdinaryPath $env:GITHUB_EVENT_PATH $false
    $eventFile = [IO.File]::Open($env:GITHUB_EVENT_PATH, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
    try {
        $eventBytes = Read-BoundedStream $eventFile 1048576
        $event = ConvertFrom-Json -InputObject ($utf8.GetString($eventBytes)) -ErrorAction Stop
    } finally { $eventFile.Dispose() }
    if ($event -isnot [pscustomobject] -or $event.repository.full_name -cne $repository -or
        $event.repository.owner.login -cne 'mkurtgerald' -or $event.sender.login -cne 'mkurtgerald' -or
        $event.ref -cne $env:GITHUB_REF -or $event.after -cne $env:GITHUB_SHA -or
        $event.head_commit.id -cne $env:GITHUB_SHA -or $event.deleted -isnot [bool] -or $event.deleted -ne $false -or
        $event.forced -isnot [bool] -or $event.forced -ne $false) { throw 'event' }
    $ref = Read-FixedApi ('git/ref/heads/' + $branch)
    if ($ref -isnot [pscustomobject] -or $ref.ref -cne $env:GITHUB_REF -or
        $ref.object.type -cne 'commit' -or $ref.object.sha -cne $env:GITHUB_SHA) { throw 'ref' }
    $commit = Read-FixedApi ('git/commits/' + $env:GITHUB_SHA)
    if ($commit -isnot [pscustomobject] -or $commit.sha -cne $env:GITHUB_SHA -or
        $commit.parents -isnot [array] -or $commit.parents.Count -ne 1 -or
        $commit.parents[0].sha -cne $sourceCommit) { throw 'parent' }
}

function Assert-InboxCompiler {
    if ($PSVersionTable.PSEdition -cne 'Desktop' -or $PSVersionTable.PSVersion.Major -ne 5 -or
        $PSVersionTable.PSVersion.Minor -ne 1 -or -not [Environment]::Is64BitProcess -or
        -not [Environment]::Is64BitOperatingSystem) { throw 'runtime' }
    $runtime = [Runtime.InteropServices.RuntimeEnvironment]::GetRuntimeDirectory().TrimEnd('\')
    if ($runtime -cne 'C:\Windows\Microsoft.NET\Framework64\v4.0.30319') { throw 'runtime' }
    Assert-OrdinaryPath $runtime $true
    $compilerPath = Join-Path $runtime 'csc.exe'
    $compiler = Get-Item -LiteralPath $compilerPath -Force -ErrorAction Stop
    if ($compiler.FullName -cne $compilerPath -or $compiler.PSIsContainer -or
        ($compiler.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'runtime' }
    $script:systemCorePath = Join-Path $runtime 'System.Core.dll'
    $systemCore = Get-Item -LiteralPath $script:systemCorePath -Force -ErrorAction Stop
    if ($systemCore.FullName -cne $script:systemCorePath -or $systemCore.PSIsContainer -or
        ($systemCore.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'runtime' }
    if ($null -ne ('K5FixedGitObservation' -as [type])) { throw 'type_reuse' }
    # Existing OS, PowerShell and .NET/compiler/references are explicit trust assumptions.
}

function Assert-CompilerIdle {
    $rows = @(Get-CimInstance -ClassName Win32_Process -Property Name -OperationTimeoutSec 10 -ErrorAction Stop)
    if ($rows.Count -lt 1 -or $rows.Count -gt 32768) { throw 'compiler_inventory' }
    foreach ($row in $rows) {
        if ($row.Name -isnot [string] -or $row.Name.Length -eq 0 -or $row.Name.Length -gt 260 -or
            $row.Name -match '[\x00\\/:\r\n]') { throw 'compiler_inventory' }
        if ($row.Name -iin @('csc.exe', 'cvtres.exe', 'vbc.exe', 'VBCSCompiler.exe')) { throw 'compiler_occupied' }
    }
}

function New-PrivateDirectory([string]$Path) {
    Assert-OrdinaryPath ([IO.Path]::GetDirectoryName($Path)) $true
    if ([IO.File]::Exists($Path) -or [IO.Directory]::Exists($Path) -or
        (Test-Path -LiteralPath $Path -ErrorAction Stop)) { throw 'exists' }
    $created = New-Item -Path $Path -ItemType Directory -ErrorAction Stop
    if ($created.FullName -cne $Path) { throw 'directory' }
    Assert-OrdinaryPath $Path $true
}

function Get-BundleRoot {
    Assert-OrdinaryPath $env:RUNNER_TEMP $true
    return Join-Path $env:RUNNER_TEMP ('k5-git-link-observation-' + $env:GITHUB_RUN_ID + '-' + $env:GITHUB_RUN_ATTEMPT)
}

function New-PinnedBundle {
    if ($Mode -ceq 'HostedQualify') { $script:qualificationPhase = 'bundle_creation' }
    $root = Get-BundleRoot
    New-PrivateDirectory $root
    $scripts = Join-Path $root 'scripts'
    New-PrivateDirectory $scripts
    $sources = @{}
    if ($Mode -ceq 'HostedQualify') { $script:qualificationPhase = 'source_binding' }
    foreach ($pin in $pins) {
        $bytes = Get-PinnedSource $pin
        $path = Join-Path $scripts $pin.name
        Assert-OrdinaryPath $scripts $true
        $file = [IO.File]::Open($path, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
        try { $file.Write($bytes, 0, $bytes.Length); $file.Flush() } finally { $file.Dispose() }
        Assert-OrdinaryPath $path $false
        # Only verified in-memory bytes are ever parsed or invoked by this helper.
        $sources[$pin.name] = $bytes
    }
    return @{ root = $root; scripts = $scripts; sources = $sources }
}

function Assert-CompilerArtifacts([string]$Path) {
    Assert-OrdinaryPath $Path $true
    $entries = [IO.Directory]::EnumerateFileSystemEntries($Path).GetEnumerator()
    $count = 0
    [long]$bytes = 0
    try {
        while ($entries.MoveNext()) {
            $count++
            if ($count -gt 64) { throw 'compiler_artifacts' }
            $entry = [string]$entries.Current
            Assert-OrdinaryPath $entry $false
            $file = Get-Item -LiteralPath $entry -Force -ErrorAction Stop
            $bytes += $file.Length
            if ($file.Length -lt 0 -or $bytes -gt 16777216) { throw 'compiler_artifacts' }
        }
    } finally { $entries.Dispose() }
    # Retained files only: acceptance bounds, not a hard compiler disk quota.
}

function Assert-RetainedArtifacts {
    $root = Get-BundleRoot
    if (Test-Path -LiteralPath $root -ErrorAction Stop) {
        Assert-OrdinaryPath $root $true
        $compilerTemp = Join-Path $root 'compiler-temp'
        if (Test-Path -LiteralPath $compilerTemp -ErrorAction Stop) { Assert-CompilerArtifacts $compilerTemp }
    }
    # Missing bundle/temp can follow an earlier refusal. Post success alone
    # never replaces the workflow's required successful qualification/observation.
}

function Get-HostedFailureProjection([Management.Automation.ErrorRecord]$FailureRecord) {
    $reason = 'unknown'
    $compilerCode = 'unknown'
    # Exact validation literals only; arbitrary error text never leaves this function.
    switch -CaseSensitive ($FailureRecord.Exception.Message) {
        'path' { $reason = 'path' }
        'compiler_inventory' { $reason = 'compiler_inventory' }
        'compiler_occupied' { $reason = 'compiler_occupied' }
        'exists' { $reason = 'exists' }
        'directory' { $reason = 'directory' }
        'response_size' { $reason = 'response_size' }
        'response_empty' { $reason = 'response_empty' }
        'api_request' { $reason = 'api_request' }
        'api_response' { $reason = 'api_response' }
        'source_metadata' { $reason = 'source_metadata' }
        'source_encoding' { $reason = 'source_encoding' }
        'source_size' { $reason = 'source_size' }
        'source_hash' { $reason = 'source_hash' }
        'source_blob' { $reason = 'source_blob' }
        'source_parse' { $reason = 'source_parse' }
        'compiled_type' { $reason = 'compiled_type' }
        'compiler_artifacts' { $reason = 'compiler_artifacts' }
        'compiler_environment' { $reason = 'compiler_environment' }
    }
    if ($script:qualificationPhase -ceq 'compile' -and $FailureRecord.FullyQualifiedErrorId -cin @(
        'SOURCE_CODE_ERROR,Microsoft.PowerShell.Commands.AddTypeCommand',
        'COMPILER_ERRORS,Microsoft.PowerShell.Commands.AddTypeCommand'
    )) {
        $reason = 'compiler_error'
        if ($FailureRecord.TargetObject -is [System.CodeDom.Compiler.CompilerError] -and
            $FailureRecord.TargetObject.ErrorNumber -cmatch '\ACS[0-9]{4}\z') {
            $compilerCode = $FailureRecord.TargetObject.ErrorNumber
        }
    }
    return @{ reason = $reason; compiler_code = $compilerCode }
}

function Invoke-HostedQualification {
    $script:qualificationPhase = 'compiler_idle_pre'
    Assert-CompilerIdle
    $bundle = New-PinnedBundle
    foreach ($name in @('observe-installed-git-links.ps1', 'assert-stage-one-physical-admission.ps1')) {
        $script:qualificationPhase = $(if ($name -ceq 'observe-installed-git-links.ps1') { 'wrapper_parse' } else { 'guard_parse' })
        $tokens = $null
        $errors = $null
        $scriptText = $utf8.GetString($bundle.sources[$name])
        $null = [Management.Automation.Language.Parser]::ParseInput($scriptText, [ref]$tokens, [ref]$errors)
        if ($errors.Count -ne 0) { throw 'source_parse' }
    }
    $script:qualificationPhase = 'compiler_temp'
    $compilerTemp = Join-Path $bundle.root 'compiler-temp'
    New-PrivateDirectory $compilerTemp
    $previousTemp = [Environment]::GetEnvironmentVariable('TEMP', 'Process')
    $previousTmp = [Environment]::GetEnvironmentVariable('TMP', 'Process')
    $compileFailure = $null
    $restoreError = $null
    try {
        [Environment]::SetEnvironmentVariable('TEMP', $compilerTemp, 'Process')
        [Environment]::SetEnvironmentVariable('TMP', $compilerTemp, 'Process')
        $source = $utf8.GetString($bundle.sources['observe_installed_git_links.cs'])
        $script:qualificationPhase = 'compile'
        $types = @(Add-Type -TypeDefinition $source -Language CSharp -PassThru -ReferencedAssemblies @($script:systemCorePath) -ErrorAction Stop -WarningAction SilentlyContinue -Verbose:$false -Debug:$false)
        $script:qualificationPhase = 'compiled_type'
        if (@($types | Where-Object { $_.FullName -ceq 'K5FixedGitObservation' }).Count -ne 1) { throw 'compiled_type' }
        # Do not invoke any collector method, wrapper, guard or native entry point.
    } catch {
        $compileFailure = $_
        throw
    } finally {
        try { [Environment]::SetEnvironmentVariable('TEMP', $previousTemp, 'Process') } catch { if ($null -eq $restoreError) { $restoreError = $_ } }
        try { [Environment]::SetEnvironmentVariable('TMP', $previousTmp, 'Process') } catch { if ($null -eq $restoreError) { $restoreError = $_ } }
        if ($null -ne $restoreError -and $null -eq $compileFailure) {
            $script:qualificationPhase = 'compiler_environment'
            throw [Management.Automation.RuntimeException]::new('compiler_environment', $restoreError.Exception)
        }
    }
    $script:qualificationPhase = 'compiler_idle_post'
    Assert-CompilerIdle
    $script:qualificationPhase = 'compiler_artifacts'
    Assert-CompilerArtifacts $compilerTemp
}

function Invoke-PhysicalPost {
    # Fresh exact original source; independent of a killed observation process,
    # prior bundle, compilation or mutable on-disk script. Never compile here.
    try {
        $guardBytes = Get-PinnedSource $pins[0]
        $guard = [scriptblock]::Create($utf8.GetString($guardBytes))
        & $guard > $null
    } finally {
        # Even a guard refusal must independently check compiler occupancy.
        Assert-CompilerIdle
    }
    Assert-RetainedArtifacts
}

$failure = 'context_binding'
$passed = $false
$failureRecord = $null
$script:qualificationPhase = 'unknown'
$projection = @{ reason = 'unknown'; compiler_code = 'unknown' }
try {
    Assert-Context
    $failure = 'runtime_binding'
    Assert-InboxCompiler
    switch -CaseSensitive ($Mode) {
        'HostedQualify' {
            $failure = 'hosted_qualification'
            Invoke-HostedQualification
        }
        'HostedPost' {
            $failure = 'hosted_post'
            Assert-CompilerIdle
            Assert-RetainedArtifacts
        }
        'PhysicalObserve' {
            $failure = 'physical_preparation'
            $bundle = New-PinnedBundle
            $wrapperText = $utf8.GetString($bundle.sources['observe-installed-git-links.ps1'])
            $failure = 'physical_observation'
            & ([scriptblock]::Create($wrapperText)) -BundleScripts $bundle.scripts
        }
        'PhysicalPost' {
            $failure = 'physical_post'
            Invoke-PhysicalPost
        }
        default { throw 'mode' }
    }
    $passed = $true
} catch {
    $failureRecord = $_
    if ($Mode -ceq 'HostedQualify' -and $failure -ceq 'hosted_qualification') {
        $projection = Get-HostedFailureProjection $failureRecord
    }
    # Keep the original ErrorRecord private, including when environment restoration failed.
    # Never emit exception details, compiler text, source, paths, token, or environment.
}
$record = [ordered]@{
    schema_version = 'fixed-git-metadata-preparation-v1'; mode = $Mode
    status = $(if ($passed) { 'passed' } else { 'refused' })
    code = $(if ($passed) { 'none' } else { $failure })
    phase = $(if ($passed) { 'none' } else { $script:qualificationPhase })
    reason = $(if ($passed) { 'none' } else { $projection.reason })
    compiler_code = $(if ($passed) { 'none' } else { $projection.compiler_code })
    source_commit = $sourceCommit; compiler_artifacts_retained = $true
    storage_admission = $false; retry_authority = $false; exception_authority = $false
}
Write-Host ('K5_GIT_METADATA_PREPARATION=' + ($record | ConvertTo-Json -Compress -Depth 3))
if (-not $passed) { throw 'Fixed Git metadata preparation refused. Existing owners were preserved.' }
