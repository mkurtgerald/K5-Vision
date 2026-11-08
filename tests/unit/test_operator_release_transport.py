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
