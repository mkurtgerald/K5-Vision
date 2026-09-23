"""Keep private camera sources out of child-process argument vectors."""

from pathlib import Path

_WORKFLOWS = Path(".github/workflows")
_PROBE = "stage03_credential_probe"
_UNSAFE = "stage03_credential_probe $env:K5_STAGE03_SOURCE"
_SAFE = 'stage03_credential_probe "env:K5_STAGE03_SOURCE"'


def test_physical_probe_workflows_use_only_private_env_handle() -> None:
    probe_workflows: list[str] = []
    unsafe_workflows: list[str] = []

    for path in sorted(_WORKFLOWS.glob("*.yml")):
        text = path.read_text(encoding="utf-8")
        if "runs-on: [self-hosted," not in text or _PROBE not in text:
            continue
        probe_workflows.append(path.name)
        if _UNSAFE in text or text.count(_PROBE) != text.count(_SAFE):
            unsafe_workflows.append(path.name)

    assert probe_workflows
    assert not unsafe_workflows, unsafe_workflows
