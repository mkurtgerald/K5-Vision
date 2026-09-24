"""Keep qualification admission and publication explicit across all workflows."""

from pathlib import Path

import pytest

_WORKFLOW = ".github/workflows/physical-validation.yml"
_WORKFLOWS = Path(".github/workflows")
_PHYSICAL = tuple(
    path
    for path in sorted(_WORKFLOWS.glob("*.yml"))
    if "runs-on: [self-hosted," in path.read_text(encoding="utf-8")
)
_TARGETED_DISPATCH = {
    "stage04-physical.yml",
    "stage05-physical.yml",
    "stage06-physical.yml",
    "stage31-presentation-controller.yml",
    "stage32-presentation-runtime.yml",
    "stage33-presentation-host.yml",
    "stage34-presentation-replacement.yml",
    "stage35-windows-presentation-surface.yml",
    "stage-one-operator-physical.yml",
    "stage-one-remaining-physical-suite.yml",
}
_ANALYTICS_PHYSICAL = {
    "stage-one-operator-physical.yml",
    "stage-one-remaining-physical-suite.yml",
}
_CHECKOUT_ACTION = "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"
_PRIVATE_CAMERA_BINDINGS = (
    "K5_STAGE03_SOURCE: ${{ secrets.K5_STAGE03_SOURCE }}",
    "K5_STAGE03_CAM_CRED: ${{ secrets.CAM_CRED }}",
)
_STAGE_ONE_CAMERA_STEPS = {
    "stage-one-operator-physical.yml": {
        "Verify private physical configuration",
        "Run authenticated physical operator witness",
    },
    "stage-one-remaining-physical-suite.yml": {
        "Verify private physical configuration",
        "Resolve private camera credential candidate once",
        "Run Stage 05 physical readiness",
        "Run Stage 06 physical live view",
        "Run Stage 31 controller qualification",
        "Run Stage 32 runtime qualification",
        "Run Stage 33 host qualification",
        "Run Stage 34 replacement qualification",
        "Run Stage 35 Windows surface qualification",
        "Run authenticated Stage One operator witness",
    },
}


def _workflow_text() -> str:
    return Path(_WORKFLOW).read_text(encoding="utf-8")


def _analytics_sha(text: str) -> str:
    marker = "ANALYTICS_LAB_SHA: "
    assert marker in text
    return text.split(marker, maxsplit=1)[1].splitlines()[0].strip()


def _named_step(text: str, name: str) -> str:
    marker = f"      - name: {name}\n"
    assert marker in text
    return text.split(marker, maxsplit=1)[1].split("      - name: ", maxsplit=1)[0]


def test_stage03_physical_workflow_is_manual_only() -> None:
    trigger_block = _workflow_text().split("\npermissions:", maxsplit=1)[0]

    assert "\n  workflow_dispatch:\n" in trigger_block
    assert "\n  push:" not in trigger_block
    assert "\n  pull_request:" not in trigger_block


def test_stage03_retained_evidence_requires_success() -> None:
    upload_block = _workflow_text().split(
        "- name: Upload retained qualification evidence", maxsplit=1
    )[1]
    guard_block = upload_block.split("uses:", maxsplit=1)[0]

    assert "if: success()" in guard_block
    assert "if: always()" not in guard_block


def test_stage03_private_source_never_enters_probe_process_argv() -> None:
    physical = _workflow_text()
    remaining = (_WORKFLOWS / "stage-one-remaining-physical-suite.yml").read_text(encoding="utf-8")
    unsafe = "stage03_credential_probe $env:K5_STAGE03_SOURCE"
    safe = 'stage03_credential_probe "env:K5_STAGE03_SOURCE"'

    assert unsafe not in physical
    assert unsafe not in remaining
    assert safe in physical
    assert safe in remaining


def test_qualification_inventory_remains_present() -> None:
    assert len(_PHYSICAL) == 26
    names = {path.name for path in _PHYSICAL}
    assert _TARGETED_DISPATCH.issubset(names)
    assert all(path.is_file() for path in _PHYSICAL)


@pytest.mark.parametrize("path", _PHYSICAL, ids=lambda path: path.stem)
def test_qualification_requires_explicit_reviewed_revision(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    triggers = text.split("\non:\n", maxsplit=1)[1].split("\npermissions:", maxsplit=1)[0]
    admission = text.split("\njobs:\n", maxsplit=1)[1].split("    runs-on:", maxsplit=1)[0]

    assert "    if: >-\n" in admission
    assert "github.event_name == 'workflow_dispatch' &&" in admission
    assert "github.actor == github.repository_owner &&" in admission
    assert "github.triggering_actor == github.repository_owner &&" in admission
    assert "github.event.pull_request.head.sha" not in text

    checkouts = [part for part in text.split("      - name: ") if "uses: actions/checkout@" in part]
    primary_checkouts = [part for part in checkouts if "          repository:" not in part]
    assert primary_checkouts
    assert all("          persist-credentials: false\n" in checkout for checkout in checkouts)

    if path.name in _TARGETED_DISPATCH:
        assert triggers == (
            "  workflow_dispatch:\n"
            "    inputs:\n"
            "      reviewed_branch:\n"
            "        description: Exact branch whose current head is being qualified\n"
            "        required: true\n"
            "        type: string\n"
            "      reviewed_sha:\n"
            "        description: Exact reviewed commit selected for this qualification\n"
            "        required: true\n"
            "        type: string\n"
        )
        assert "github.ref == 'refs/heads/main' &&\n" in admission
        assert "inputs.reviewed_branch == 'main' &&\n" in admission
        assert "inputs.reviewed_sha == github.sha\n" in admission
        assert "K5_REVIEWED_BRANCH: ${{ inputs.reviewed_branch }}" in text
        assert "K5_REVIEWED_SHA: ${{ inputs.reviewed_sha }}" in text
        assert "https://api.github.com/repos/$env:GITHUB_REPOSITORY/branches/" in text
        assert "$branch.commit.sha -ne $env:K5_REVIEWED_SHA" in text
        for checkout in primary_checkouts:
            assert "          ref: ${{ inputs.reviewed_sha }}\n" in checkout
    else:
        assert triggers == (
            "  workflow_dispatch:\n"
            "    inputs:\n"
            "      reviewed_sha:\n"
            "        description: Exact reviewed commit selected for this qualification\n"
            "        required: true\n"
            "        type: string\n"
        )
        assert "inputs.reviewed_sha == github.sha\n" in admission
        for checkout in primary_checkouts:
            assert "          ref: ${{ github.sha }}\n" in checkout


@pytest.mark.parametrize("path", _PHYSICAL, ids=lambda path: path.stem)
def test_qualification_upload_requires_its_validation_outcome(path: Path) -> None:
    steps = path.read_text(encoding="utf-8").split("      - name: ")[1:]
    validators = [step for step in steps if "        id: safe_evidence\n" in step]
    uploads = [step for step in steps if "uses: actions/upload-artifact@" in step]
    assert uploads
    if validators:
        assert len(validators) == 1
        validation = validators[0]
        assert "        if: always()\n" in validation
        assert "source-free" in validation.split("\n", maxsplit=1)[0]
        assert "throw " in validation
        for upload in uploads:
            assert steps.index(validation) < steps.index(upload)
            header = upload.split("        uses:", maxsplit=1)[0]
            assert "        if: success() && steps.safe_evidence.outcome == 'success'\n" in header
            assert "if: always()" not in header
    else:
        for upload in uploads:
            header = upload.split("        uses:", maxsplit=1)[0]
            assert "        if: success()\n" in header
            assert "if: always()" not in header


def test_stage_one_physical_paths_bind_same_reviewed_analytics_revision() -> None:
    compatibility = (_WORKFLOWS / "stage-one-analytics-compat.yml").read_text(encoding="utf-8")
    expected_sha = _analytics_sha(compatibility)

    for name in sorted(_ANALYTICS_PHYSICAL):
        text = (_WORKFLOWS / name).read_text(encoding="utf-8")
        assert _analytics_sha(text) == expected_sha
        checkout = next(
            part
            for part in text.split("      - name: ")
            if "          repository: mkurtgerald/Analytics-lab\n" in part
        )
        assert f"        uses: {_CHECKOUT_ACTION} # v7\n" in checkout
        assert "          ref: ${{ env.ANALYTICS_LAB_SHA }}\n" in checkout
        assert "          path: analytics-lab\n" in checkout
        assert "          persist-credentials: false\n" in checkout
        assert "steps.analytics_checkout.outputs.commit" not in text
        assert (
            "https://api.github.com/repos/mkurtgerald/Analytics-lab/commits/"
            "$env:ANALYTICS_LAB_SHA"
        ) in text
        assert (
            "$commit.sha.ToLowerInvariant() -ne "
            "$env:ANALYTICS_LAB_SHA.ToLowerInvariant()"
        ) in text
        assert "K5_ANALYTICS_EVIDENCE_ROOT" in text
        assert "analytics_lab.validation_seed" in text
        assert '"openvino==2026.3.1"' in text
        assert '"opencv-python-headless==4.12.0.88"' in text
        assert "git -C $analytics fetch" not in text


def test_stage_one_reviewed_main_lookup_is_canonicalized() -> None:
    for name in sorted(_ANALYTICS_PHYSICAL):
        text = (_WORKFLOWS / name).read_text(encoding="utf-8")
        verify = _named_step(text, "Verify exact reviewed branch head")
        assert "$reviewedBranch = $env:K5_REVIEWED_BRANCH.ToLowerInvariant()" in verify
        assert '$reviewedBranch -ne "main"' in verify
        assert "[uri]::EscapeDataString($reviewedBranch)" in verify
        assert "[uri]::EscapeDataString($env:K5_REVIEWED_BRANCH)" not in verify


def test_stage_one_private_camera_configuration_is_step_scoped() -> None:
    for name, consumers in _STAGE_ONE_CAMERA_STEPS.items():
        text = (_WORKFLOWS / name).read_text(encoding="utf-8")
        job_scope = text.split("    steps:\n", maxsplit=1)[0]
        for binding in _PRIVATE_CAMERA_BINDINGS:
            assert binding not in job_scope

        for step_name in consumers:
            step = _named_step(text, step_name)
            for binding in _PRIVATE_CAMERA_BINDINGS:
                assert f"          {binding}\n" in step

        action_steps = [
            part for part in text.split("      - name: ")[1:] if "        uses: actions/" in part
        ]
        assert action_steps
        for step in action_steps:
            for binding in _PRIVATE_CAMERA_BINDINGS:
                assert binding not in step


def test_remaining_stage_one_suite_covers_every_required_gate() -> None:
    text = (_WORKFLOWS / "stage-one-remaining-physical-suite.yml").read_text(encoding="utf-8")
    required_tests = {
        "tests/integration/test_stage05_physical.py",
        "tests/integration/test_stage06_physical.py",
        "tests/integration/test_stage31_physical.py",
        "tests/integration/test_stage32_physical.py",
        "tests/integration/test_stage33_physical.py",
        "tests/integration/test_stage34_physical.py",
        "tests/integration/test_stage35_physical.py",
        "tests/integration/test_stage_one_operator_physical.py",
    }
    assert required_tests.issubset(set(text.split()))
    assert text.count("stage03_credential_probe") == 1
    assert "if-no-files-found: error" in text
    assert "stage-one-remaining-physical-suite-evidence" in text
