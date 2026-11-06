"""Contract: a click-through install must admit its actual extracted payload."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_native_setup_refuses_success_before_installed_payload_admission():
    setup = (ROOT / "windows/desktop/K5Vision.iss").read_text(encoding="utf-8")
    code = setup.split("procedure CurStepChanged(Step: TSetupStep);", 1)[1].split(
        "procedure DeinitializeSetup();", 1
    )[0]
    assert "if Step = ssPostInstall then begin" in code
    assert "ExpandConstant('{app}\\K5Vision.exe')" in code
    assert "'--installation-payload-check'" in code
    assert "ewWaitUntilTerminated" in code
    assert "(ExitCode <> 0)" in code
    assert "RaiseException(" in code
    assert code.index("RaiseException(") < code.index("ReleaseInstallationLease();")


def test_supervisor_payload_admission_uses_sealed_hashes_without_profile_mutation():
    source = (ROOT / "windows/desktop/K5Vision.cs").read_text(encoding="utf-8")
    code = source.split('arguments[0] == "--installation-payload-check"', 1)[1].split(
        'arguments[0] == "--installation-identity"', 1
    )[0]
    assert "Custody.VerifyPayload(Path.GetDirectoryName(Application.ExecutablePath))" in code
    assert "catch { return 1; }" in code
    assert "PrepareProfile(" not in code
    assert "OperatorWindow(" not in code
    assert "Process.Start(" not in code
