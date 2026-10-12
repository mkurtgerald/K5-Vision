"""Run the served JavaScript's deterministic DOM/fetch-double regressions."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def test_operator_camera_dialog_request_ownership():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Operator web interaction unit tests require Node.js with node:test support")
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [node, "--test", str(root / "tests/javascript/operator_camera_dialog.test.cjs")],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
