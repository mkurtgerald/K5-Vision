"""Frozen native Windows tools require identical provenance on fallback transport."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOL_STEPS = (
    ".github/actions/provision-operator-tools/action.yml",
    ".github/workflows/operator-product-windows.yml",
)


def test_native_tool_api_fallback_preserves_original_release_and_sha256():
    for path in TOOL_STEPS:
        workflow = (ROOT / path).read_text(encoding="utf-8")
        assert "function Get-ReleaseAssetFallback {" in workflow
        assert "https://api.github.com/repos/$owner/$repository/releases/tags/$tag" in workflow
        assert "$_.browser_download_url -ceq $Url" in workflow
        assert "$_.digest -ceq $expectedDigest" in workflow
        assert '$expectedDigest = "sha256:" + $ExpectedHash.ToLowerInvariant()' in workflow
        assert '"application/octet-stream"' in workflow
        assert "Get-ReleaseAssetFallback -Url $Url -ExpectedHash $ExpectedHash" in workflow
        assert "if ($status -notin @(408, 429, 502, 503, 504))" in workflow
        assert "Native tool archive SHA256 mismatch" in workflow
        assert "Native executable content identity does not match" in workflow
        assert "Native tool archive download was refused" in workflow


def test_native_powershell_launcher_retries_only_transient_failures_with_original_pins():
    """A CDN 504 may not license a different worker, URI or release artifact."""
    script = (ROOT / "scripts/Start-K5Operator.ps1").read_text(encoding="utf-8")
    fallback = script.split("function Get-K5OfficialReleaseArchive {", 1)[1].split(
        "function Get-K5PinnedArchive {", 1
    )[0]
    retry = script.split("function Get-K5PinnedArchive {", 1)[1].split(
        "function Get-K5PinnedTool {", 1
    )[0]
    assert "https://api.github.com/repos/" in fallback
    assert "releases/tags/" in fallback
    assert "$release.tag_name -cne $Version" in fallback
    assert "$_.name -ceq $Filename" in fallback
    assert "$_.browser_download_url -ceq $Url" in fallback
    assert "$_.digest -ceq $digest" in fallback
    assert "'sha256:' + $ExpectedHash" in fallback
    assert "$assets.Count -ne 1" in fallback
    assert "[long]$assets[0].id -le 0" in fallback
    assert "'application/octet-stream'" in fallback
    assert "releases/assets/" in fallback
    assert "for ($attempt = 1; $attempt -le 3; $attempt++)" in retry
    assert "$status -notin @(408, 429, 502, 503, 504)" in retry
    assert "throw 'Pinned native tool download refused.'" in retry
    assert "Get-K5OfficialReleaseArchive -Url $Url -Repository $Repository" in retry
    assert "Remove-Item -LiteralPath $Archive -Force" in retry
    # This applies after either transport path and before the ZIP is inspected.
    install = script.split("function Get-K5PinnedTool {", 1)[1]
    assert "Get-K5PinnedArchive -Url $artifact.url" in install
    assert "if ((Get-K5Sha256 -Path $archive) -cne $artifact.sha256)" in install
    assert install.index("Get-K5Sha256 -Path $archive") < install.index(
        "[IO.Compression.ZipFile]::OpenRead($archive)"
    )
    assert "Get-K5Sha256 -Path $candidate" in install
    assert "Get-K5Sha256 -Path $licenseCandidate" in install
