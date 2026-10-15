"""Source-free hosted diagnosis must exercise the immutable installed product."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/owner-installed-runtime-diagnosis.yml"
TEXT = WORKFLOW.read_text(encoding="utf-8")
INSTALL, DIAGNOSIS = TEXT.split(
    "      - name: Diagnose exact installed launcher and decoder with source-free projection\n", 1
)
DIAGNOSIS, CLEANUP = DIAGNOSIS.split("      - name: Remove only owned installed application\n", 1)
SCRIPT = "\n".join(line[10:] for line in DIAGNOSIS.splitlines() if line.startswith("          "))
PYTHON = SCRIPT.split("$diagnostic = @'\n", 1)[1].split("\n'@", 1)[0]


def test_diagnosis_uses_installer_owned_isolated_interpreter():
    assert "shell: python" not in TEXT
    assert "'K5VisionAlpha\\python312\\python.exe'" in DIAGNOSIS
    assert "$diagnostic | & $python -I -B -" in DIAGNOSIS
    assert "sys.flags.isolated == 1" in PYTHON
    assert "sys.version_info[:3] == (3, 12, 10)" in PYTHON
    assert "pathlib.Path(sys.executable).resolve()" in PYTHON
    assert "$LASTEXITCODE -ne 0" in DIAGNOSIS
    ast.parse(PYTHON)


def test_exact_installer_archive_and_executable_stay_independently_bound():
    assert "11702177807/zip" in INSTALL
    assert "ca581625d82ce0abebd23f632702a4002f0a05dea813bf76579b5affbbcb607e" in INSTALL
    assert "78a677e6ee31734999e1d8a402ac6129cf66d59976890e017de89b71536553b0" in INSTALL
    revision = "70b24135a085c8bb1dca23253a3c25fef9e18164"
    assert f"K5VisionAlpha-Setup-{revision}.exe" in INSTALL
    assert f"== '{revision}'" in PYTHON
    assert "Get-FileHash $zip -Algorithm SHA256" in INSTALL
    assert "Get-FileHash $setup -Algorithm SHA256" in INSTALL


def test_cleanup_requires_this_runs_ownership_after_absence_admission():
    assert "        id: install\n" in INSTALL
    absent = INSTALL.index("if (Test-Path -LiteralPath $root)")
    ownership = INSTALL.index("'owned_install=true'")
    install = INSTALL.index("$p = Start-Process -FilePath $setup")
    assert absent < ownership < install
    assert "if: always() && steps.install.outputs.owned_install == 'true'" in CLEANUP
    assert "Remove-Item" not in CLEANUP


def test_hosted_diagnosis_retains_read_only_and_no_public_acceptance_boundaries():
    assert "runs-on: windows-2025" in TEXT
    assert "RUNNER_ENVIRONMENT -cne 'github-hosted'" in INSTALL
    assert "permissions:\n  contents: read\n  actions: read\n" in TEXT
    assert "'K5_OWNER_PUBLIC_ACCEPTANCE=not_proven'" in PYTHON
    assert "& $launcher -ExitAfterPublicTest" in PYTHON
    assert "-PublicRtspSource" not in PYTHON
    assert "avdec_h264" not in PYTHON
    assert "write_text" not in PYTHON and "write_bytes" not in PYTHON


def test_capture_retains_bounded_memory_and_fixed_output_projection():
    assert "len(destination) + len(block) <= 65536" in PYTHON
    assert "overflow.is_set() or any(t.is_alive() for t in threads)" in PYTHON
    assert "re.fullmatch(r'K5_RUNTIME_DIAG_" in PYTHON
    assert "print(output)" not in PYTHON
    assert "print('K5_RUNTIME_DIAG_ELEMENT_' + element" in PYTHON


@pytest.mark.parametrize(
    ("code", "output", "passed"),
    [
        (0, "K5_RUNTIME_DIAG_LAUNCH=passed\n", True),
        (0, "K5_RUNTIME_DIAG_LAUNCH=failed\n", False),
        (1, "K5_RUNTIME_DIAG_LAUNCH=passed\n", False),
        (0, "prefix K5_RUNTIME_DIAG_LAUNCH=passed\n", False),
        (0, "", False),
    ],
)
def test_diagnostic_exit_cannot_treat_a_failed_launch_as_success(code, output, passed):
    tree = ast.parse(PYTHON)
    gate = tree.body[-1]
    assert isinstance(gate, ast.If)
    assert isinstance(gate.body[0], ast.Raise)
    condition = compile(ast.Expression(gate.test), "<diagnostic-exit-contract>", "eval")
    assert bool(eval(condition, {"__builtins__": {}}, {"code": code, "output": output})) != passed
