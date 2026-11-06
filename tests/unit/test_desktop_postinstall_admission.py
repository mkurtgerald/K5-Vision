"""Contract: a click-through install must admit its actual extracted payload."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_native_setup_refuses_success_before_finalizing_uninstall_log():
    setup = (ROOT / "windows/desktop/K5Vision.iss").read_text(encoding="utf-8")
    files = setup.split("[Files]", 1)[1].split("[Icons]", 1)[0]
    assert files.index('Source: "{#PayloadRoot}\\*"') < files.index(
        "AfterInstall: AdmitExtractedPayload"
    )
    assert 'DestDir: "{tmp}"' in files
    assert "deleteafterinstall" in files
    code = setup.split("procedure AdmitExtractedPayload();", 1)[1].split(
        "procedure CurStepChanged(Step: TSetupStep);", 1
    )[0]
    assert "ExpandConstant('{app}\\K5Vision.exe')" in code
    assert "'--installation-payload-check'" in code
    assert "ewWaitUntilTerminated" in code
    assert "(ExitCode <> 0)" in code
    assert "RaiseException(" in code
    assert "ReleaseInstallationLease();" not in code


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
