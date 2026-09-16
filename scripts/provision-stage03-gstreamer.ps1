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

if (-not (Assert-ExpectedRuntime)) {
    New-Item -ItemType Directory -Force -Path $installRoot | Out-Null

    $installerName = "gstreamer-1.0-msvc-x86_64-$Version.exe"
    $installerPath = Join-Path $env:RUNNER_TEMP $installerName
    $downloadUri = "https://gstreamer.freedesktop.org/data/pkg/windows/$Version/msvc/$installerName"

    if (-not (Test-Path -LiteralPath $installerPath)) {
        Invoke-WebRequest -Uri $downloadUri -OutFile $installerPath -UseBasicParsing
    }

    $signature = Get-AuthenticodeSignature -LiteralPath $installerPath
    if ($signature.Status -ne [System.Management.Automation.SignatureStatus]::Valid) {
        Remove-Item -LiteralPath $installerPath -Force -ErrorAction SilentlyContinue
        throw "Downloaded Stage 03 GStreamer installer did not have a valid Authenticode signature."
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
}

$registryPath = Join-Path $env:RUNNER_TEMP "k5-stage03-gstreamer-registry-$Version.bin"
if (-not [string]::IsNullOrWhiteSpace($env:GITHUB_PATH)) {
    Add-Content -LiteralPath $env:GITHUB_PATH -Value $binPath
}
if (-not [string]::IsNullOrWhiteSpace($env:GITHUB_ENV)) {
    Add-Content -LiteralPath $env:GITHUB_ENV -Value "K5_GSTREAMER_ROOT=$installRoot"
    Add-Content -LiteralPath $env:GITHUB_ENV -Value "GST_REGISTRY_1_0=$registryPath"
}

Write-Host "Provisioned isolated Stage 03 GStreamer runtime $Version."
