"""Keep the physical job's detector dependencies out of shared runner Python."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

WORKFLOW = Path(".github/workflows/stage-one-operator-physical.yml")


def _step(name: str) -> str:
    text = WORKFLOW.read_text(encoding="utf-8")
    return text.split(f"      - name: {name}\n", 1)[1].split("      - name: ", 1)[0]


def test_isolation_is_created_after_cache_setup_before_dependency_install() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    setup = text.index("      - name: Set up Python\n")
    isolation = text.index("      - name: Create job-local Python environment\n")
    install = text.index("      - name: Install K5 and pinned detector evidence runtimes\n")
    assert setup < isolation < install
    assert "PIP_REQUIRE_VIRTUALENV" not in text[:isolation]
    isolated = _step("Create job-local Python environment")
    for token in (
        "$env:RUNNER_TEMP",
        "$env:GITHUB_RUN_ID",
        "$env:GITHUB_RUN_ATTEMPT",
        "Test-Path -LiteralPath $venvPath",
        "python -m venv $venvPath",
        "sys.prefix != sys.base_prefix",
        "site.ENABLE_USER_SITE is False",
        '"VIRTUAL_ENV=$venvPath"',
        '"PIP_REQUIRE_VIRTUALENV=true"',
    ):
        assert token in isolated
    assert isolated.index("sys.prefix != sys.base_prefix") < isolated.index("$env:GITHUB_PATH")
    assert "Split-Path -Parent $venvPython" in isolated
    assert "--system-site-packages" not in isolated


def test_dependency_installs_use_selected_python_and_fail_closed() -> None:
    install = _step("Install K5 and pinned detector evidence runtimes")
    assert install.count("python -m pip install") == 3
    assert install.count("if ($LASTEXITCODE -ne 0)") == 3
    assert "\n          pip install" not in install
    assert '"openvino==2026.3.1" "opencv-python-headless==4.12.0.88"' in install


def test_existing_analytics_path_and_acceptance_boundaries_are_preserved() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert 'PYTHONHOME: ""' in text
    assert '"PYTHONPATH=$analytics"' in _step("Bind reviewed Analytics-lab revision")
    assert "PYTHONPATH" not in _step("Create job-local Python environment")
    assert "runs-on: [self-hosted, Windows, X64, k5-physical, camera-lab]" in text
    assert "if: github.ref == 'refs/heads/main'" in text
    assert "timeout-minutes: 35" in text
    assert "ANALYTICS_LAB_SHA: c8b347ae538991a0c0ce38eabc2dc17b566531d3" in text
    assert (
        r"run: .\scripts\windows-alpha\Invoke-K5VisionAlphaWitness.ps1"
        " -Output $env:K5_ALPHA_DIRECT_RTSP_OUTPUT"
    ) in _step("Prove installed alpha generated RTSP Windows presentation")


def test_private_venv_excludes_shared_site_packages_and_keeps_analytics_path(
    tmp_path: Path,
) -> None:
    target = tmp_path / "isolated"
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(target)],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    python = target / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    analytics = tmp_path / "reviewed-analytics"
    analytics.mkdir()
    (analytics / "isolation_fixture.py").write_text("VALUE = 1\n", encoding="utf-8")
    env = os.environ.copy()
    env.update({"PYTHONHOME": "", "PYTHONPATH": str(analytics)})
    code = (
        "import json,site,sys,isolation_fixture;"
        "print(json.dumps({'prefix':sys.prefix,'base':sys.base_prefix,"
        "'user_site':site.ENABLE_USER_SITE,'path':sys.path,'fixture':isolation_fixture.VALUE}))"
    )
    result = subprocess.run(
        [str(python), "-c", code],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    state = json.loads(result.stdout)
    assert Path(state["prefix"]).resolve() == target.resolve()
    assert state["prefix"] != state["base"]
    assert state["user_site"] is False
    assert state["fixture"] == 1
    for path in state["path"]:
        if path.replace("\\", "/").endswith("site-packages"):
            assert Path(path).resolve().is_relative_to(target.resolve())
