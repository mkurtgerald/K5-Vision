[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$Output,
    [Parameter(Mandatory=$true)][string]$WorkRoot,
    [string]$Revision = $env:K5_STAGE_ONE_REVISION,
    [string]$AnalyticsSource = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
if ($env:OS -ne "Windows_NT") { throw "Windows is required." }
if ($Revision -cnotmatch '^[0-9a-f]{40}$') { throw "An exact candidate revision is required." }
$repoRoot = Split-Path (Split-Path $PSScriptRoot -Parent) -Parent
if ([string]::IsNullOrWhiteSpace($AnalyticsSource)) {
    $AnalyticsSource = Join-Path $repoRoot "analytics-lab"
}
foreach ($name in @("K5_ANALYTICS_EVIDENCE_ROOT", "K5_GSTREAMER_ROOT", "RUNNER_TEMP", "LOCALAPPDATA")) {
    if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($name))) {
        throw "Required installed witness configuration is missing."
    }
}
# The caller owns the serialized physical allocation and existing idle admission.
# Do not dispatch workflows, provision shared tools, touch the installed Alpha,
# change environment exports, or stop existing processes to make room.
$outputPath = [IO.Path]::GetFullPath($Output)
$driver = Join-Path $repoRoot "scripts\installed_analytics_witness.py"
& python -I -B $driver --repo $repoRoot --analytics-source $AnalyticsSource --revision $Revision --output $outputPath --temp-root $env:RUNNER_TEMP --work-root $WorkRoot
if ($LASTEXITCODE -ne 0) { throw "Installed normal-app analytics witness failed." }
if (-not (Test-Path -LiteralPath $outputPath)) { throw "Installed witness receipt is missing." }
Write-Host "Installed normal-app analytics witness passed; both launches and owned cleanup verified."
