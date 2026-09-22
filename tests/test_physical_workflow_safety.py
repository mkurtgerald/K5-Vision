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


def _workflow_text() -> str:
    return Path(_WORKFLOW).read_text(encoding="utf-8")


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


def test_qualification_inventory_remains_present() -> None:
    assert len(_PHYSICAL) == 25
    assert {
        "stage04-physical.yml",
        "stage05-physical.yml",
        "stage35-windows-presentation-surface.yml",
        "stage-one-operator-physical.yml",
    }.issubset({path.name for path in _PHYSICAL})
    assert all(path.is_file() for path in _PHYSICAL)


@pytest.mark.parametrize("path", _PHYSICAL, ids=lambda path: path.stem)
def test_qualification_requires_explicit_reviewed_revision(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    triggers = text.split("\non:\n", maxsplit=1)[1].split("\npermissions:", maxsplit=1)[0]
    assert triggers == (
        "  workflow_dispatch:\n"
        "    inputs:\n"
        "      reviewed_sha:\n"
        "        description: Exact reviewed commit selected for this qualification\n"
        "        required: true\n"
        "        type: string\n"
    )
    admission = text.split("\njobs:\n", maxsplit=1)[1].split("    runs-on:", maxsplit=1)[0]
    assert "    if: >-\n" in admission
    assert "github.event_name == 'workflow_dispatch' &&" in admission
    assert "github.actor == github.repository_owner &&" in admission
    assert "github.triggering_actor == github.repository_owner &&" in admission
    assert "inputs.reviewed_sha == github.sha\n" in admission
    assert "github.event.pull_request.head.sha" not in text
    checkouts = [part for part in text.split("      - name: ") if "uses: actions/checkout@" in part]
    assert checkouts
    for checkout in checkouts:
        assert "          ref: ${{ github.sha }}\n" in checkout
        assert "          persist-credentials: false\n" in checkout


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
            assert "        if: always() && steps.safe_evidence.outcome == 'success'\n" in header
    else:
        for upload in uploads:
            header = upload.split("        uses:", maxsplit=1)[0]
            assert "        if: success()\n" in header
            assert "if: always()" not in header
