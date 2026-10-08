"""Camera-free owner GUI contract, separate from the actual installer-release gate."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "scripts" / "windows-alpha" / "owner-installer" / "K5VisionAlpha.cs"
WORKFLOW = ROOT / ".github" / "workflows" / "windows-alpha-script-smoke.yml"


class OwnerGuiContract(unittest.TestCase):
    def test_gui_not_a_console_or_shell_handoff(self):
        source = SOURCE.read_text(encoding="utf-8")
        for marker in (
            'Application.Run(new Launcher())',
            'info.CreateNoWindow = true;',
            'info.RedirectStandardInput = true;',
            'info.RedirectStandardOutput = true;',
            'info.RedirectStandardError = true;',
            '-ExitAfterPublicTest',
            'uri.UserInfo.Length != 0',
            'Private or local camera addresses',
            'Recording is disabled',
            '--self-check',
            'File.Exists(gst)',
        ):
            if marker == 'Private or local camera addresses':
                marker = 'private or local camera addresses'
            self.assertIn(marker, source)
        self.assertNotIn('Status(line)', source)
        self.assertNotIn('Status(e.Data)', source)
        self.assertNotIn('MessageBox.Show(this, e.Data', source)
        self.assertNotIn('UseShellExecute = true', source)

    def test_windows_hosted_compiler_is_mandatory_for_gui_changes(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn('scripts/windows-alpha/**', workflow)
        self.assertIn('Compile camera-free owner GUI candidate', workflow)
        self.assertIn('K5VisionAlpha.cs', workflow)
        self.assertIn('K5_OWNER_GUI_HOSTED_COMPILE=passed', workflow)
        self.assertIn('if ($LASTEXITCODE -ne 3)', workflow)
        self.assertIn("$env:GITHUB_WORKSPACE", workflow)
        self.assertIn("'scripts\\windows-alpha\\owner-installer\\K5VisionAlpha.cs'", workflow)


if __name__ == "__main__":
    unittest.main()
