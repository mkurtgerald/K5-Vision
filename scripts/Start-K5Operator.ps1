[CmdletBinding()]
param(
    [string]$PackagePath = (Split-Path -Parent $PSScriptRoot),
    [string]$PythonExecutable = '',
    [string]$RuntimeRoot = (Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'K5VisionOperatorRuntime'),
    [string]$StateRoot = (Join-Path ([Environment]::GetFolderPath('LocalApplicationData')) 'K5VisionOperatorState'),
    [ValidateRange(1024, 65535)][int]$Port = 8000,
    [string]$DiagnosticName = '',
    [switch]$NoBrowser
)

# Native Windows, per-user engineering pilot. Requires installed 64-bit CPython
# 3.12.4+ and network access for package installation and pinned upstream tools.
# This is a foreground supervisor, not a Windows service or accepted Alpha setup.
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$stage = 'platform'
$launcherLock = $null
$result = 1
$script:diagnosticReady = $false
$script:diagnosticPath = $null

function Write-K5LauncherDiagnostic {
    param([int]$ExitCode, [string]$Reason = 'unclassified', [int]$Line = 0)
    if (-not $script:diagnosticReady) { return }
    $stages = @('platform', 'python', 'private-state', 'runtime-installation',
        'venv-admission', 'venv-creation', 'venv-file', 'venv-version',
        'pip-installation', 'native-workers', 'worker-assembly', 'tools-admission',
        'media-manifest', 'gateway-manifest', 'media-cache', 'gateway-cache',
        'identity', 'recordings', 'operator')
    if ($stage -cnotin $stages) { return }
    $reasons = @('unclassified', 'python-operation', 'manifest-admission',
        'artifact-count', 'artifact-admission', 'cached-identity', 'archive-identity',
        'archive-contents', 'extracted-identity', 'license-identity')
    if ($Reason -cnotin $reasons) { $Reason = 'unclassified' }
    if ($Line -lt 0 -or $Line -gt 5000) { $Line = 0 }
    $stream = $null
    try {
        $document = @{ schema_version = '1'; result = 'failed'; stage = $stage;
            reason = $Reason; line = $Line; exit_code = $ExitCode }
        $bytes = [Text.Encoding]::UTF8.GetBytes(($document | ConvertTo-Json -Compress))
        # The basename is fixed grammar and its admitted private parent already
        # exists. Create-new never follows or replaces an existing diagnostic.
        $stream = [IO.File]::Open($script:diagnosticPath, [IO.FileMode]::CreateNew,
            [IO.FileAccess]::Write, [IO.FileShare]::None)
        $stream.Write($bytes, 0, $bytes.Length)
        $stream.Flush($true)
    } catch {
        # This optional finite diagnostic cannot expose raw native/Python output.
    } finally { if ($null -ne $stream) { $stream.Dispose() } }
}

function Invoke-K5PythonText {
    param([string]$Python, [string[]]$Arguments)
    if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) { throw 'Python executable required.' }
    # Windows PowerShell 5.1 strips embedded double quotes in native -c argv.
    # Transport our fixed helper code as base64; paths remain separate arguments.
    $Arguments = @($Arguments)
    for ($index = 0; $index -lt $Arguments.Count - 1; $index++) {
        if ($Arguments[$index] -ceq '-c') {
            $encoded = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($Arguments[$index + 1]))
            $Arguments[$index + 1] = "exec(__import__('base64').b64decode('$encoded'))"
            break
        }
    }
    $previousErrorAction = $ErrorActionPreference
    try {
        # PowerShell 5.1 turns native stderr into error records. Collect them
        # privately and use the process exit code, including harmless pip warnings.
        $ErrorActionPreference = 'Continue'
        $output = @(& $Python @Arguments 2>&1)
        $exitCode = $LASTEXITCODE
    } finally { $ErrorActionPreference = $previousErrorAction }
    if ($exitCode -ne 0) { throw 'K5 Python operation failed.' }
    return ($output -join "`n").Trim()
}

function Assert-K5Python {
    param([string]$Python)
    $code = 'import json,struct,sys; assert sys.platform == "win32" and sys.version_info[:2] == (3,12) and sys.version_info.micro >= 4 and struct.calcsize("P") == 8; print(json.dumps(sys.executable,ensure_ascii=True))'
    # ASCII JSON preserves Unicode account paths across PowerShell 5.1 code pages.
    return (Invoke-K5PythonText -Python $Python -Arguments @('-I', '-B', '-c', $code)) | ConvertFrom-Json
}

function Assert-K5PrivateDirectory {
    param([string]$Python, [string]$Helper, [string]$Path)
    # Reuse the existing stdlib-only native security inspector before pip or
    # project imports. Existing ACLs are inspected, never repaired or widened.
    $code = @'
import importlib.util,os,stat,sys
from pathlib import Path
spec=importlib.util.spec_from_file_location("k5_launcher_security",sys.argv[1])
security=importlib.util.module_from_spec(spec)
sys.modules[spec.name]=security
spec.loader.exec_module(security)
path=Path(sys.argv[2])
security.check_windows_volume(path)
for component in reversed((path,*path.parents)):
    try: info=component.lstat()
    except FileNotFoundError: continue
    if stat.S_ISLNK(info.st_mode) or getattr(info,"st_file_attributes",0)&0x400:
        raise ValueError("Linked path refused")
    if not stat.S_ISDIR(info.st_mode): raise ValueError("Directory required")
    private=component in (path,path.parent)
    security.check_windows_security(component,private=private,require_file_inheritance=private)
if not path.exists():
    if not path.parent.is_dir(): raise ValueError("Existing private parent required")
    path.mkdir(mode=0o700)
security.check_windows_security(path,private=True,require_file_inheritance=True)
'@
    $null = Invoke-K5PythonText -Python $Python -Arguments @('-I', '-B', '-c', $code, $Helper, $Path)
}

function Assert-K5PrivateFile {
    param([string]$Python, [string]$Helper, [string]$Path)
    $code = @'
import importlib.util,stat,sys
from pathlib import Path
spec=importlib.util.spec_from_file_location("k5_launcher_security",sys.argv[1])
security=importlib.util.module_from_spec(spec)
sys.modules[spec.name]=security
spec.loader.exec_module(security)
path=Path(sys.argv[2]);security.check_windows_volume(path)
for component in reversed((path,*path.parents)):
    info=component.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info,"st_file_attributes",0)&0x400:
        raise ValueError("Linked path refused")
    if component==path:
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:
            raise ValueError("Ordinary unlinked file required")
        security.check_windows_security(component,private=True)
    else:
        if not stat.S_ISDIR(info.st_mode): raise ValueError("Directory required")
        private=component==path.parent
        security.check_windows_security(component,private=private,require_file_inheritance=private)
'@
    $null = Invoke-K5PythonText -Python $Python -Arguments @('-I', '-B', '-c', $code, $Helper, $Path)
}

function Get-K5InstalledManifest {
    param([string]$Python, [string]$Name)
    $code = 'import importlib.resources,sys; print(importlib.resources.files("k5vision").joinpath("data",sys.argv[1]).read_text(encoding="utf-8"))'
    $text = Invoke-K5PythonText -Python $Python -Arguments @('-I', '-B', '-c', $code, $Name)
    return $text | ConvertFrom-Json
}

function Get-K5Sha256 {
    param([string]$Path)
    # Use .NET directly: the native 5.1 starter must not depend on hash-module
    # autoload behavior inherited from a PowerShell 7 parent process.
    $stream = $null
    $algorithm = $null
    try {
        $stream = [IO.File]::Open($Path, [IO.FileMode]::Open,
            [IO.FileAccess]::Read, [IO.FileShare]::Read)
        $algorithm = [Security.Cryptography.SHA256]::Create()
        return [BitConverter]::ToString($algorithm.ComputeHash($stream)).Replace('-', '').ToLowerInvariant()
    } finally {
        if ($null -ne $algorithm) { $algorithm.Dispose() }
        if ($null -ne $stream) { $stream.Dispose() }
    }
}

function Get-K5PinnedTool {
    param(
        [string]$Python, [string]$Helper, [string]$ToolsRoot,
        [object]$Manifest, [string]$Component, [string]$Repository, [string]$Executable
    )
    if ($Manifest.schema_version -cne '1' -or $Manifest.component -cne $Component -or
        $Manifest.version -cnotmatch '^v[0-9]+\.[0-9]+\.[0-9]+$' -or
        $Manifest.source -cne ('https://github.com/' + $Repository)) {
        throw 'Native tool manifest admission failed.'
    }
    $artifacts = @($Manifest.artifacts | Where-Object { $_.platform -ceq 'windows_amd64' })
    if ($artifacts.Count -ne 1) { throw 'Exactly one Windows artifact required.' }
    $artifact = $artifacts[0]
    if ($artifact.executable -cne $Executable -or
        $artifact.sha256 -cnotmatch '^[0-9a-f]{64}$' -or
        $artifact.executable_sha256 -cnotmatch '^[0-9a-f]{64}$' -or
        $Manifest.license_sha256 -cnotmatch '^[0-9a-f]{64}$' -or
        $artifact.filename -cnotmatch '^[A-Za-z0-9_.-]+\.zip$' -or
        $artifact.url -cne ('https://github.com/' + $Repository + '/releases/download/' + $Manifest.version + '/' + $artifact.filename)) {
        throw 'Native tool artifact admission failed.'
    }
    $componentRoot = Join-Path $ToolsRoot $Component
    Assert-K5PrivateDirectory -Python $Python -Helper $Helper -Path $componentRoot
    $cache = Join-Path $componentRoot $Manifest.version
    Assert-K5PrivateDirectory -Python $Python -Helper $Helper -Path $cache
    $target = Join-Path $cache $Executable
    $license = Join-Path $cache 'LICENSE'
    if (Test-Path -LiteralPath $target) {
        Assert-K5PrivateFile -Python $Python -Helper $Helper -Path $target
        Assert-K5PrivateFile -Python $Python -Helper $Helper -Path $license
        if ((Get-K5Sha256 -Path $target) -cne $artifact.executable_sha256 -or
            (Get-K5Sha256 -Path $license) -cne $Manifest.license_sha256) {
            throw 'Cached native tool identity mismatch.'
        }
        return $target
    }
    $nonce = [Guid]::NewGuid().ToString('N')
    $archive = Join-Path $cache ('download-' + $nonce + '.zip')
    $candidate = Join-Path $cache ('candidate-' + $nonce + '.exe')
    $licenseCandidate = Join-Path $cache ('license-' + $nonce + '.txt')
    $package = $null
    try {
        $previousTls = [Net.ServicePointManager]::SecurityProtocol
        try {
            [Net.ServicePointManager]::SecurityProtocol = $previousTls -bor [Net.SecurityProtocolType]::Tls12
            Invoke-WebRequest -UseBasicParsing -TimeoutSec 120 -Uri $artifact.url -OutFile $archive
        } finally { [Net.ServicePointManager]::SecurityProtocol = $previousTls }
        if ((Get-K5Sha256 -Path $archive) -cne $artifact.sha256) {
            throw 'Native archive identity mismatch.'
        }
        $package = [IO.Compression.ZipFile]::OpenRead($archive)
        $binaryEntries = @($package.Entries | Where-Object { $_.FullName -ceq $Executable })
        $licenseEntries = @($package.Entries | Where-Object { $_.FullName -ceq 'LICENSE' })
        if ($binaryEntries.Count -ne 1 -or $licenseEntries.Count -ne 1 -or
            $binaryEntries[0].Length -ne $artifact.executable_bytes -or
            $binaryEntries[0].Length -gt 134217728 -or $licenseEntries[0].Length -gt 65536) {
            throw 'Native archive contents refused.'
        }
        # Extract only exact fixed entry names, never arbitrary archive paths.
        [IO.Compression.ZipFileExtensions]::ExtractToFile($binaryEntries[0], $candidate, $false)
        [IO.Compression.ZipFileExtensions]::ExtractToFile($licenseEntries[0], $licenseCandidate, $false)
        Assert-K5PrivateFile -Python $Python -Helper $Helper -Path $candidate
        Assert-K5PrivateFile -Python $Python -Helper $Helper -Path $licenseCandidate
        if ((Get-K5Sha256 -Path $candidate) -cne $artifact.executable_sha256 -or
            (Get-K5Sha256 -Path $licenseCandidate) -cne $Manifest.license_sha256) {
            throw 'Extracted native tool identity mismatch.'
        }
        if (Test-Path -LiteralPath $license) {
            Assert-K5PrivateFile -Python $Python -Helper $Helper -Path $license
            if ((Get-K5Sha256 -Path $license) -cne $Manifest.license_sha256) {
                throw 'Existing native license identity mismatch.'
            }
        } else { [IO.File]::Move($licenseCandidate, $license) }
        [IO.File]::Move($candidate, $target)
        return $target
    } finally {
        if ($null -ne $package) { $package.Dispose() }
        foreach ($owned in @($archive, $candidate, $licenseCandidate)) {
            if (Test-Path -LiteralPath $owned) { Remove-Item -LiteralPath $owned -Force }
        }
    }
}

try {
    if ($env:OS -cne 'Windows_NT' -or -not [Environment]::Is64BitOperatingSystem) {
        throw 'Native 64-bit Windows required.'
    }
    $stage = 'python'
    if ([string]::IsNullOrWhiteSpace($PythonExecutable)) {
        $py = Get-Command py.exe -ErrorAction SilentlyContinue
        if ($null -ne $py) {
            $PythonExecutable = (Invoke-K5PythonText -Python $py.Source -Arguments @('-3.12', '-I', '-B', '-c', 'import json,sys; print(json.dumps(sys.executable,ensure_ascii=True))')) | ConvertFrom-Json
        } else {
            $python = Get-Command python.exe -ErrorAction Stop
            $PythonExecutable = $python.Source
        }
    }
    $PythonExecutable = Assert-K5Python -Python $PythonExecutable
    $stage = 'private-state'
    $project = Split-Path -Parent $PSScriptRoot
    $helper = Join-Path $project 'src\k5vision\windows_identity_security.py'
    if (-not (Test-Path -LiteralPath $helper -PathType Leaf)) { throw 'Complete launcher project required.' }
    $PackagePath = (Resolve-Path -LiteralPath $PackagePath).ProviderPath
    $package = Get-Item -LiteralPath $PackagePath
    if (($package.PSIsContainer -and -not (Test-Path -LiteralPath (Join-Path $PackagePath 'pyproject.toml'))) -or
        (-not $package.PSIsContainer -and $package.Extension -cne '.whl')) {
        throw 'Local project or wheel required.'
    }
    $separation = @'
from pathlib import Path
import sys
roots=[Path(value) for value in sys.argv[1:]]
for path in roots:
    if not path.is_absolute() or ".." in path.parts: raise ValueError("Absolute ordinary root required")
runtime,state,project,package=roots
for other in (runtime,project,package):
    if state.is_relative_to(other) or other.is_relative_to(state): raise ValueError("State must be separate")
if runtime.is_relative_to(project) or project.is_relative_to(runtime): raise ValueError("Runtime must be separate")
'@
    $null = Invoke-K5PythonText -Python $PythonExecutable -Arguments @('-I', '-B', '-c', $separation, $RuntimeRoot, $StateRoot, $project, $PackagePath)
    Assert-K5PrivateDirectory -Python $PythonExecutable -Helper $helper -Path $RuntimeRoot
    if ($DiagnosticName) {
        if ($DiagnosticName -cnotmatch '^launcher-diagnostic-[0-9a-f]{32}\.json$') {
            throw 'Diagnostic basename refused.'
        }
        $script:diagnosticPath = Join-Path $RuntimeRoot $DiagnosticName
        $script:diagnosticReady = $true
    }
    Assert-K5PrivateDirectory -Python $PythonExecutable -Helper $helper -Path $StateRoot
    $lockPath = Join-Path $RuntimeRoot 'operator-launcher.lock'
    if (Test-Path -LiteralPath $lockPath) {
        Assert-K5PrivateFile -Python $PythonExecutable -Helper $helper -Path $lockPath
    }
    $launcherLock = [IO.File]::Open($lockPath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    $stage = 'runtime-installation'
    Write-Host 'Preparing the private K5 operator runtime...'
    $venv = Join-Path $RuntimeRoot '.venv'
    $python = Join-Path $venv 'Scripts\python.exe'
    $venvExists = Test-Path -LiteralPath $venv
    $stage = 'venv-admission'
    Assert-K5PrivateDirectory -Python $PythonExecutable -Helper $helper -Path $venv
    if (-not $venvExists) {
        $stage = 'venv-creation'
        $null = Invoke-K5PythonText -Python $PythonExecutable -Arguments @('-I', '-B', '-m', 'venv', $venv)
    }
    $stage = 'venv-file'
    Assert-K5PrivateFile -Python $PythonExecutable -Helper $helper -Path $python
    $stage = 'venv-version'
    $null = Assert-K5Python -Python $python
    $stage = 'venv-admission'
    Assert-K5PrivateDirectory -Python $python -Helper $helper -Path $venv
    $stage = 'pip-installation'
    $null = Invoke-K5PythonText -Python $python -Arguments @('-I', '-B', '-m', 'pip', 'install', '--no-input', '--disable-pip-version-check', '--quiet', '--upgrade', $PackagePath)
    $stage = 'native-workers'
    Write-Host 'Preparing verified native Windows media workers...'
    $stage = 'worker-assembly'
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $tools = Join-Path $RuntimeRoot 'tools'
    $stage = 'tools-admission'
    Assert-K5PrivateDirectory -Python $python -Helper $helper -Path $tools
    $stage = 'media-manifest'
    $mediaManifest = Get-K5InstalledManifest -Python $python -Name 'mediamtx-donor-manifest.json'
    $stage = 'gateway-manifest'
    $gatewayManifest = Get-K5InstalledManifest -Python $python -Name 'caddy-donor-manifest.json'
    $stage = 'media-cache'
    $media = Get-K5PinnedTool -Python $python -Helper $helper -ToolsRoot $tools -Manifest $mediaManifest -Component 'MediaMTX' -Repository 'bluenviron/mediamtx' -Executable 'mediamtx.exe'
    $stage = 'gateway-cache'
    $gateway = Get-K5PinnedTool -Python $python -Helper $helper -ToolsRoot $tools -Manifest $gatewayManifest -Component 'Caddy' -Repository 'caddyserver/caddy' -Executable 'caddy.exe'
    $stage = 'identity'
    $identity = Join-Path $StateRoot 'identity'
    if (-not (Test-Path -LiteralPath $identity)) {
        Write-Host 'Create the local recovery-admin account. The password stays off the command line.'
        & $python -I -B -m k5vision.cli setup-admin --identity-dir $identity
        if ($LASTEXITCODE -ne 0) { throw 'Recovery administrator setup incomplete.' }
    }
    # Existing or interrupted identity state is never reset or reinitialized.
    $recordings = Join-Path $StateRoot 'recordings'
    $stage = 'recordings'
    Assert-K5PrivateDirectory -Python $python -Helper $helper -Path $recordings
    $stage = 'operator'
    Write-Host 'Starting the local operator. Keep this window open; Ctrl+C stops the pilot.'
    $arguments = @('-I', '-B', '-m', 'k5vision.cli', 'operator', '--identity-dir', $identity, '--recording-root', $recordings, '--media-exe', $media, '--gateway-exe', $gateway, '--port', [string]$Port)
    if (-not $NoBrowser) { $arguments += '--open-browser' }
    & $python @arguments
    $result = $LASTEXITCODE
    if ($result -ne 0) { Write-K5LauncherDiagnostic -ExitCode $result }
} catch {
    # Never echo Python, pip, configuration or raw source errors into the owner flow.
    # Only exact messages thrown by our own helpers map to public finite codes.
    # Native, library and unrecognized exception text is discarded.
    $reason = switch -CaseSensitive -Exact ($_.Exception.Message) {
        'K5 Python operation failed.' { 'python-operation' }
        'Native tool manifest admission failed.' { 'manifest-admission' }
        'Exactly one Windows artifact required.' { 'artifact-count' }
        'Native tool artifact admission failed.' { 'artifact-admission' }
        'Cached native tool identity mismatch.' { 'cached-identity' }
        'Native archive identity mismatch.' { 'archive-identity' }
        'Native archive contents refused.' { 'archive-contents' }
        'Extracted native tool identity mismatch.' { 'extracted-identity' }
        'Existing native license identity mismatch.' { 'license-identity' }
        default { 'unclassified' }
    }
    Write-Host ('K5 operator launch failed at ' + $stage + '. Existing identity and recordings were preserved.')
    $result = 1
    Write-K5LauncherDiagnostic -ExitCode $result -Reason $reason -Line $_.InvocationInfo.ScriptLineNumber
} finally {
    if ($null -ne $launcherLock) { $launcherLock.Dispose() }
}
exit $result
