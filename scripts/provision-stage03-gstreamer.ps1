param(
    [string]$Version = "1.28.7"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if ($Version -notmatch '^1\.28\.\d+$') {
    throw "Stage 03 GStreamer version is outside the reviewed stable series."
}

$installBase = if (-not [string]::IsNullOrWhiteSpace($env:RUNNER_TOOL_CACHE)) {
    $env:RUNNER_TOOL_CACHE
} else {
    Join-Path $env:LOCALAPPDATA "K5RunnerTools"
}
$installRoot = Join-Path $installBase "k5-gstreamer\$Version\msvc_x86_64"
$binPath = Join-Path $installRoot "bin"
$gstLaunch = Join-Path $binPath "gst-launch-1.0.exe"
$gstInspect = Join-Path $binPath "gst-inspect-1.0.exe"
$hashRecord = Join-Path $installRoot "k5-installer.sha256"
$installerSha256 = $null

function Assert-ExpectedRuntime {
    if (-not (Test-Path -LiteralPath $gstLaunch) -or -not (Test-Path -LiteralPath $gstInspect)) {
        return $false
    }

    $versionOutput = @(& $gstLaunch --version 2>$null)
    if ($LASTEXITCODE -ne 0) {
        return $false
    }
    if (-not ($versionOutput -join "`n").Contains("GStreamer $Version")) {
        return $false
    }

    foreach ($plugin in @("rtspsrc", "queue", "identity", "fakesink")) {
        & $gstInspect $plugin *> $null
        if ($LASTEXITCODE -ne 0) {
            return $false
        }
    }

    return $true
}

function Get-RecordedInstallerHash {
    if (-not (Test-Path -LiteralPath $hashRecord)) {
        return $null
    }

    $recordedHash = (Get-Content -LiteralPath $hashRecord -Raw).Trim().ToLowerInvariant()
    if ($recordedHash -notmatch '^[0-9a-f]{64}$') {
        return $null
    }
    return $recordedHash
}

$recordedInstallerSha256 = Get-RecordedInstallerHash
$runtimeReady = Assert-ExpectedRuntime

if (-not $runtimeReady -or [string]::IsNullOrWhiteSpace([string]$recordedInstallerSha256)) {
    New-Item -ItemType Directory -Force -Path $installRoot | Out-Null
    $downloadRoot = Join-Path $env:RUNNER_TEMP "k5-stage03-gstreamer-$Version"
    if (Test-Path -LiteralPath $downloadRoot) {
        Remove-Item -LiteralPath $downloadRoot -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $downloadRoot | Out-Null

    $installerName = "gstreamer-1.0-msvc-x86_64-$Version.exe"
    $installerPath = Join-Path $downloadRoot $installerName
    $checksumPath = "$installerPath.sha256sum"
    $baseUri = "https://gstreamer.freedesktop.org/data/pkg/windows/$Version/msvc"
    $installerUri = "$baseUri/$installerName"
    $checksumUri = "$installerUri.sha256sum"

    Invoke-WebRequest -Uri $checksumUri -OutFile $checksumPath -UseBasicParsing
    Invoke-WebRequest -Uri $installerUri -OutFile $installerPath -UseBasicParsing

    $checksumText = (Get-Content -LiteralPath $checksumPath -Raw).Trim()
    if ($checksumText -notmatch '^(?<hash>[0-9A-Fa-f]{64})\s+\*?(?<name>\S+)$') {
        throw "Official Stage 03 GStreamer checksum file had an unexpected format."
    }
    if ($Matches['name'] -ne $installerName) {
        throw "Official Stage 03 GStreamer checksum did not name the expected installer."
    }

    $expectedSha256 = $Matches['hash'].ToLowerInvariant()
    $installerSha256 = (Get-FileHash -LiteralPath $installerPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($installerSha256 -ne $expectedSha256) {
        Remove-Item -LiteralPath $installerPath -Force -ErrorAction SilentlyContinue
        throw "Downloaded Stage 03 GStreamer installer failed the official upstream SHA-256 check."
    }

    $arguments = @(
        "/CURRENTUSER",
        "/TYPE=runtime",
        "/DIR=$installRoot",
        "/VERYSILENT",
        "/NORESTART",
        "/SP-"
    )
    $process = Start-Process -FilePath $installerPath -ArgumentList $arguments -Wait -PassThru
    if ($process.ExitCode -ne 0) {
        throw "Isolated Stage 03 GStreamer installer failed."
    }

    if (-not (Assert-ExpectedRuntime)) {
        throw "Isolated Stage 03 GStreamer runtime failed post-install verification."
    }

    Set-Content -LiteralPath $hashRecord -Value $installerSha256 -Encoding Ascii -NoNewline
} else {
    $installerSha256 = $recordedInstallerSha256
}

$registryPath = Join-Path $env:RUNNER_TEMP "k5-stage03-gstreamer-registry-$Version.bin"
if (-not [string]::IsNullOrWhiteSpace($env:GITHUB_PATH)) {
    Add-Content -LiteralPath $env:GITHUB_PATH -Value $binPath
}
if (-not [string]::IsNullOrWhiteSpace($env:GITHUB_ENV)) {
    Add-Content -LiteralPath $env:GITHUB_ENV -Value "K5_GSTREAMER_ROOT=$installRoot"
    Add-Content -LiteralPath $env:GITHUB_ENV -Value "GST_REGISTRY_1_0=$registryPath"
    if (-not [string]::IsNullOrWhiteSpace([string]$installerSha256)) {
        Add-Content -LiteralPath $env:GITHUB_ENV -Value "K5_GSTREAMER_INSTALLER_SHA256=$installerSha256"
    }
}

Write-Host "Provisioned isolated Stage 03 GStreamer runtime $Version with official upstream SHA-256 verification."
