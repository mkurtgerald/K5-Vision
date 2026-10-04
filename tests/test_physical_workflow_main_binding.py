from pathlib import Path

_WORKFLOWS = Path(".github/workflows")
_STAGE_ONE = _WORKFLOWS / "stage-one-operator-physical.yml"
_INSTALLED_CANDIDATE = _WORKFLOWS / "installed-analytics-candidate.yml"


def test_physical_qualifications_remain_main_bound_except_exact_installed_candidate() -> None:
    physical = tuple(
        path
        for path in sorted(_WORKFLOWS.glob("*.yml"))
        if "runs-on: [self-hosted," in path.read_text(encoding="utf-8")
    )

    assert len(physical) == 27
    assert sum(path != _INSTALLED_CANDIDATE for path in physical) == 26
    for path in physical:
        text = path.read_text(encoding="utf-8")
        admission = text.split("\njobs:\n", maxsplit=1)[1].split("    runs-on:", maxsplit=1)[0]
        if path == _INSTALLED_CANDIDATE:
            assert "github.repository == 'mkurtgerald/K5-Vision'" in admission
            assert (
                "github.ref == 'refs/heads/feat/installed-analytics-operator-20261003'" in admission
            )
            events = text.split("\non:\n", maxsplit=1)[1].split("\nconcurrency:", maxsplit=1)[0]
            assert (
                "  push:\n    branches:\n      - feat/installed-analytics-operator-20261003\n"
                in events
            )
            for forbidden in ("pull_request:", "workflow_dispatch:", "workflow_call:"):
                assert forbidden not in events
            assert "$head.commit.sha -cne $env:K5_EXPECTED_SHA" in text
            assert "K5_EXPECTED_SHA: ${{ github.sha }}" in text
            continue
        if path == _STAGE_ONE:
            assert "github.ref == 'refs/heads/main'" in admission, path.name
            assert "github.actor == github.repository_owner" not in admission, path.name
            assert "github.triggering_actor == github.repository_owner" not in admission, path.name
            continue
        assert "github.event_name == 'workflow_dispatch' &&\n" in admission, path.name
        assert "github.actor == github.repository_owner &&\n" in admission, path.name
        assert "github.triggering_actor == github.repository_owner &&\n" in admission, path.name
        assert "github.ref == 'refs/heads/main' &&\n" in admission, path.name
        assert "inputs.reviewed_sha == github.sha\n" in admission, path.name


def test_stage_one_exact_head_lookup_canonicalizes_reviewed_main_branch() -> None:
    text = _STAGE_ONE.read_text(encoding="utf-8")
    assert "$reviewedBranch = $env:K5_REVIEWED_BRANCH.ToLowerInvariant()" in text
    assert 'if ($reviewedBranch -ne "main") {' in text
    assert "$encodedBranch = [uri]::EscapeDataString($reviewedBranch)" in text
    lookup_line = next(line for line in text.splitlines() if "$encodedBranch =" in line)
    assert "$reviewedBranch" in lookup_line
    assert "$env:K5_REVIEWED_BRANCH" not in lookup_line
