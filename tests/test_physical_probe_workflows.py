"""Keep private camera sources out of child-process argument vectors."""

from pathlib import Path


_WORKFLOWS = Path(".github/workflows")
_PROBE = "stage03_credential_probe"
_UNSAFE = "stage03_credential_probe $env:K5_STAGE03_SOURCE"
_SAFE = 'stage03_credential_probe "env:K5_STAGE03_SOURCE"'
_EXPECTED_PROBE_WORKFLOWS = {
    "physical-validation.yml",
    "stage04-physical.yml",
    "stage05-physical.yml",
    "stage06-physical.yml",
    "stage07-physical.yml",
    "stage08-physical.yml",
    "stage-one-remaining-physical-suite.yml",
}


def test_physical_probe_workflows_use_only_private_env_handle() -> None:
    probe_workflows: set[str] = set()

    for path in sorted(_WORKFLOWS.glob("*.yml")):
        text = path.read_text(encoding="utf-8")
        if "runs-on: [self-hosted," not in text or _PROBE not in text:
            continue
        probe_workflows.add(path.name)
        assert _UNSAFE not in text, path.name
        assert text.count(_PROBE) == text.count(_SAFE), path.name

    assert _EXPECTED_PROBE_WORKFLOWS.issubset(probe_workflows)
