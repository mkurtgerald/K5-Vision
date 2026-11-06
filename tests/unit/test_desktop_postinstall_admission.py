"""Contract: click-through installation must verify sealed bytes and exact final tree."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_native_setup_verifies_bytes_during_extraction_and_exact_tree_before_success():
    setup = (ROOT / "windows/desktop/K5Vision.iss").read_text(encoding="utf-8")
    files = setup.split("[Files]", 1)[1].split("[Icons]", 1)[0]
    assert files.index('Source: "{#PayloadRoot}\\*"' ) < files.index(
        "AfterInstall: AdmitExtractedPayload"
    )
    assert 'DestDir: "{tmp}"' in files
    assert "deleteafterinstall" in files

    early = setup.split("procedure AdmitExtractedPayload();", 1)[1].split(
        "procedure CurStepChanged(Step: TSetupStep);", 1
    )[0]
    assert "ExpandConstant('{app}\\K5Vision.exe')" in early
    assert "'--installation-extraction-check'" in early
    assert "'--installation-payload-check'" not in early
    assert "ewWaitUntilTerminated" in early
    assert "(ExitCode <> 0)" in early
    assert "RaiseException(" in early
    assert "ReleaseInstallationLease();" not in early

    post = setup.split("procedure CurStepChanged(Step: TSetupStep);", 1)[1].split(
        "procedure DeinitializeSetup();", 1
    )[0]
    assert "Step = ssPostInstall" in post
    assert "'--installation-payload-check'" in post
    assert "ewWaitUntilTerminated" in post
    assert "(ExitCode <> 0)" in post
    assert "RaiseException(" in post
    assert post.index("RaiseException(") < post.index("ReleaseInstallationLease();")


def test_supervisor_staged_and_final_admission_remain_sealed_and_state_free():
    source = (ROOT / "windows/desktop/K5Vision.cs").read_text(encoding="utf-8")
    strict = source.split('arguments[0] == "--installation-payload-check"', 1)[1].split(
        'arguments[0] == "--installation-extraction-check"', 1
    )[0]
    staged = source.split('arguments[0] == "--installation-extraction-check"', 1)[1].split(
        'arguments[0] == "--installation-identity"', 1
    )[0]
    assert "Custody.VerifyPayload(Path.GetDirectoryName(Application.ExecutablePath))" in strict
    assert "Custody.VerifySealedFiles(Path.GetDirectoryName(Application.ExecutablePath))" in staged
    assert "catch { return 1; }" in strict
    assert "catch { return 1; }" in staged
    assert "PrepareProfile(" not in strict + staged
    assert "OperatorWindow(" not in strict + staged
    assert "Process.Start(" not in strict + staged
    assert "VerifyPayload(root, false);" in source
    assert "VerifyPayload(root, true);" in source
    assert "if (requireFullTree) VerifyTree(root, root, admitted);" in source
