"""Keep the owner-gated Stage One witness failure output privacy-bounded."""

from pathlib import Path


_WORKFLOW = Path(".github/workflows/stage-one-operator-physical.yml")


def test_stage_one_physical_witness_suppresses_unbounded_pytest_failure_output() -> None:
    text = _WORKFLOW.read_text(encoding="utf-8")
    step = text.split(
        "      - name: Run authenticated physical operator witness\n", maxsplit=1
    )[1].split("      - name: ", maxsplit=1)[0]

    assert "--tb=no" in step
    assert "--show-capture=no" in step
    assert "K5_STAGE03_SOURCE: ${{ secrets.K5_STAGE03_SOURCE }}" in step
    assert "K5_STAGE03_CAM_CRED: ${{ secrets.CAM_CRED }}" in step


def test_stage_one_witness_output_path_is_workspace_relative() -> None:
    text = _WORKFLOW.read_text(encoding="utf-8")

    assert "K5_STAGE_ONE_OUTPUT: artifacts\\stage-one-operator-physical.json" in text
    assert (
        "K5_STAGE_ONE_OUTPUT: ${{ github.workspace }}\\artifacts\\stage-one-operator-physical.json"
        not in text
    )
