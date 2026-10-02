param(
    [string]$Output = "artifacts\stage-one-rtsp-joined-analytics-physical.json"
)

$ErrorActionPreference = "Stop"

foreach ($required in @(
    "K5_ANALYTICS_EVIDENCE_ROOT",
    "K5_GSTREAMER_ROOT",
    "ANALYTICS_LAB_SHA",
    "K5_STAGE_ONE_REVISION"
)) {
    if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($required))) {
        throw "Required Stage One environment variable '$required' is missing."
    }
}

$outputPath = [System.IO.Path]::GetFullPath((Join-Path (Get-Location) $Output))
$outputParent = Split-Path -Parent $outputPath
if (-not (Test-Path -LiteralPath $outputParent)) {
    New-Item -ItemType Directory -Path $outputParent -Force | Out-Null
}
if (Test-Path -LiteralPath $outputPath) {
    Remove-Item -LiteralPath $outputPath -Force
}

$env:K5_STAGE_ONE_RTSP_JOINED_ANALYTICS_PHYSICAL = "1"
$env:K5_STAGE_ONE_RTSP_JOINED_ANALYTICS_OUTPUT = $outputPath

python -m pytest tests/integration/test_stage_one_rtsp_joined_analytics_physical.py -q --no-cov --tb=no --show-capture=no
if ($LASTEXITCODE -ne 0) {
    throw "RTSP-joined Stage One physical witness failed."
}
if (-not (Test-Path -LiteralPath $outputPath)) {
    throw "RTSP-joined Stage One physical witness did not produce a receipt."
}

$receipt = Get-Content -LiteralPath $outputPath -Raw | ConvertFrom-Json
foreach ($field in @(
    "delivered_frames",
    "presentations",
    "analytics_provider_calls",
    "analytics_provider_submissions",
    "analytics_provider_completions",
    "analytics_tracked_detections",
    "analytics_rendered_boxes"
)) {
    if ([int64]$receipt.$field -lt 1) {
        throw "RTSP-joined Stage One receipt failed positive evidence field '$field'."
    }
}
if ([int64]$receipt.analytics_failures -ne 0) {
    throw "RTSP-joined Stage One receipt reported analytics failures."
}

Write-Host "RTSP-joined Stage One witness passed; source-free receipt: $outputPath"
