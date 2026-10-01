[CmdletBinding()]
param(
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA "K5VisionAlpha"),
    [string]$GStreamerVersion = "1.28.7"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
if ($env:OS -ne "Windows_NT") { throw "Windows is required." }

$K5Revision = "d531d50d479f46af6ceed324a7cc379745becb61"
$bootstrapRoot = Join-Path $env:TEMP ("K5VisionBootstrap-" + [Guid]::NewGuid().ToString("N"))
$archivePath = Join-Path $bootstrapRoot "k5vision.zip"
$extractRoot = Join-Path $bootstrapRoot "source"

try {
    New-Item -ItemType Directory -Force -Path $bootstrapRoot | Out-Null
    New-Item -ItemType Directory -Force -Path $extractRoot | Out-Null

    $archiveUri = "https://github.com/mkurtgerald/K5-Vision/archive/$K5Revision.zip"
    Write-Host "Downloading reviewed K5 Vision Alpha revision $K5Revision..."
    Invoke-WebRequest -Uri $archiveUri -OutFile $archivePath -UseBasicParsing
    if (-not (Test-Path -LiteralPath $archivePath)) {
        throw "K5 Vision Alpha source archive download failed."
    }

    Expand-Archive -LiteralPath $archivePath -DestinationPath $extractRoot -Force
    $sourceRoot = Join-Path $extractRoot ("K5-Vision-" + $K5Revision)
    $installer = Join-Path $sourceRoot "scripts\windows-alpha\Install-K5VisionAlpha.ps1"
    if (-not (Test-Path -LiteralPath $installer)) {
        throw "Reviewed K5 Vision Alpha installer was not found in the pinned archive."
    }

    & $installer -InstallRoot $InstallRoot -GStreamerVersion $GStreamerVersion -K5Revision $K5Revision
    if ($LASTEXITCODE -ne 0) {
        throw "K5 Vision Alpha installation failed."
    }

    $installedRevision = Join-Path $InstallRoot "k5-revision.txt"
    if (-not (Test-Path -LiteralPath $installedRevision)) {
        throw "Installed K5 revision record is missing."
    }
    $actualRevision = (Get-Content -LiteralPath $installedRevision -Raw).Trim()
    if ($actualRevision -ne $K5Revision) {
        throw "Installed K5 revision does not match the reviewed alpha revision."
    }

    Write-Host ""
    Write-Host "K5 Vision Alpha installation PASS."
    Write-Host "Launch the 'K5 Vision Alpha' desktop shortcut to run the non-recording local synthetic RTSP operator test."
}
finally {
    if (Test-Path -LiteralPath $bootstrapRoot) {
        Remove-Item -LiteralPath $bootstrapRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}
