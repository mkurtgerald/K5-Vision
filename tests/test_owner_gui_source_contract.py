"""Camera-free owner GUI contract, separate from the actual installer-release gate."""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "scripts" / "windows-alpha" / "owner-installer" / "K5VisionAlpha.cs"
WORKFLOW = ROOT / ".github" / "workflows" / "windows-alpha-script-smoke.yml"


class OwnerGuiContract(unittest.TestCase):
    def test_gui_not_a_console_or_shell_handoff(self):
        source = SOURCE.read_text(encoding="utf-8")
        for marker in (
            "Application.Run(new Launcher())",
            "info.CreateNoWindow = true;",
            "info.RedirectStandardInput = true;",
            "info.RedirectStandardOutput = true;",
            "info.RedirectStandardError = true;",
            "-ExitAfterPublicTest",
            "uri.UserInfo.Length != 0",
            "Private or local camera addresses",
            "Recording is disabled",
            "--self-check",
            "File.Exists(gst)",
        ):
            if marker == "Private or local camera addresses":
                marker = "private or local camera addresses"
            self.assertIn(marker, source)
        self.assertNotIn("Status(line)", source)
        self.assertNotIn("Status(e.Data)", source)
        self.assertNotIn("MessageBox.Show(this, e.Data", source)
        self.assertNotIn("UseShellExecute = true", source)

    def test_gui_requires_positive_receipts_not_just_zero_process_exit(self):
        source = SOURCE.read_text(encoding="utf-8")
        for marker in (
            "finished.WaitForExit()",
            "healthEvidence && presentationEvidence && privacyEvidence",
            "video && analyticsEvidence && boxesEvidence",
            "analyticsEvidence = true",
            "boxesEvidence = true",
            "presentationEvidence = true",
            "Stage-One NOT qualified:",
            "public RTSP analytics NOT qualified.",
            "No test-stream recording or retained media was created.",
            "submissions=([1-9][0-9]*)",
            "presentations=([1-9][0-9]*)",
        ):
            self.assertIn(marker, source)
        self.assertNotIn('result == 0 ? "Test completed."', source)
        self.assertIn(
            "bool full = source.Length > 0 && video && analyticsEvidence && boxesEvidence;",
            source,
        )
        self.assertNotIn("bool full = video && analyticsEvidence && boxesEvidence;", source)

    def test_gui_explains_missing_analytics_to_owner(self):
        source = SOURCE.read_text(encoding="utf-8")
        self.assertIn("K5 analytics disabled; video-only alpha acceptance selected.", source)
        self.assertIn("Analytics is not provisioned or configured.", source)
        self.assertIn("Status(failure);", source)

    def test_windows_hosted_compiler_is_mandatory_for_gui_changes(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("scripts/windows-alpha/**", workflow)
        self.assertIn("Compile camera-free owner GUI candidate", workflow)
        self.assertIn("K5VisionAlpha.cs", workflow)
        self.assertIn("K5_OWNER_GUI_HOSTED_COMPILE=passed", workflow)
        self.assertIn(
            "Start-Process -FilePath $output -ArgumentList '--self-check' -Wait -PassThru",
            workflow,
        )
        self.assertIn("if ($guiProbe.ExitCode -ne 3)", workflow)
        self.assertIn("$env:GITHUB_WORKSPACE", workflow)
        self.assertIn("'scripts\\windows-alpha\\owner-installer\\K5VisionAlpha.cs'", workflow)


if __name__ == "__main__":
    unittest.main()
