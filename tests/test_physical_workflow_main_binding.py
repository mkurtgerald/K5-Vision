from pathlib import Path

_WORKFLOWS = Path(".github/workflows")
_STAGE_ONE = _WORKFLOWS / "stage-one-operator-physical.yml"


def test_all_self_hosted_physical_qualifications_are_main_bound() -> None:
    physical = tuple(
        path
        for path in sorted(_WORKFLOWS.glob("*.yml"))
        if "runs-on: [self-hosted," in path.read_text(encoding="utf-8")
    )

    assert len(physical) == 26
    for path in physical:
        text = path.read_text(encoding="utf-8")
        admission = text.split("\njobs:\n", maxsplit=1)[1].split("    runs-on:", maxsplit=1)[0]
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
