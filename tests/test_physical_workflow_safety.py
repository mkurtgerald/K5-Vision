_WORKFLOW = ".github/workflows/physical-validation.yml"


def _workflow_text() -> str:
    with open(_WORKFLOW, encoding="utf-8") as workflow:
        return workflow.read()


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
